from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional, List, Tuple, Dict, Any

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Max, Sum
from django.utils import timezone

from notifications.models import Notification

from .models import (
    MerryGoRound,
    MerryMember,
    MerrySeat,
    MerryJoinRequest,
    MerrySlotConfig,
    MerryContributionDue,
    MerryPayment,
    MerryPaymentAllocation,
    MerryPayout,
    MerryWallet,
    MerryWalletTransaction,
)

# Loan-side integration for merry payout offsets
try:
    from loans.models import LoanSecurityAllocation, Loan
    from loans.services import apply_merry_payout_to_active_loan
except Exception:  # pragma: no cover
    LoanSecurityAllocation = None
    Loan = None

    def apply_merry_payout_to_active_loan(*, payout):  # type: ignore
        return {
            "applied_to_loan": Decimal("0.00"),
            "remaining_amount": Decimal(getattr(payout, "amount", Decimal("0.00"))),
            "loan_ids": [],
        }


User = get_user_model()


# -----------------------------
# Domain errors
# -----------------------------
class MerryServiceError(Exception):
    pass


class NotAllowed(MerryServiceError):
    pass


class NotFound(MerryServiceError):
    pass


class BadState(MerryServiceError):
    pass


class Conflict(MerryServiceError):
    pass


# -----------------------------
# Helpers
# -----------------------------
def q2(x: Decimal | str | int | float) -> Decimal:
    return Decimal(x).quantize(Decimal("0.01"))


def parse_decimal(value, field_name: str) -> Decimal:
    if value is None or value == "":
        raise BadState(f"{field_name} is required.")
    try:
        return q2(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        raise BadState(f"{field_name} must be a valid number.")


def parse_int(
    value,
    field_name: str,
    *,
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
) -> int:
    try:
        n = int(value)
    except (ValueError, TypeError):
        raise BadState(f"{field_name} must be an integer.")

    if min_value is not None and n < min_value:
        raise BadState(f"{field_name} must be >= {min_value}.")
    if max_value is not None and n > max_value:
        raise BadState(f"{field_name} must be <= {max_value}.")
    return n


def parse_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)

    s = str(value).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return default


def is_admin(user) -> bool:
    return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


def get_merry(merry_id: int) -> MerryGoRound:
    merry = MerryGoRound.objects.filter(id=merry_id).select_related("created_by").first()
    if not merry:
        raise NotFound("Merry not found.")
    return merry


def get_join_request(request_id: int) -> MerryJoinRequest:
    jr = MerryJoinRequest.objects.select_related("merry", "user").filter(id=request_id).first()
    if not jr:
        raise NotFound("Join request not found.")
    return jr


def get_active_member(merry: MerryGoRound, user) -> MerryMember:
    member = (
        MerryMember.objects.select_related("merry", "user")
        .filter(merry=merry, user=user, is_active=True)
        .first()
    )
    if not member:
        raise NotFound("You are not an active member of this merry.")
    return member


def get_member_by_id(member_id: int, *, lock: bool = False) -> MerryMember:
    qs = MerryMember.objects.select_related("merry", "user")
    if lock:
        qs = qs.select_for_update()
    member = qs.filter(id=member_id).first()
    if not member:
        raise NotFound("Member not found.")
    return member


def _safe_frontend_merry_detail_url(merry_id: int) -> str:
    return f"/merry/{merry_id}"


def _safe_frontend_admin_join_requests_url(merry_id: int) -> str:
    return f"/merry/admin-join-requests?merryId={merry_id}"


def _create_notification(
    *,
    user,
    title: str,
    message: str,
    notification_type: str = "INFO",
    created_by=None,
    action_url: str = "",
    merry_id: Optional[int] = None,
    loan_id: Optional[int] = None,
    group_id: Optional[int] = None,
) -> None:
    if not user or not getattr(user, "id", None):
        return

    Notification.objects.create(
        user=user,
        created_by=created_by if getattr(created_by, "id", None) else None,
        title=(title or "").strip()[:150],
        message=(message or "").strip(),
        notification_type=notification_type,
        action_url=(action_url or "").strip()[:255] or None,
        merry_id=merry_id,
        loan_id=loan_id,
        group_id=group_id,
    )


def _notify_join_request_submitted(
    *,
    user,
    merry: MerryGoRound,
    requested_seats: int,
) -> None:
    _create_notification(
        user=user,
        created_by=user,
        title="Join request submitted",
        message=(
            f"Your request to join {merry.name} for "
            f"{requested_seats} seat(s) has been submitted successfully."
        ),
        notification_type="SUCCESS",
        action_url=_safe_frontend_merry_detail_url(merry.id),
        merry_id=merry.id,
    )

    if merry.created_by_id and merry.created_by_id != user.id:
        actor_name = (
            getattr(user, "username", None)
            or getattr(user, "phone", None)
            or "A member"
        )
        _create_notification(
            user=merry.created_by,
            created_by=user,
            title="New merry join request",
            message=(
                f"{actor_name} requested {requested_seats} seat(s) in "
                f"{merry.name}. Please review and assign seats."
            ),
            notification_type="ACTION",
            action_url=_safe_frontend_admin_join_requests_url(merry.id),
            merry_id=merry.id,
        )


def _notify_join_request_approved(
    *,
    user,
    admin_user,
    merry: MerryGoRound,
    seats_created: List[MerrySeat],
) -> None:
    seat_numbers = [seat.seat_no for seat in seats_created]
    seat_text = ", ".join(str(n) for n in seat_numbers) if seat_numbers else "assigned"

    _create_notification(
        user=user,
        created_by=admin_user,
        title="Join request approved",
        message=(
            f"Your request to join {merry.name} has been approved. "
            f"Assigned seat(s): {seat_text}."
        ),
        notification_type="SUCCESS",
        action_url=_safe_frontend_merry_detail_url(merry.id),
        merry_id=merry.id,
    )


def _notify_join_request_rejected(
    *,
    user,
    admin_user,
    merry: MerryGoRound,
    note: str = "",
) -> None:
    extra = f" Reason: {note.strip()}" if (note or "").strip() else ""

    _create_notification(
        user=user,
        created_by=admin_user,
        title="Join request not approved",
        message=f"Your request to join {merry.name} was not approved.{extra}",
        notification_type="WARNING",
        action_url=_safe_frontend_merry_detail_url(merry.id),
        merry_id=merry.id,
    )


def _notify_join_request_cancelled(
    *,
    user,
    merry: MerryGoRound,
) -> None:
    _create_notification(
        user=user,
        created_by=user,
        title="Join request cancelled",
        message=f"Your join request for {merry.name} has been cancelled.",
        notification_type="INFO",
        action_url=_safe_frontend_merry_detail_url(merry.id),
        merry_id=merry.id,
    )


def get_current_period_key(merry: MerryGoRound) -> str:
    return merry.current_period_key()


def payouts_per_period(merry: MerryGoRound) -> int:
    n = int(getattr(merry, "payouts_per_period", 1) or 1)
    return max(1, n)


def validate_slot(merry: MerryGoRound, slot_no: int) -> None:
    limit = payouts_per_period(merry)
    if slot_no < 1 or slot_no > limit:
        raise BadState(f"slot_no must be between 1 and {limit} for this merry.")


def next_payout_position_for_seat(merry: MerryGoRound) -> int:
    mx = merry.seats.filter(is_active=True).aggregate(m=Max("payout_position")).get("m") or 0
    return int(mx) + 1


def get_next_available_slot(merry: MerryGoRound, period_key: str) -> int:
    limit = payouts_per_period(merry)
    used = set(
        MerryPayout.objects.filter(merry=merry, period_key=period_key).values_list("slot_no", flat=True)
    )
    for s in range(1, limit + 1):
        if s not in used:
            return s
    raise Conflict(f"Payout slots are full for period {period_key}. Max slots: {limit}.")


def get_next_available_seat_numbers(merry: MerryGoRound, count: int) -> List[int]:
    count = parse_int(count, "count", min_value=1)

    if hasattr(merry, "next_available_seat_numbers"):
        return merry.next_available_seat_numbers(count)

    taken = set(MerrySeat.objects.filter(merry=merry).values_list("seat_no", flat=True))

    if merry.max_seats and merry.max_seats > 0:
        available = [n for n in range(1, merry.max_seats + 1) if n not in taken]
        if len(available) < count:
            raise BadState("Not enough available seat numbers.")
        return available[:count]

    picked: List[int] = []
    n = 1
    while len(picked) < count:
        if n not in taken:
            picked.append(n)
        n += 1
    return picked


def _normalize_manual_seat_numbers(
    seat_numbers,
    *,
    field_name: str = "seat_numbers",
) -> List[int]:
    if seat_numbers is None:
        raise BadState(f"{field_name} is required.")

    if not isinstance(seat_numbers, list):
        raise BadState(f"{field_name} must be a list of integers.")

    normalized = [parse_int(v, f"{field_name} item", min_value=1) for v in seat_numbers]

    if not normalized:
        raise BadState(f"{field_name} cannot be empty.")

    if len(set(normalized)) != len(normalized):
        raise BadState(f"{field_name} must contain unique seat numbers.")

    return normalized


def _validate_manual_seat_numbers_for_merry(
    *,
    merry: MerryGoRound,
    seat_numbers: List[int],
    expected_count: Optional[int] = None,
    exclude_seat_id: Optional[int] = None,
) -> List[int]:
    normalized = _normalize_manual_seat_numbers(seat_numbers)

    if expected_count is not None and len(normalized) != expected_count:
        raise BadState(f"Exactly {expected_count} seat number(s) must be provided.")

    if merry.max_seats and merry.max_seats > 0:
        invalid = [s for s in normalized if s > merry.max_seats]
        if invalid:
            raise BadState(
                f"These seat number(s) exceed max_seats ({merry.max_seats}): {sorted(invalid)}"
            )

    qs = MerrySeat.objects.filter(merry=merry, seat_no__in=normalized)
    if exclude_seat_id:
        qs = qs.exclude(id=exclude_seat_id)

    taken = sorted(qs.values_list("seat_no", flat=True))
    if taken:
        raise Conflict(
            f"These seat number(s) are already used in this merry: {taken}"
        )

    return normalized


def _seat_has_financial_history(seat: MerrySeat) -> bool:
    return seat.dues.exists() or seat.payouts.exists()


# ---------- period stepping ----------
def _next_week_period_key(period_key: str) -> str:
    try:
        year = int(period_key[:4])
        week = int(period_key.split("-W")[1])
    except Exception:
        raise BadState("Invalid WEEKLY period_key format. Expected YYYY-W##.")

    d = date.fromisocalendar(year, week, 1) + timedelta(days=7)
    y, w, _ = d.isocalendar()
    return f"{y:04d}-W{w:02d}"


def _next_month_period_key(period_key: str) -> str:
    try:
        year = int(period_key[:4])
        month = int(period_key.split("-")[1])
    except Exception:
        raise BadState("Invalid MONTHLY period_key format. Expected YYYY-MM.")

    month += 1
    if month == 13:
        month = 1
        year += 1
    return f"{year:04d}-{month:02d}"


def _next_period_key(merry: MerryGoRound, period_key: str) -> str:
    if (merry.payout_frequency or "WEEKLY").upper() == "MONTHLY":
        return _next_month_period_key(period_key)
    return _next_week_period_key(period_key)


def get_period_date_range(*, merry: MerryGoRound, period_key: str) -> Dict[str, Any]:
    pk = (period_key or "").strip()
    if not pk:
        raise BadState("period_key is required.")

    freq = (merry.payout_frequency or "WEEKLY").upper()

    if freq == "MONTHLY":
        try:
            year = int(pk[:4])
            month = int(pk.split("-")[1])
        except Exception:
            raise BadState("Invalid MONTHLY period_key format. Expected YYYY-MM.")
        start = date(year, month, 1)
        end = date(year, month, monthrange(year, month)[1])
        return {
            "period_key": pk,
            "label": start.strftime("%B %Y"),
            "start_date": start,
            "end_date": end,
            "frequency": freq,
        }

    try:
        year = int(pk[:4])
        week = int(pk.split("-W")[1])
    except Exception:
        raise BadState("Invalid WEEKLY period_key format. Expected YYYY-W##.")

    start = date.fromisocalendar(year, week, 1)
    end = start + timedelta(days=6)
    return {
        "period_key": pk,
        "label": f"Week {week} of {year}",
        "start_date": start,
        "end_date": end,
        "frequency": freq,
    }


# -----------------------------
# Wallet helpers
# -----------------------------
def _get_or_create_wallet_for_user(user) -> MerryWallet:
    wallet, _ = MerryWallet.objects.select_for_update().get_or_create(
        user=user,
        defaults={"balance": Decimal("0.00")},
    )
    return wallet


def _create_wallet_tx(
    *,
    user,
    tx_type: str,
    amount: Decimal,
    balance_before: Decimal,
    balance_after: Decimal,
    reference: str = "",
    narration: str = "",
    mpesa_receipt_number: Optional[str] = None,
) -> MerryWalletTransaction:
    wallet = _get_or_create_wallet_for_user(user)
    return MerryWalletTransaction.objects.create(
        wallet=wallet,
        user=user,
        tx_type=tx_type,
        amount=q2(amount),
        balance_before=q2(balance_before),
        balance_after=q2(balance_after),
        reference=(reference or "").strip()[:64],
        narration=(narration or "").strip()[:255],
        mpesa_receipt_number=(mpesa_receipt_number or "").strip()[:64] or None,
    )


def add_merry_wallet_credit(
    *,
    user,
    amount: Decimal,
    reference: str = "",
    narration: str = "",
    mpesa_receipt_number: Optional[str] = None,
) -> MerryWallet:
    amt = parse_decimal(amount, "amount")
    if amt <= 0:
        raise BadState("Wallet credit amount must be > 0.")

    wallet = _get_or_create_wallet_for_user(user)
    before = q2(wallet.balance or Decimal("0.00"))
    after = q2(before + amt)

    wallet.balance = after
    wallet.save(update_fields=["balance", "updated_at"])

    _create_wallet_tx(
        user=user,
        tx_type="CREDIT",
        amount=amt,
        balance_before=before,
        balance_after=after,
        reference=reference,
        narration=narration or "Excess merry payment saved to wallet.",
        mpesa_receipt_number=mpesa_receipt_number,
    )
    return wallet


def get_user_merry_wallet_balance(*, user) -> Decimal:
    if not user or not getattr(user, "id", None):
        return Decimal("0.00")
    wallet = MerryWallet.objects.filter(user=user).first()
    return q2(wallet.balance or Decimal("0.00")) if wallet else Decimal("0.00")


# -----------------------------
# Cross-merry allocation helpers
# -----------------------------
def _active_members_for_user(user_id: int) -> List[MerryMember]:
    return list(
        MerryMember.objects.select_related("merry", "user")
        .filter(user_id=user_id, is_active=True, merry__isnull=False)
        .order_by("merry_id", "id")
    )


def _ensure_current_dues_for_user_memberships(user_id: int) -> List[MerryMember]:
    members = _active_members_for_user(user_id)
    for member in members:
        ensure_dues_for_member_period(member.merry, member, get_current_period_key(member.merry))
    return members


def _collect_open_dues_for_member_period(member: MerryMember, period_key: str) -> List[MerryContributionDue]:
    return list(
        MerryContributionDue.objects.select_for_update()
        .filter(
            merry=member.merry,
            seat__member=member,
            seat__is_active=True,
            period_key=period_key,
            status__in=["PENDING", "PARTIAL", "OVERDUE"],
        )
        .select_related("seat", "merry", "seat__member", "seat__member__user")
        .order_by("due_date", "slot_no", "seat__seat_no", "id")
    )


def _collect_open_dues_for_member(member: MerryMember) -> List[MerryContributionDue]:
    current_pk = get_current_period_key(member.merry)
    ensure_dues_for_member_period(member.merry, member, current_pk)

    return list(
        MerryContributionDue.objects.select_for_update()
        .filter(
            merry=member.merry,
            seat__member=member,
            seat__is_active=True,
            status__in=["PENDING", "PARTIAL", "OVERDUE"],
        )
        .select_related("seat", "merry", "seat__member", "seat__member__user")
        .order_by("due_date", "period_key", "slot_no", "seat__seat_no", "id")
    )


def _create_confirmed_payment_shell(
    *,
    member: MerryMember,
    total_amount: Decimal,
    payer_phone: str,
    period_key: str,
    initiated_by=None,
    mpesa_receipt_number: Optional[str] = None,
    paid_at=None,
) -> MerryPayment:
    return MerryPayment.objects.create(
        merry=member.merry,
        beneficiary_member=member,
        initiated_by=initiated_by,
        payer_phone=(payer_phone or "").strip(),
        period_key=period_key,
        amount=q2(total_amount),
        status="CONFIRMED",
        paid_at=paid_at or timezone.now(),
        mpesa_receipt_number=(mpesa_receipt_number or "").strip()[:64] or None,
    )


def _outstanding_amount(due: MerryContributionDue) -> Decimal:
    return q2((due.due_amount or Decimal("0")) - (due.paid_amount or Decimal("0")))


def _due_bucket(due: MerryContributionDue, today=None) -> str:
    today = today or timezone.localdate()

    if due.status in ["PAID", "CANCELLED"]:
        return "closed"

    if due.due_date and due.due_date < today:
        return "overdue"
    if due.due_date and due.due_date == today:
        return "current"
    if due.due_date and due.due_date > today:
        return "future"

    return "current"


def _get_member_next_future_dues(member: MerryMember) -> List[MerryContributionDue]:
    today = timezone.localdate()

    future_dues = list(
        MerryContributionDue.objects
        .filter(
            merry=member.merry,
            seat__member=member,
            seat__is_active=True,
            status__in=["PENDING", "PARTIAL", "OVERDUE"],
            is_advance_payable=True,
            due_date__gt=today,
        )
        .select_related("seat", "merry")
        .order_by("due_date", "slot_no", "seat__seat_no", "id")
    )

    if not future_dues:
        return []

    first_due_date = future_dues[0].due_date
    return [d for d in future_dues if d.due_date == first_due_date]


def _select_member_dues_for_breakdown(
    *,
    member: MerryMember,
    include_next: bool = False,
) -> List[MerryContributionDue]:
    today = timezone.localdate()

    current_pk = get_current_period_key(member.merry)
    ensure_dues_for_member_period(member.merry, member, current_pk)

    open_dues = list(
        MerryContributionDue.objects
        .filter(
            merry=member.merry,
            seat__member=member,
            seat__is_active=True,
            status__in=["PENDING", "PARTIAL", "OVERDUE"],
        )
        .select_related("seat", "merry")
        .order_by("due_date", "slot_no", "seat__seat_no", "id")
    )

    required_dues: List[MerryContributionDue] = []
    for due in open_dues:
        if _outstanding_amount(due) <= 0:
            continue
        bucket = _due_bucket(due, today=today)
        if bucket in ["overdue", "current"]:
            required_dues.append(due)

    if not include_next:
        return required_dues

    next_due_rows = _get_member_next_future_dues(member)
    return required_dues + next_due_rows


def _select_member_dues_for_payment(
    *,
    member: MerryMember,
    include_next: bool = False,
) -> List[MerryContributionDue]:
    today = timezone.localdate()

    current_pk = get_current_period_key(member.merry)
    ensure_dues_for_member_period(member.merry, member, current_pk)

    open_dues = list(
        MerryContributionDue.objects.select_for_update()
        .filter(
            merry=member.merry,
            seat__member=member,
            seat__is_active=True,
            status__in=["PENDING", "PARTIAL", "OVERDUE"],
        )
        .select_related("seat", "merry")
        .order_by("due_date", "slot_no", "seat__seat_no", "id")
    )

    required_dues: List[MerryContributionDue] = []
    for due in open_dues:
        if _outstanding_amount(due) <= 0:
            continue
        bucket = _due_bucket(due, today=today)
        if bucket in ["overdue", "current"]:
            required_dues.append(due)

    if not include_next:
        return required_dues

    next_due_rows = _get_member_next_future_dues(member)
    return required_dues + next_due_rows


@transaction.atomic
def apply_merry_wallet_to_user_open_dues(
    *,
    user_id: int,
    reference: str = "",
    narration: str = "",
    max_amount_to_use: Optional[Decimal] = None
) -> Dict[str, Any]:
    uid = parse_int(user_id, "user_id", min_value=1)
    user = User.objects.filter(id=uid).first()
    if not user:
        raise NotFound("User not found.")

    wallet = _get_or_create_wallet_for_user(user)
    wallet_balance = q2(wallet.balance or Decimal("0.00"))

    if wallet_balance <= 0:
        return {
            "used_amount": Decimal("0.00"),
            "remaining_wallet_balance": Decimal("0.00"),
            "allocations": [],
        }

    available = wallet_balance
    if max_amount_to_use is not None:
        limit = parse_decimal(max_amount_to_use, "max_amount_to_use")
        if limit > 0:
            available = q2(min(wallet_balance, limit))

    members = _ensure_current_dues_for_user_memberships(uid)
    if not members:
        return {
            "used_amount": Decimal("0.00"),
            "remaining_wallet_balance": wallet_balance,
            "allocations": [],
        }

    open_due_rows: List[Tuple[MerryMember, MerryContributionDue]] = []
    for member in members:
        dues = _collect_open_dues_for_member(member)
        for due in dues:
            if _outstanding_amount(due) > 0:
                open_due_rows.append((member, due))

    open_due_rows.sort(
        key=lambda x: (
            x[1].due_date or timezone.localdate(),
            x[1].period_key,
            x[1].slot_no,
            x[0].merry_id,
            x[1].seat.seat_no,
            x[1].id,
        )
    )

    remaining = available
    used_total = Decimal("0.00")
    allocation_rows = []

    for member, due in open_due_rows:
        if remaining <= 0:
            break

        need = _outstanding_amount(due)
        if need <= 0:
            continue

        alloc = remaining if remaining < need else need
        if alloc <= 0:
            continue

        due.paid_amount = q2((due.paid_amount or Decimal("0.00")) + alloc)
        due.recalc_status()
        due.save(update_fields=["paid_amount", "status", "updated_at"])

        remaining = q2(remaining - alloc)
        used_total = q2(used_total + alloc)

        allocation_rows.append({
            "member_id": member.id,
            "merry_id": member.merry_id,
            "due_id": due.id,
            "period_key": due.period_key,
            "slot_no": due.slot_no,
            "seat_no": due.seat.seat_no,
            "amount": q2(alloc),
        })

    if used_total > 0:
        before = wallet_balance
        after = q2(before - used_total)

        wallet.balance = after
        wallet.save(update_fields=["balance", "updated_at"])

        _create_wallet_tx(
            user=user,
            tx_type="DEBIT",
            amount=used_total,
            balance_before=before,
            balance_after=after,
            reference=reference,
            narration=narration or "Wallet used to settle merry dues.",
        )

    return {
        "used_amount": q2(used_total),
        "remaining_wallet_balance": q2(wallet.balance or Decimal("0.00")),
        "allocations": allocation_rows,
    }


@transaction.atomic
def apply_mpesa_contribution_by_user_reference(
    *,
    user_id: int,
    amount: Decimal,
    mpesa_tx=None,
    reference: str = "",
):
    uid = parse_int(user_id, "user_id", min_value=1)
    total_amount = parse_decimal(amount, "amount")
    if total_amount <= 0:
        raise BadState("amount must be > 0.")

    user = User.objects.filter(id=uid).first()
    if not user:
        raise NotFound("User not found.")

    members = _ensure_current_dues_for_user_memberships(uid)
    if not members:
        raise NotFound("User has no active merry memberships.")

    payer_phone = ""
    mpesa_receipt = None
    paid_at = timezone.now()

    if mpesa_tx is not None:
        tx_phone = (getattr(mpesa_tx, "phone", "") or "").strip()
        matched_user_phone = (getattr(mpesa_tx, "matched_user_phone", "") or "").strip()
        user_phone = (getattr(user, "phone", "") or "").strip()

        if tx_phone.startswith("254") and len(tx_phone) <= 15:
            payer_phone = tx_phone
        elif matched_user_phone.startswith("254") and len(matched_user_phone) <= 15:
            payer_phone = matched_user_phone
        elif user_phone.startswith("254") and len(user_phone) <= 15:
            payer_phone = user_phone

        mpesa_receipt = getattr(mpesa_tx, "mpesa_receipt_number", None)
        tx_date = getattr(mpesa_tx, "transaction_date", None)
        if tx_date:
            paid_at = tx_date

    remaining = q2(total_amount)
    if remaining <= 0:
        raise BadState("amount must be > 0.")

    plan: List[Tuple[MerryMember, MerryContributionDue, Decimal]] = []

    current_due_rows: List[Tuple[MerryMember, MerryContributionDue]] = []
    for member in members:
        current_pk = get_current_period_key(member.merry)
        dues = _collect_open_dues_for_member_period(member, current_pk)
        for due in dues:
            current_due_rows.append((member, due))

    current_due_rows.sort(
        key=lambda x: (
            x[1].due_date or timezone.localdate(),
            x[1].period_key,
            x[1].slot_no,
            x[0].merry_id,
            x[1].seat.seat_no,
            x[1].id,
        )
    )

    for member, due, *_ in [(m, d, None) for m, d in current_due_rows]:
        if remaining <= 0:
            break

        need = _outstanding_amount(due)
        if need <= 0:
            continue

        alloc = remaining if remaining < need else need
        if alloc <= 0:
            continue

        plan.append((member, due, alloc))
        remaining = q2(remaining - alloc)

    if not plan and remaining == total_amount:
        raise BadState("No allocatable merry dues were found for this user.")

    grouped: Dict[Tuple[int, str], Dict[str, object]] = {}
    for member, due, alloc in plan:
        key = (member.id, due.period_key)
        if key not in grouped:
            grouped[key] = {
                "member": member,
                "period_key": due.period_key,
                "amount": Decimal("0"),
                "items": [],
            }
        grouped[key]["amount"] = q2(grouped[key]["amount"] + alloc)
        grouped[key]["items"].append((due, alloc))

    created_payments: List[MerryPayment] = []

    for _, bundle in grouped.items():
        member = bundle["member"]
        period_key = bundle["period_key"]
        pay_amount = q2(bundle["amount"])
        items = bundle["items"]

        payment = _create_confirmed_payment_shell(
            member=member,
            total_amount=pay_amount,
            payer_phone=payer_phone,
            period_key=period_key,
            initiated_by=user,
            mpesa_receipt_number=mpesa_receipt,
            paid_at=paid_at,
        )

        for due, alloc in items:
            MerryPaymentAllocation.objects.create(
                payment=payment,
                due=due,
                amount_allocated=q2(alloc),
            )

            due.paid_amount = q2((due.paid_amount or Decimal("0")) + alloc)
            due.recalc_status()
            due.save(update_fields=["paid_amount", "status", "updated_at"])

        created_payments.append(payment)

    wallet_credit_added = Decimal("0.00")
    wallet_balance_after = get_user_merry_wallet_balance(user=user)

    if remaining > 0:
        add_merry_wallet_credit(
            user=user,
            amount=remaining,
            reference=reference,
            narration="Excess manual merry payment saved to wallet.",
            mpesa_receipt_number=mpesa_receipt,
        )
        wallet_credit_added = q2(remaining)
        wallet_balance_after = get_user_merry_wallet_balance(user=user)

    return {
        "payments": created_payments,
        "allocated_amount": q2(total_amount - remaining),
        "wallet_credit_added": q2(wallet_credit_added),
        "wallet_balance_after": q2(wallet_balance_after),
    }


def apply_mpesa_contribution_by_user(
    *,
    user_id: int,
    amount: Decimal,
    mpesa_tx=None,
    reference: str = "",
):
    return apply_mpesa_contribution_by_user_reference(
        user_id=user_id,
        amount=amount,
        mpesa_tx=mpesa_tx,
        reference=reference,
    )


def apply_mpesa_contribution(
    *,
    user,
    amount: Decimal,
    mpesa_tx=None,
    reference: str = "",
):
    if not user or not getattr(user, "id", None):
        raise BadState("Valid user is required for merry contribution allocation.")

    return apply_mpesa_contribution_by_user_reference(
        user_id=user.id,
        amount=amount,
        mpesa_tx=mpesa_tx,
        reference=reference,
    )


# -----------------------------
# Merry lifecycle
# -----------------------------
@transaction.atomic
def create_merry(
    *,
    creator,
    name: str,
    contribution_amount: Decimal,
    cycle_duration_weeks: int = 1,
    payout_order_type: str = "manual",
    next_payout_date=None,
    payout_frequency: str = "WEEKLY",
    payouts_per_period: int = 1,
    is_open: bool = True,
    max_seats: int = 0,
) -> MerryGoRound:
    if not is_admin(creator):
        raise NotAllowed("Admin only.")

    name = (name or "").strip()
    if not name:
        raise BadState("name is required.")

    amount = parse_decimal(contribution_amount, "contribution_amount")
    if amount <= 0:
        raise BadState("contribution_amount must be > 0.")

    cycle_duration_weeks = parse_int(
        cycle_duration_weeks,
        "cycle_duration_weeks",
        min_value=1,
        max_value=520,
    )

    payout_order_type = (payout_order_type or "manual").strip().lower()
    if payout_order_type not in ("manual", "random"):
        raise BadState("payout_order_type must be 'manual' or 'random'.")

    payout_frequency = (payout_frequency or "WEEKLY").upper().strip()
    if payout_frequency not in ("WEEKLY", "MONTHLY"):
        raise BadState("payout_frequency must be 'WEEKLY' or 'MONTHLY'.")

    payouts_per_period = parse_int(
        payouts_per_period,
        "payouts_per_period",
        min_value=1,
        max_value=14,
    )

    is_open = parse_bool(is_open, default=True)
    max_seats = parse_int(max_seats or 0, "max_seats", min_value=0)

    return MerryGoRound.objects.create(
        name=name,
        contribution_amount=amount,
        cycle_duration_weeks=cycle_duration_weeks,
        payout_order_type=payout_order_type,
        next_payout_date=next_payout_date or None,
        created_by=creator,
        payout_frequency=payout_frequency,
        payouts_per_period=payouts_per_period,
        is_open=is_open,
        max_seats=max_seats,
    )


# -----------------------------
# Slot config
# -----------------------------
@transaction.atomic
def set_slot_config_bulk(*, admin_user, merry_id: int, items: List[dict]) -> List[MerrySlotConfig]:
    if not is_admin(admin_user):
        raise NotAllowed("Admin only.")

    merry = get_merry(merry_id)

    if not isinstance(items, list) or not items:
        raise BadState("items must be a non-empty list.")

    seen = set()
    for it in items:
        slot_no = parse_int(it.get("slot_no"), "slot_no", min_value=1)
        weekday = parse_int(it.get("weekday"), "weekday", min_value=0, max_value=6)

        validate_slot(merry, slot_no)

        if slot_no in seen:
            raise BadState("Duplicate slot_no in payload.")
        seen.add(slot_no)

    out: List[MerrySlotConfig] = []
    for it in items:
        slot_no = int(it["slot_no"])
        weekday = int(it["weekday"])
        obj, _ = MerrySlotConfig.objects.get_or_create(
            merry=merry,
            slot_no=slot_no,
            defaults={"weekday": weekday},
        )
        if obj.weekday != weekday:
            obj.weekday = weekday
            obj.full_clean()
            obj.save(update_fields=["weekday"])
        out.append(obj)

    return out


# -----------------------------
# Join requests
# -----------------------------
@transaction.atomic
def request_to_join_merry(
    *,
    user,
    merry_id: int,
    note: str = "",
    requested_seats: int = 1,
) -> MerryJoinRequest:
    merry = MerryGoRound.objects.select_for_update().filter(id=merry_id).first()
    if not merry:
        raise NotFound("Merry not found.")

    if MerryMember.objects.filter(merry=merry, user=user, is_active=True).exists():
        raise Conflict("You are already a member of this merry.")

    note = (note or "").strip()[:255]
    requested_seats = parse_int(
        requested_seats,
        "requested_seats",
        min_value=1,
        max_value=50,
    )

    if hasattr(merry, "can_accept_join_request"):
        ok, reason = merry.can_accept_join_request(requested_seats)
        if not ok:
            raise BadState(reason)

    existing_pending = (
        MerryJoinRequest.objects.select_for_update()
        .filter(merry=merry, user=user, status="PENDING")
        .first()
    )
    if existing_pending:
        changed = False
        if existing_pending.note != note:
            existing_pending.note = note
            changed = True
        if existing_pending.requested_seats != requested_seats:
            existing_pending.requested_seats = requested_seats
            changed = True
        if changed:
            existing_pending.full_clean()
            existing_pending.save(update_fields=["note", "requested_seats"])

        _notify_join_request_submitted(
            user=user,
            merry=merry,
            requested_seats=existing_pending.requested_seats,
        )
        return existing_pending

    existing_latest = (
        MerryJoinRequest.objects.select_for_update()
        .filter(merry=merry, user=user)
        .order_by("-created_at", "-id")
        .first()
    )

    if existing_latest:
        existing_latest.status = "PENDING"
        existing_latest.note = note
        existing_latest.requested_seats = requested_seats
        existing_latest.reviewed_by = None
        existing_latest.reviewed_at = None
        existing_latest.created_at = timezone.now()
        existing_latest.full_clean()
        existing_latest.save(
            update_fields=[
                "status",
                "note",
                "requested_seats",
                "reviewed_by",
                "reviewed_at",
                "created_at",
            ]
        )

        _notify_join_request_submitted(
            user=user,
            merry=merry,
            requested_seats=existing_latest.requested_seats,
        )
        return existing_latest

    jr = MerryJoinRequest(
        merry=merry,
        user=user,
        status="PENDING",
        note=note,
        requested_seats=requested_seats,
    )
    jr.full_clean()
    jr.save()

    _notify_join_request_submitted(
        user=user,
        merry=merry,
        requested_seats=jr.requested_seats,
    )
    return jr


@transaction.atomic
def cancel_join_request(*, user, request_id: int) -> MerryJoinRequest:
    jr = MerryJoinRequest.objects.select_for_update().filter(id=request_id).first()
    if not jr:
        raise NotFound("Join request not found.")
    if jr.user_id != user.id:
        raise NotAllowed("You can only cancel your own join request.")
    if jr.status != "PENDING":
        raise BadState("Only PENDING requests can be cancelled.")

    jr.status = "CANCELLED"
    jr.save(update_fields=["status"])

    _notify_join_request_cancelled(
        user=user,
        merry=jr.merry,
    )
    return jr


@transaction.atomic
def admin_approve_join_request(
    *,
    admin_user,
    request_id: int,
    assigned_seat_numbers: Optional[List[int]] = None,
) -> Tuple[MerryMember, List[MerrySeat]]:
    if not is_admin(admin_user):
        raise NotAllowed("Admin only.")

    jr = (
        MerryJoinRequest.objects.select_for_update()
        .select_related("merry", "user")
        .filter(id=request_id)
        .first()
    )
    if not jr:
        raise NotFound("Join request not found.")
    if jr.status != "PENDING":
        raise BadState("Only PENDING requests can be approved.")

    merry = MerryGoRound.objects.select_for_update().filter(id=jr.merry_id).first()
    if not merry:
        raise NotFound("Merry not found.")

    user = jr.user
    seats_requested = parse_int(
        jr.requested_seats or 1,
        "requested_seats",
        min_value=1,
        max_value=50,
    )

    if hasattr(merry, "can_accept_join_request"):
        ok, reason = merry.can_accept_join_request(seats_requested)
        if not ok:
            raise BadState(reason)

    member, _ = MerryMember.objects.get_or_create(
        merry=merry,
        user=user,
        defaults={"joined_at": timezone.now(), "is_active": True},
    )

    if not member.is_active:
        member.is_active = True
        if not member.joined_at:
            member.joined_at = timezone.now()
            member.save(update_fields=["is_active", "joined_at"])
        else:
            member.save(update_fields=["is_active"])

    if assigned_seat_numbers is None:
        seat_numbers = get_next_available_seat_numbers(merry, seats_requested)
    else:
        seat_numbers = _validate_manual_seat_numbers_for_merry(
            merry=merry,
            seat_numbers=assigned_seat_numbers,
            expected_count=seats_requested,
        )

    seats_created: List[MerrySeat] = []
    try:
        for seat_no in seat_numbers:
            payout_position: Optional[int] = None
            if merry.payout_order_type == "manual":
                payout_position = next_payout_position_for_seat(merry)

            seat = MerrySeat.objects.create(
                merry=merry,
                member=member,
                seat_no=seat_no,
                payout_position=payout_position,
                is_active=True,
                created_at=timezone.now(),
            )
            seats_created.append(seat)
    except IntegrityError:
        raise Conflict("Failed to create seats (duplicate seat_no or payout_position). Try again.")

    jr.status = "APPROVED"
    jr.reviewed_by = admin_user
    jr.reviewed_at = timezone.now()
    jr.save(update_fields=["status", "reviewed_by", "reviewed_at"])

    _notify_join_request_approved(
        user=user,
        admin_user=admin_user,
        merry=merry,
        seats_created=seats_created,
    )

    return member, seats_created


@transaction.atomic
def admin_reject_join_request(*, admin_user, request_id: int, note: str = "") -> MerryJoinRequest:
    if not is_admin(admin_user):
        raise NotAllowed("Admin only.")

    jr = (
        MerryJoinRequest.objects.select_for_update()
        .select_related("merry", "user")
        .filter(id=request_id)
        .first()
    )
    if not jr:
        raise NotFound("Join request not found.")
    if jr.status != "PENDING":
        raise BadState("Only PENDING requests can be rejected.")

    rejection_note = (note or "").strip()[:255]

    jr.status = "REJECTED"
    jr.reviewed_by = admin_user
    jr.reviewed_at = timezone.now()
    if rejection_note:
        jr.note = rejection_note
    jr.save(update_fields=["status", "reviewed_by", "reviewed_at", "note"])

    _notify_join_request_rejected(
        user=jr.user,
        admin_user=admin_user,
        merry=jr.merry,
        note=rejection_note,
    )
    return jr


# -----------------------------
# Professional seat management
# -----------------------------
@transaction.atomic
def add_seats_to_existing_member(
    *,
    admin_user,
    member_id: int,
    assigned_seat_numbers: List[int],
) -> List[MerrySeat]:
    if not is_admin(admin_user):
        raise NotAllowed("Admin only.")

    member = get_member_by_id(member_id, lock=True)
    if not member.is_active:
        raise BadState("Cannot add seats to an inactive member.")

    merry = MerryGoRound.objects.select_for_update().get(id=member.merry_id)

    seat_numbers = _validate_manual_seat_numbers_for_merry(
        merry=merry,
        seat_numbers=assigned_seat_numbers,
    )

    ok, reason = merry.can_accept_join_request(len(seat_numbers))
    if not ok:
        raise BadState(reason)

    created: List[MerrySeat] = []
    try:
        for seat_no in seat_numbers:
            payout_position: Optional[int] = None
            if merry.payout_order_type == "manual":
                payout_position = next_payout_position_for_seat(merry)

            seat = MerrySeat.objects.create(
                merry=merry,
                member=member,
                seat_no=seat_no,
                payout_position=payout_position,
                is_active=True,
                created_at=timezone.now(),
            )
            created.append(seat)
    except IntegrityError:
        raise Conflict("Failed to add seat(s). Duplicate seat_no or payout_position detected.")

    return created


@transaction.atomic
def reassign_existing_clean_seat(
    *,
    admin_user,
    seat_id: int,
    new_member_id: int,
) -> MerrySeat:
    if not is_admin(admin_user):
        raise NotAllowed("Admin only.")

    seat = (
        MerrySeat.objects.select_for_update()
        .select_related("merry", "member", "member__user")
        .filter(id=seat_id)
        .first()
    )
    if not seat:
        raise NotFound("Seat not found.")

    if not seat.is_active:
        raise BadState("Only active seats can be reassigned.")

    new_member = get_member_by_id(new_member_id, lock=True)
    if not new_member.is_active:
        raise BadState("New member must be active.")

    if seat.member_id == new_member.id:
        raise BadState("Seat already belongs to this member.")

    if new_member.merry_id != seat.merry_id:
        raise BadState("Seat can only be reassigned to a member in the same merry.")

    if _seat_has_financial_history(seat):
        raise BadState(
            "This seat already has dues or payout history and cannot be reassigned directly. "
            "Use a new seat assignment for the target member instead."
        )

    seat.member = new_member
    seat.full_clean()
    seat.save(update_fields=["member"])
    return seat


# -----------------------------
# Dues scheduling
# -----------------------------
@transaction.atomic
def ensure_dues_for_period(*, admin_user, merry_id: int, period_key: Optional[str] = None) -> int:
    if not is_admin(admin_user):
        raise NotAllowed("Admin only.")
    merry = get_merry(merry_id)
    pk = (period_key or "").strip() or get_current_period_key(merry)
    count = merry.ensure_dues_for_period(period_key=pk)

    memberships = MerryMember.objects.filter(merry=merry, is_active=True).select_related("user")
    for membership in memberships:
        apply_merry_wallet_to_user_open_dues(
            user_id=membership.user_id,
            reference=f"AUTO-{pk}",
            narration=f"Automatic wallet application after due generation for {pk}.",
        )

    return count


@transaction.atomic
def ensure_dues_for_member_period(merry: MerryGoRound, member: MerryMember, period_key: str) -> None:
    due_amt = merry.contribution_amount or Decimal("0")
    slots = payouts_per_period(merry)

    active_seats = list(
        MerrySeat.objects.select_for_update()
        .filter(merry=merry, member=member, is_active=True)
        .order_by("seat_no", "id")
    )

    for seat in active_seats:
        for slot_no in range(1, slots + 1):
            due_date = merry.get_slot_due_date(period_key, slot_no) if hasattr(merry, "get_slot_due_date") else None

            due, created = MerryContributionDue.objects.get_or_create(
                merry=merry,
                seat=seat,
                period_key=period_key,
                slot_no=slot_no,
                defaults={
                    "due_amount": due_amt,
                    "paid_amount": Decimal("0"),
                    "status": "PENDING",
                    "due_date": due_date,
                    "is_advance_payable": True,
                },
            )

            changed = False

            if not created and due.due_date != due_date:
                due.due_date = due_date
                changed = True

            if not created and getattr(due, "is_advance_payable", True) is not True:
                due.is_advance_payable = True
                changed = True

            if changed:
                due.recalc_status()
                due.save(update_fields=["due_date", "is_advance_payable", "status", "updated_at"])

    apply_merry_wallet_to_user_open_dues(
        user_id=member.user_id,
        reference=f"AUTO-{period_key}",
        narration=f"Automatic wallet application for generated dues in {period_key}.",
    )


# -----------------------------
# Payments
# -----------------------------
@transaction.atomic
def create_payment_intent(*, user, merry_id: int, amount: Decimal, payer_phone: str) -> MerryPayment:
    merry = get_merry(merry_id)
    member = get_active_member(merry, user)

    amt = parse_decimal(amount, "amount")
    if amt <= 0:
        raise BadState("amount must be > 0.")

    payer_phone = (payer_phone or "").strip()
    if not payer_phone:
        raise BadState("payer_phone is required.")

    period_key = get_current_period_key(merry)
    ensure_dues_for_member_period(merry, member, period_key)

    return MerryPayment.objects.create(
        merry=merry,
        beneficiary_member=member,
        initiated_by=user,
        payer_phone=payer_phone,
        period_key=period_key,
        amount=amt,
        status="PENDING",
    )


@transaction.atomic
def confirm_payment_and_allocate(
    *,
    payment_id: int,
    mpesa_receipt_number: Optional[str] = None,
    paid_at=None,
) -> MerryPayment:
    p = (
        MerryPayment.objects.select_for_update()
        .select_related("merry", "beneficiary_member", "beneficiary_member__user")
        .filter(id=payment_id)
        .first()
    )
    if not p:
        raise NotFound("Payment not found.")

    if mpesa_receipt_number:
        receipt = (mpesa_receipt_number or "").strip()[:64]
        exists_elsewhere = (
            MerryPayment.objects.exclude(id=p.id)
            .filter(mpesa_receipt_number=receipt)
            .exists()
        )
        if exists_elsewhere:
            raise Conflict("This M-Pesa receipt number is already used.")
    else:
        receipt = None

    if p.status == "CONFIRMED":
        return p

    if p.status in ("CANCELLED",):
        raise BadState(f"Cannot confirm payment from status={p.status}")

    p.status = "CONFIRMED"
    p.paid_at = paid_at or timezone.now()
    if receipt:
        p.mpesa_receipt_number = receipt

    if receipt:
        p.save(update_fields=["status", "paid_at", "mpesa_receipt_number"])
    else:
        p.save(update_fields=["status", "paid_at"])

    allocate_payment(payment_id=p.id)

    # Preserve existing logic and only keep next payout date synchronized.
    maybe_update_next_payout_date(merry=p.merry)
    return p


@transaction.atomic
def mark_payment_failed(*, payment_id: int) -> MerryPayment:
    p = MerryPayment.objects.select_for_update().filter(id=payment_id).first()
    if not p:
        raise NotFound("Payment not found.")
    if p.status == "CONFIRMED":
        raise BadState("Cannot fail a CONFIRMED payment.")
    if p.status == "CANCELLED":
        raise BadState("Cannot fail a CANCELLED payment.")

    p.status = "FAILED"
    p.save(update_fields=["status"])
    return p


@transaction.atomic
def allocate_payment(*, payment_id: int) -> MerryPayment:
    payment = (
        MerryPayment.objects.select_for_update()
        .select_related("merry", "beneficiary_member", "beneficiary_member__user")
        .get(id=payment_id)
    )

    if payment.status != "CONFIRMED":
        raise BadState("Payment must be CONFIRMED before allocation.")

    merry = payment.merry
    member = payment.beneficiary_member

    if member.merry_id != merry.id:
        raise BadState("Payment beneficiary does not belong to this merry.")

    if not member.is_active:
        raise BadState("Cannot allocate payment for an inactive member.")

    remaining = payment.amount or Decimal("0")
    if remaining <= 0:
        raise BadState("Payment amount must be > 0.")

    period_key = (payment.period_key or "").strip() or get_current_period_key(merry)

    safety = 0
    while remaining > 0:
        safety += 1
        if safety > 2000:
            raise BadState("Allocation safety limit reached.")

        ensure_dues_for_member_period(merry, member, period_key)

        dues = list(
            MerryContributionDue.objects.select_for_update()
            .filter(
                merry=merry,
                seat__member=member,
                seat__is_active=True,
                period_key=period_key,
                status__in=["PENDING", "PARTIAL", "OVERDUE"],
            )
            .select_related("seat")
            .order_by("due_date", "slot_no", "seat__seat_no", "id")
        )

        any_needed = False
        for due in dues:
            need = _outstanding_amount(due)
            if need <= 0:
                continue

            any_needed = True
            alloc = remaining if remaining < need else need
            if alloc <= 0:
                continue

            allocation, _ = MerryPaymentAllocation.objects.get_or_create(
                payment=payment,
                due=due,
                defaults={"amount_allocated": Decimal("0")},
            )
            allocation.amount_allocated = q2((allocation.amount_allocated or Decimal("0")) + alloc)
            allocation.full_clean()
            allocation.save(update_fields=["amount_allocated"])

            due.paid_amount = q2((due.paid_amount or Decimal("0")) + alloc)
            due.recalc_status()
            due.save(update_fields=["paid_amount", "status", "updated_at"])

            remaining = q2(remaining - alloc)
            if remaining <= 0:
                break

        if remaining <= 0:
            break

        period_key = _next_period_key(merry, period_key)

        if not any_needed:
            continue

    if payment.beneficiary_member and payment.beneficiary_member.user_id:
        apply_merry_wallet_to_user_open_dues(
            user_id=payment.beneficiary_member.user_id,
            reference=f"PAY-{payment.id}",
            narration=f"Automatic wallet application after payment allocation #{payment.id}.",
        )

    return payment


# -----------------------------
# Payout cycle helpers
# -----------------------------
def _ordered_active_payout_seats(merry: MerryGoRound) -> List[MerrySeat]:
    seats = list(
        MerrySeat.objects.filter(merry=merry, is_active=True)
        .select_related("member", "member__user")
        .order_by("payout_position", "seat_no", "id")
    )
    if not seats:
        raise BadState("This merry has no active seats.")
    return seats


def _cycle_length(merry: MerryGoRound) -> int:
    return len(_ordered_active_payout_seats(merry))


def _paid_payout_count(merry: MerryGoRound) -> int:
    return MerryPayout.objects.filter(merry=merry, status="PAID").count()


def _current_cycle_number(merry: MerryGoRound) -> int:
    seats_count = _cycle_length(merry)
    if seats_count <= 0:
        return 0
    paid_count = _paid_payout_count(merry)
    return (paid_count // seats_count) + 1


def _is_cycle_complete(merry: MerryGoRound) -> bool:
    seats_count = _cycle_length(merry)
    if seats_count <= 0:
        return False
    paid_count = _paid_payout_count(merry)
    return paid_count > 0 and (paid_count % seats_count == 0)


def _next_turn_index(merry: MerryGoRound) -> int:
    seats_count = _cycle_length(merry)
    if seats_count <= 0:
        raise BadState("This merry has no active seats.")
    paid_count = _paid_payout_count(merry)
    return paid_count % seats_count


def _next_turn_seat(merry: MerryGoRound) -> MerrySeat:
    seats = _ordered_active_payout_seats(merry)
    return seats[_next_turn_index(merry)]


def _find_next_open_period_slot_for_seat(
    *,
    merry: MerryGoRound,
    seat: MerrySeat,
    start_period_key: Optional[str] = None,
) -> Tuple[str, int]:
    period_key = (start_period_key or "").strip() or get_current_period_key(merry)
    limit = payouts_per_period(merry)

    safety = 0
    while True:
        safety += 1
        if safety > 500:
            raise BadState("Could not find a valid future payout period/slot.")

        used_slots = set(
            MerryPayout.objects.filter(merry=merry, period_key=period_key)
            .values_list("slot_no", flat=True)
        )

        seat_has_payout_in_period = MerryPayout.objects.filter(
            merry=merry,
            seat=seat,
            period_key=period_key,
        ).exists()

        if not seat_has_payout_in_period:
            for slot_no in range(1, limit + 1):
                if slot_no not in used_slots:
                    return period_key, slot_no

        period_key = _next_period_key(merry, period_key)


def _preview_next_payout_meta(merry: MerryGoRound) -> Dict[str, Any]:
    seat = _next_turn_seat(merry)
    period_key, slot_no = _find_next_open_period_slot_for_seat(merry=merry, seat=seat)
    due_date = merry.get_slot_due_date(period_key, slot_no) if hasattr(merry, "get_slot_due_date") else None

    return {
        "seat": seat,
        "period_key": period_key,
        "slot_no": slot_no,
        "due_date": due_date,
        "cycle_number": _current_cycle_number(merry),
        "cycle_complete": _is_cycle_complete(merry),
    }


def maybe_update_next_payout_date(*, merry: MerryGoRound) -> None:
    try:
        preview = _preview_next_payout_meta(merry)
    except Exception:
        next_date = None
    else:
        next_date = preview.get("due_date")

    if merry.next_payout_date != next_date:
        merry.next_payout_date = next_date
        merry.save(update_fields=["next_payout_date"])


# -----------------------------
# Payout readiness helpers
# -----------------------------
def _slot_due_total(*, merry: MerryGoRound, period_key: str, slot_no: int) -> Decimal:
    total = (
        MerryContributionDue.objects.filter(
            merry=merry,
            period_key=period_key,
            slot_no=slot_no,
        )
        .aggregate(s=Sum("due_amount"))
        .get("s")
        or Decimal("0.00")
    )
    return q2(total)


def _slot_paid_total(*, merry: MerryGoRound, period_key: str, slot_no: int) -> Decimal:
    total = (
        MerryContributionDue.objects.filter(
            merry=merry,
            period_key=period_key,
            slot_no=slot_no,
        )
        .aggregate(s=Sum("paid_amount"))
        .get("s")
        or Decimal("0.00")
    )
    return q2(total)


def _slot_outstanding_total(*, merry: MerryGoRound, period_key: str, slot_no: int) -> Decimal:
    due_total = _slot_due_total(merry=merry, period_key=period_key, slot_no=slot_no)
    paid_total = _slot_paid_total(merry=merry, period_key=period_key, slot_no=slot_no)
    outstanding = q2(due_total - paid_total)
    return outstanding if outstanding > 0 else Decimal("0.00")


def _slot_is_ready_for_payout(*, merry: MerryGoRound, period_key: str, slot_no: int) -> bool:
    due_total = _slot_due_total(merry=merry, period_key=period_key, slot_no=slot_no)
    paid_total = _slot_paid_total(merry=merry, period_key=period_key, slot_no=slot_no)
    if due_total <= 0:
        return False
    return paid_total >= due_total


def _slot_has_existing_payout(*, merry: MerryGoRound, period_key: str, slot_no: int) -> bool:
    return MerryPayout.objects.filter(
        merry=merry,
        period_key=period_key,
        slot_no=slot_no,
    ).exists()


def _slot_member_status_rows(*, merry: MerryGoRound, period_key: str, slot_no: int) -> List[Dict[str, Any]]:
    dues = list(
        MerryContributionDue.objects.filter(
            merry=merry,
            period_key=period_key,
            slot_no=slot_no,
        )
        .select_related("seat", "seat__member", "seat__member__user")
        .order_by("seat__seat_no", "id")
    )

    rows: List[Dict[str, Any]] = []
    for due in dues:
        user = due.seat.member.user
        outstanding = q2((due.due_amount or Decimal("0.00")) - (due.paid_amount or Decimal("0.00")))
        if outstanding < 0:
            outstanding = Decimal("0.00")

        rows.append({
            "due_id": due.id,
            "seat_id": due.seat_id,
            "seat_no": due.seat.seat_no,
            "member_id": due.seat.member_id,
            "user_id": user.id,
            "username": getattr(user, "username", None),
            "phone": getattr(user, "phone", None),
            "due_amount": q2(due.due_amount or Decimal("0.00")),
            "paid_amount": q2(due.paid_amount or Decimal("0.00")),
            "outstanding": outstanding,
            "status": due.status,
            "due_date": due.due_date,
        })

    return rows


@transaction.atomic
def get_payout_readiness_status(
    *,
    merry_id: int,
    period_key: Optional[str] = None,
    slot_no: Optional[int] = None,
) -> Dict[str, Any]:
    merry = MerryGoRound.objects.select_for_update().filter(id=merry_id).first()
    if not merry:
        raise NotFound("Merry not found.")

    resolved_period_key = (period_key or "").strip() or get_current_period_key(merry)

    if slot_no is None:
        try:
            preview = _preview_next_payout_meta(merry)
            resolved_slot_no = preview["slot_no"]
            next_turn = {
                "seat_id": preview["seat"].id,
                "seat_no": preview["seat"].seat_no,
                "member_id": preview["seat"].member_id,
                "user_id": preview["seat"].member.user_id,
                "username": getattr(preview["seat"].member.user, "username", None),
                "payout_position": preview["seat"].payout_position,
                "cycle_number": preview["cycle_number"],
                "cycle_complete": preview["cycle_complete"],
            }
        except Exception:
            resolved_slot_no = 1
            next_turn = None
    else:
        resolved_slot_no = parse_int(slot_no, "slot_no", min_value=1)
        validate_slot(merry, resolved_slot_no)
        next_turn = None
        try:
            preview = _preview_next_payout_meta(merry)
            next_turn = {
                "seat_id": preview["seat"].id,
                "seat_no": preview["seat"].seat_no,
                "member_id": preview["seat"].member_id,
                "user_id": preview["seat"].member.user_id,
                "username": getattr(preview["seat"].member.user, "username", None),
                "payout_position": preview["seat"].payout_position,
                "cycle_number": preview["cycle_number"],
                "cycle_complete": preview["cycle_complete"],
            }
        except Exception:
            next_turn = None

    due_total = _slot_due_total(merry=merry, period_key=resolved_period_key, slot_no=resolved_slot_no)
    paid_total = _slot_paid_total(merry=merry, period_key=resolved_period_key, slot_no=resolved_slot_no)
    outstanding_total = _slot_outstanding_total(merry=merry, period_key=resolved_period_key, slot_no=resolved_slot_no)
    ready_for_payout = _slot_is_ready_for_payout(merry=merry, period_key=resolved_period_key, slot_no=resolved_slot_no)
    payout_exists = _slot_has_existing_payout(merry=merry, period_key=resolved_period_key, slot_no=resolved_slot_no)

    try:
        period_meta = get_period_date_range(merry=merry, period_key=resolved_period_key)
    except Exception:
        period_meta = {
            "period_key": resolved_period_key,
            "label": resolved_period_key,
            "start_date": None,
            "end_date": None,
            "frequency": merry.payout_frequency,
        }

    rows = _slot_member_status_rows(merry=merry, period_key=resolved_period_key, slot_no=resolved_slot_no)

    return {
        "merry_id": merry.id,
        "merry_name": merry.name,
        "period_key": resolved_period_key,
        "period_label": period_meta["label"],
        "period_start_date": period_meta["start_date"],
        "period_end_date": period_meta["end_date"],
        "slot_no": resolved_slot_no,
        "due_total": due_total,
        "paid_total": paid_total,
        "outstanding_total": outstanding_total,
        "ready_for_payout": ready_for_payout,
        "payout_already_exists": payout_exists,
        "can_admin_create_payout": bool(ready_for_payout and not payout_exists),
        "next_turn": next_turn,
        "rows": rows,
    }


# -----------------------------
# Payouts
# -----------------------------
@transaction.atomic
def compute_payout_amount_for_slot(*, merry_id: int, period_key: str, slot_no: int) -> Decimal:
    merry = get_merry(merry_id)
    validate_slot(merry, slot_no)

    pk = (period_key or "").strip()
    if not pk:
        raise BadState("period_key is required.")

    total = (
        MerryContributionDue.objects.filter(merry=merry, period_key=pk, slot_no=slot_no)
        .aggregate(s=Sum("paid_amount"))
        .get("s")
        or Decimal("0")
    )
    return q2(total)


@transaction.atomic
def get_next_payout_turn(*, merry_id: int) -> Dict[str, Any]:
    merry = MerryGoRound.objects.select_for_update().filter(id=merry_id).first()
    if not merry:
        raise NotFound("Merry not found.")

    preview = _preview_next_payout_meta(merry)
    seat = preview["seat"]
    period_key = preview["period_key"]
    slot_no = preview["slot_no"]

    period_meta = get_period_date_range(merry=merry, period_key=period_key)

    return {
        "merry_id": merry.id,
        "merry_name": merry.name,
        "seat_id": seat.id,
        "seat_no": seat.seat_no,
        "member_id": seat.member_id,
        "user_id": seat.member.user_id,
        "username": getattr(seat.member.user, "username", None),
        "payout_position": seat.payout_position,
        "period_key": period_key,
        "period_label": period_meta["label"],
        "period_start_date": period_meta["start_date"],
        "period_end_date": period_meta["end_date"],
        "slot_no": slot_no,
        "due_date": preview["due_date"],
        "cycle_number": preview["cycle_number"],
        "cycle_complete": preview["cycle_complete"],
        "expected_amount": q2(
            merry.total_pool_per_slot()
            if hasattr(merry, "total_pool_per_slot")
            else Decimal("0.00")
        ),
    }


@transaction.atomic
def create_payout_record(
    *,
    admin_user,
    merry_id: int,
    seat_id: Optional[int] = None,
    amount: Optional[Decimal] = None,
    period_key: Optional[str] = None,
    slot_no: Optional[int] = None,
    notes: str = "",
    auto_select_next_turn: bool = False,
) -> MerryPayout:
    if not is_admin(admin_user):
        raise NotAllowed("Admin only.")

    merry = MerryGoRound.objects.select_for_update().filter(id=merry_id).first()
    if not merry:
        raise NotFound("Merry not found.")

    if auto_select_next_turn or seat_id is None:
        preview = _preview_next_payout_meta(merry)
        seat = preview["seat"]
        pk = preview["period_key"]
        resolved_slot_no = preview["slot_no"]
    else:
        seat = (
            MerrySeat.objects.select_for_update()
            .filter(id=seat_id, merry=merry, is_active=True)
            .select_related("member", "member__user")
            .first()
        )
        if not seat:
            raise NotFound("Seat not found in this merry.")

        pk = (period_key or "").strip() or get_current_period_key(merry)

        if slot_no is None:
            resolved_slot_no = get_next_available_slot(merry, pk)
        else:
            resolved_slot_no = parse_int(slot_no, "slot_no", min_value=1)
            validate_slot(merry, resolved_slot_no)
            if MerryPayout.objects.filter(merry=merry, period_key=pk, slot_no=resolved_slot_no).exists():
                raise Conflict(f"Slot {resolved_slot_no} is already used for period {pk}.")

        if MerryPayout.objects.filter(merry=merry, seat=seat, period_key=pk).exists():
            raise Conflict("This seat already has a payout record in this period.")

    if amount is None or amount == "":
        amt = q2(
            merry.total_pool_per_slot()
            if hasattr(merry, "total_pool_per_slot")
            else Decimal("0.00")
        )
    else:
        amt = parse_decimal(amount, "amount")

    if amt <= 0:
        raise BadState("amount must be > 0.")

    try:
        payout = MerryPayout.objects.create(
            merry=merry,
            seat=seat,
            period_key=pk,
            slot_no=resolved_slot_no,
            amount=amt,
            status="SCHEDULED",
            notes=(notes or "").strip()[:255],
        )
    except IntegrityError:
        raise Conflict("Failed to create payout (duplicate slot or seat payout). Try again.")

    maybe_update_next_payout_date(merry=merry)
    return payout


@transaction.atomic
def create_next_cycle_payout_record(
    *,
    admin_user,
    merry_id: int,
    notes: str = "",
) -> MerryPayout:
    return create_payout_record(
        admin_user=admin_user,
        merry_id=merry_id,
        seat_id=None,
        amount=None,
        period_key=None,
        slot_no=None,
        notes=notes,
        auto_select_next_turn=True,
    )


@transaction.atomic
def mark_payout_paid(*, payout_id: int, paid_at=None) -> MerryPayout:
    p = (
        MerryPayout.objects.select_for_update()
        .select_related("merry", "seat", "seat__member", "seat__member__user")
        .filter(id=payout_id)
        .first()
    )
    if not p:
        raise NotFound("Payout not found.")

    if p.status == "PAID":
        return p

    if p.status == "CANCELLED":
        raise BadState(f"Cannot mark PAID from status={p.status}")

    p.status = "PAID"
    p.paid_at = paid_at or timezone.now()
    p.save(update_fields=["status", "paid_at"])

    offset = apply_merry_payout_to_active_loan(payout=p)

    member_user = getattr(getattr(getattr(p, "seat", None), "member", None), "user", None)
    if member_user and getattr(member_user, "id", None):
        applied = q2(offset.get("applied_to_loan", Decimal("0.00")))
        remaining = q2(offset.get("remaining_amount", p.amount))
        if applied > 0:
            loan_ids = offset.get("loan_ids", [])
            loan_text = ", ".join(str(x) for x in loan_ids) if loan_ids else "loan"
            _create_notification(
                user=member_user,
                title="Merry payout applied to loan",
                message=(
                    f"Your merry payout of {q2(p.amount)} was processed. "
                    f"{applied} was used to repay {loan_text}, and {remaining} remains after loan deduction."
                ),
                notification_type="INFO",
                merry_id=p.merry_id,
                loan_id=loan_ids[0] if loan_ids else None,
                action_url=_safe_frontend_merry_detail_url(p.merry_id),
            )

    maybe_update_next_payout_date(merry=p.merry)
    return p


# -----------------------------
# Read helpers
# -----------------------------
def list_my_payments(*, user, limit: int = 200):
    limit = parse_int(limit, "limit", min_value=1, max_value=1000)
    return (
        MerryPayment.objects.filter(beneficiary_member__user=user)
        .select_related("merry", "beneficiary_member", "beneficiary_member__user")
        .order_by("-created_at")[:limit]
    )


def list_dues_for_member(*, user, merry_id: int, period_key: Optional[str] = None):
    merry = get_merry(merry_id)
    member = get_active_member(merry, user)
    pk = (period_key or "").strip() or get_current_period_key(merry)

    with transaction.atomic():
        ensure_dues_for_member_period(merry, member, pk)

    return (
        MerryContributionDue.objects.filter(
            merry=merry,
            seat__member=member,
            seat__is_active=True,
            period_key=pk,
        )
        .select_related("seat")
        .order_by("due_date", "slot_no", "seat__seat_no", "id")
    )


# -----------------------------
# Dashboard / summary helpers
# -----------------------------
def get_user_merry_due_summary(*, user) -> Dict[str, Any]:
    if not user or not getattr(user, "id", None):
        raise BadState("Valid user is required.")

    today = timezone.localdate()

    memberships = (
        MerryMember.objects
        .filter(user=user, is_active=True)
        .select_related("merry")
        .prefetch_related("seats")
        .order_by("merry__name", "id")
    )

    total_seats = MerrySeat.objects.filter(
        member__user=user,
        member__is_active=True,
        is_active=True,
    ).count()

    summary_items: List[Dict[str, Any]] = []

    grand_overdue = Decimal("0")
    grand_current = Decimal("0")
    grand_next = Decimal("0")

    for membership in memberships:
        merry = membership.merry
        current_pk = get_current_period_key(merry)
        ensure_dues_for_member_period(merry, membership, current_pk)

        seats = membership.seats.filter(is_active=True).order_by("seat_no", "id")
        seat_numbers = list(seats.values_list("seat_no", flat=True))
        seat_ids = list(seats.values_list("id", flat=True))

        dues = (
            MerryContributionDue.objects
            .filter(
                merry=merry,
                seat_id__in=seat_ids,
            )
            .exclude(status__in=["PAID", "CANCELLED"])
            .select_related("seat")
            .order_by("due_date", "slot_no", "seat__seat_no", "id")
        )

        overdue_total = Decimal("0")
        current_total = Decimal("0")
        next_total = Decimal("0")

        next_due_date = None
        next_due_rows: List[MerryContributionDue] = []

        for due in dues:
            outstanding = _outstanding_amount(due)
            if outstanding <= 0:
                continue

            bucket = _due_bucket(due, today=today)

            if bucket == "overdue":
                overdue_total += outstanding
            elif bucket == "current":
                current_total += outstanding
            elif bucket == "future" and getattr(due, "is_advance_payable", True):
                if next_due_date is None:
                    next_due_date = due.due_date
                if due.due_date == next_due_date:
                    next_due_rows.append(due)

        for due in next_due_rows:
            next_total += _outstanding_amount(due)

        required_now = q2(overdue_total + current_total)
        pay_with_next = q2(required_now + next_total)

        grand_overdue += overdue_total
        grand_current += current_total
        grand_next += next_total

        summary_items.append({
            "merry_id": merry.id,
            "merry_name": merry.name,
            "seat_count": len(seat_numbers),
            "seat_numbers": seat_numbers,
            "amount_per_seat": merry.contribution_amount,
            "overdue": q2(overdue_total),
            "current_due": q2(current_total),
            "next_due": q2(next_total),
            "next_due_date": next_due_date,
            "required_now": required_now,
            "pay_with_next": pay_with_next,
        })

    return {
        "active_merries": memberships.count(),
        "total_seats": total_seats,
        "total_overdue": q2(grand_overdue),
        "total_current_due": q2(grand_current),
        "total_next_due": q2(grand_next),
        "total_required_now": q2(grand_overdue + grand_current),
        "total_pay_with_next": q2(grand_overdue + grand_current + grand_next),
        "total_wallet_balance": get_user_merry_wallet_balance(user=user),
        "items": summary_items,
    }


def get_merry_member_payment_breakdown(
    *,
    user,
    merry_id: int,
    include_next: bool = False,
) -> Dict[str, Any]:
    merry = get_merry(merry_id)
    member = get_active_member(merry, user)
    today = timezone.localdate()

    selected_dues = _select_member_dues_for_breakdown(
        member=member,
        include_next=include_next,
    )

    seat_numbers = list(
        member.seats.filter(is_active=True).order_by("seat_no").values_list("seat_no", flat=True)
    )

    overdue_total = Decimal("0")
    current_total = Decimal("0")
    next_total = Decimal("0")
    next_due_date = None

    due_items = []
    for due in selected_dues:
        outstanding = _outstanding_amount(due)
        if outstanding <= 0:
            continue

        bucket = _due_bucket(due, today=today)
        if bucket == "overdue":
            overdue_total += outstanding
        elif bucket == "current":
            current_total += outstanding
        elif bucket == "future":
            next_total += outstanding
            if next_due_date is None:
                next_due_date = due.due_date

        due_items.append(
            {
                "due_id": due.id,
                "seat_id": due.seat_id,
                "seat_no": due.seat.seat_no,
                "period_key": due.period_key,
                "slot_no": due.slot_no,
                "due_date": due.due_date,
                "status": due.status,
                "due_amount": q2(due.due_amount or Decimal("0")),
                "paid_amount": q2(due.paid_amount or Decimal("0")),
                "outstanding": outstanding,
                "bucket": bucket,
            }
        )

    required_now = q2(overdue_total + current_total)
    pay_with_next = q2(required_now + next_total)
    wallet_balance = get_user_merry_wallet_balance(user=user)

    return {
        "merry_id": merry.id,
        "merry_name": merry.name,
        "seat_count": len(seat_numbers),
        "seat_numbers": seat_numbers,
        "amount_per_seat": merry.contribution_amount,
        "include_next": bool(include_next),
        "overdue": q2(overdue_total),
        "current_due": q2(current_total),
        "next_due": q2(next_total),
        "next_due_date": next_due_date,
        "required_now": required_now,
        "pay_with_next": pay_with_next,
        "wallet_balance": wallet_balance,
        "net_required_now_after_wallet": (
            q2(required_now - wallet_balance)
            if required_now > wallet_balance
            else Decimal("0.00")
        ),
        "selected_total": pay_with_next if include_next else required_now,
        "items": due_items,
    }


def _get_current_or_next_turn_payout(*, merry: MerryGoRound) -> Optional[MerryPayout]:
    scheduled = (
        MerryPayout.objects.filter(
            merry=merry,
            status__in=["SCHEDULED", "PROCESSING"],
        )
        .select_related("seat", "seat__member", "seat__member__user")
        .order_by("period_key", "slot_no", "id")
        .first()
    )
    if scheduled:
        return scheduled

    return (
        MerryPayout.objects.filter(merry=merry, status="PAID")
        .select_related("seat", "seat__member", "seat__member__user")
        .order_by("-paid_at", "-created_at", "-id")
        .first()
    )


def _get_member_next_turn(*, merry: MerryGoRound, member: MerryMember) -> Optional[Dict[str, Any]]:
    member_seats = list(
        member.seats.filter(is_active=True).order_by("payout_position", "seat_no", "id")
    )
    if not member_seats:
        return None

    preview = _preview_next_payout_meta(merry)
    next_seat = preview["seat"]
    period_key = preview["period_key"]
    slot_no = preview["slot_no"]
    due_date = preview["due_date"]
    period_meta = get_period_date_range(merry=merry, period_key=period_key)

    target_member_seat = member_seats[0]
    is_member_next = any(seat.id == next_seat.id for seat in member_seats)

    latest_member_payout = (
        MerryPayout.objects.filter(merry=merry, seat__member=member)
        .select_related("seat", "seat__member", "seat__member__user")
        .order_by("-created_at", "-id")
        .first()
    )

    return {
        "seat_id": target_member_seat.id,
        "seat_no": target_member_seat.seat_no,
        "payout_position": getattr(target_member_seat, "payout_position", None),
        "is_member_next": is_member_next,
        "next_turn_seat_id": next_seat.id,
        "next_turn_seat_no": next_seat.seat_no,
        "next_turn_member_id": next_seat.member_id,
        "period_key": period_key,
        "period_label": period_meta["label"],
        "period_start_date": period_meta["start_date"],
        "period_end_date": period_meta["end_date"],
        "slot_no": slot_no,
        "due_date": due_date,
        "expected_amount": q2(
            merry.total_pool_per_slot()
            if hasattr(merry, "total_pool_per_slot")
            else Decimal("0.00")
        ),
        "cycle_number": preview["cycle_number"],
        "cycle_complete": preview["cycle_complete"],
        "latest_member_payout_id": latest_member_payout.id if latest_member_payout else None,
        "latest_member_payout_status": latest_member_payout.status if latest_member_payout else "UPCOMING",
        "latest_member_paid_at": latest_member_payout.paid_at if latest_member_payout else None,
    }


def _get_member_merry_loan_offset_summary(*, merry: MerryGoRound, member: MerryMember) -> Dict[str, Any]:
    latest_paid_payout = (
        MerryPayout.objects.filter(merry=merry, seat__member=member, status="PAID")
        .select_related("seat", "seat__member")
        .order_by("-paid_at", "-id")
        .first()
    )

    gross_payout = q2(
        getattr(latest_paid_payout, "amount", Decimal("0.00"))
        if latest_paid_payout else Decimal("0.00")
    )

    deducted_to_loan = Decimal("0.00")
    loan_ids: List[int] = []
    outstanding_after = None

    if LoanSecurityAllocation is not None and Loan is not None:
        alloc_qs = LoanSecurityAllocation.objects.filter(
            owner_user=member.user,
            merry=merry,
            is_active=True,
            source_type="BORROWER_MERRY_CREDIT",
        )
        deducted_to_loan = q2(
            alloc_qs.aggregate(total=Sum("amount")).get("total") or Decimal("0.00")
        )
        loan_ids = list(alloc_qs.values_list("loan_id", flat=True).distinct())

        if loan_ids:
            loan = (
                Loan.objects.filter(id=loan_ids[0])
                .only("id", "outstanding_balance")
                .first()
            )
            if loan:
                outstanding_after = q2(loan.outstanding_balance or Decimal("0.00"))

    net_to_member = q2(max(Decimal("0.00"), gross_payout - deducted_to_loan))

    return {
        "has_active_merry_loan_offset": deducted_to_loan > 0,
        "gross_payout": gross_payout,
        "deducted_to_loan": deducted_to_loan,
        "net_to_member": net_to_member,
        "loan_ids": loan_ids,
        "loan_id": loan_ids[0] if loan_ids else None,
        "loan_outstanding_after_offset": outstanding_after,
    }


def get_member_merry_dashboard(*, user, merry_id: int) -> Dict[str, Any]:
    merry = get_merry(merry_id)
    member = get_active_member(merry, user)
    today = timezone.localdate()

    current_pk = get_current_period_key(merry)
    ensure_dues_for_member_period(merry, member, current_pk)
    period_meta = get_period_date_range(merry=merry, period_key=current_pk)

    seats = list(member.seats.filter(is_active=True).order_by("seat_no", "id"))
    seat_numbers = [s.seat_no for s in seats]
    seat_ids = [s.id for s in seats]

    selected_dues = list(
        MerryContributionDue.objects.filter(
            merry=merry,
            seat_id__in=seat_ids,
            status__in=["PENDING", "PARTIAL", "OVERDUE"],
        )
        .select_related("seat", "seat__member", "seat__member__user")
        .order_by("due_date", "period_key", "slot_no", "seat__seat_no", "id")
    )

    overdue_items: List[Dict[str, Any]] = []
    current_items: List[Dict[str, Any]] = []
    next_items: List[Dict[str, Any]] = []

    overdue_total = Decimal("0.00")
    current_total = Decimal("0.00")
    next_total = Decimal("0.00")
    next_due_date = None

    for due in selected_dues:
        outstanding = _outstanding_amount(due)
        if outstanding <= 0:
            continue

        bucket = _due_bucket(due, today=today)

        seat_member = due.seat.member
        seat_user = getattr(seat_member, "user", None)

        target_member_name = (
            getattr(seat_user, "username", None)
            or getattr(seat_user, "full_name", None)
            or getattr(seat_user, "phone", None)
            or f"Seat {due.seat.seat_no}"
        )

        row = {
            "due_id": due.id,
            "seat_id": due.seat_id,
            "seat_no": due.seat.seat_no,
            "period_key": due.period_key,
            "period": get_period_date_range(merry=merry, period_key=due.period_key),
            "slot_no": due.slot_no,
            "due_date": due.due_date,
            "status": due.status,
            "due_amount": q2(due.due_amount or Decimal("0.00")),
            "paid_amount": q2(due.paid_amount or Decimal("0.00")),
            "balance": outstanding,
            "outstanding": outstanding,
            "bucket": bucket,
            "target_member_id": seat_member.id if seat_member else None,
            "target_user_id": seat_user.id if seat_user else None,
            "target_member_name": target_member_name,
        }

        if bucket == "overdue":
            overdue_total += outstanding
            overdue_items.append(row)
        elif bucket == "current":
            current_total += outstanding
            current_items.append(row)
        elif bucket == "future" and getattr(due, "is_advance_payable", True):
            if next_due_date is None:
                next_due_date = due.due_date
            if due.due_date == next_due_date:
                next_total += outstanding
                next_items.append(row)

    overdue_total = q2(overdue_total)
    current_total = q2(current_total)
    next_total = q2(next_total)
    required_now = q2(overdue_total + current_total)
    pay_with_next = q2(required_now + next_total)
    wallet_balance = q2(get_user_merry_wallet_balance(user=user))

    next_turn = _get_member_next_turn(merry=merry, member=member)
    loan_offset = _get_member_merry_loan_offset_summary(merry=merry, member=member)
    current_or_next_payout = _get_current_or_next_turn_payout(merry=merry)

    payout_summary = None
    if current_or_next_payout:
        payout_period_meta = get_period_date_range(
            merry=merry,
            period_key=current_or_next_payout.period_key,
        )
        payout_summary = {
            "payout_id": current_or_next_payout.id,
            "seat_id": current_or_next_payout.seat_id,
            "seat_no": current_or_next_payout.seat.seat_no,
            "member_id": current_or_next_payout.seat.member_id,
            "user_id": current_or_next_payout.seat.member.user_id,
            "username": getattr(current_or_next_payout.seat.member.user, "username", None),
            "period_key": current_or_next_payout.period_key,
            "period_label": payout_period_meta["label"],
            "slot_no": current_or_next_payout.slot_no,
            "amount": q2(current_or_next_payout.amount or Decimal("0.00")),
            "status": current_or_next_payout.status,
            "paid_at": current_or_next_payout.paid_at,
        }

    readiness = None
    try:
        readiness = get_payout_readiness_status(merry_id=merry.id)
    except Exception:
        readiness = None

    return {
        "merry": {
            "id": merry.id,
            "name": merry.name,
            "contribution_amount": q2(merry.contribution_amount or Decimal("0.00")),
            "members_count": merry.members.filter(is_active=True).count(),
            "seats_count": merry.seats.filter(is_active=True).count(),
            "my_seat_count": len(seat_numbers),
            "my_seat_numbers": seat_numbers,
            "payout_frequency": merry.payout_frequency,
            "payouts_per_period": merry.payouts_per_period,
            "total_pool_per_slot": q2(
                merry.total_pool_per_slot()
                if hasattr(merry, "total_pool_per_slot")
                else Decimal("0.00")
            ),
            "total_pool_per_period": q2(
                merry.total_pool_per_period()
                if hasattr(merry, "total_pool_per_period")
                else Decimal("0.00")
            ),
            "payout_formula": (
                f"{merry.payouts_per_period} slot(s) per period"
                if getattr(merry, "payouts_per_period", 1)
                else "Standard merry payout"
            ),
        },
        "period": {
            "period_key": current_pk,
            "label": period_meta["label"],
            "start_date": period_meta["start_date"],
            "end_date": period_meta["end_date"],
            "frequency": period_meta["frequency"],
        },
        "dues": {
            "overdue_total": overdue_total,
            "current_total": current_total,
            "next_total": next_total,
            "required_now": required_now,
            "pay_with_next": pay_with_next,
            "next_due_date": next_due_date,
            "has_overdue": bool(overdue_total > 0),
            "overdue_items": overdue_items,
            "current_items": current_items,
            "next_items": next_items,
        },
        "current_turn": payout_summary,
        "my_turn": next_turn,
        "loan_offset": loan_offset,
        "wallet_balance": wallet_balance,
        "readiness": readiness,
    }