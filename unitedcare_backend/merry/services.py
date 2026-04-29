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
    return Decimal(str(x)).quantize(Decimal("0.01"))


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


# ---------- ROSCA queue scheduling ----------
def _normalized_frequency(merry: MerryGoRound) -> str:
    return (getattr(merry, "payout_frequency", None) or "WEEKLY").upper().strip()


def _add_months(d: date, months: int = 1) -> date:
    months = max(1, int(months or 1))
    year = d.year
    month = d.month + months
    while month > 12:
        month -= 12
        year += 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)


def _ordered_slot_configs(merry: MerryGoRound) -> List[MerrySlotConfig]:
    return list(merry.slot_configs.all().order_by("slot_no", "id"))


def _has_slot_config_schedule(merry: MerryGoRound) -> bool:
    return len(_ordered_slot_configs(merry)) > 0


def _next_slot_config_candidate_on_or_after(merry: MerryGoRound, anchor_date: date) -> Tuple[date, int]:
    configs = _ordered_slot_configs(merry)
    if not configs:
        return anchor_date, 1

    for offset in range(0, 21):
        candidate = anchor_date + timedelta(days=offset)
        weekday = candidate.weekday()
        for cfg in configs:
            if int(cfg.weekday) == weekday:
                return candidate, int(cfg.slot_no)

    first = configs[0]
    return anchor_date, int(first.slot_no)


def _next_slot_config_candidate_after(merry: MerryGoRound, anchor_date: date) -> Tuple[date, int]:
    return _next_slot_config_candidate_on_or_after(merry, anchor_date + timedelta(days=1))


def _add_schedule_step(merry: MerryGoRound, base_date: date) -> date:
    if _has_slot_config_schedule(merry):
        return _next_slot_config_candidate_after(merry, base_date)[0]

    if hasattr(merry, "add_schedule_step"):
        return merry.add_schedule_step(base_date)

    freq = _normalized_frequency(merry)

    if freq == "DAILY":
        return base_date + timedelta(days=1)

    if freq == "MONTHLY":
        return _add_months(base_date, 1)

    weeks = max(1, int(getattr(merry, "cycle_duration_weeks", 1) or 1))
    return base_date + timedelta(weeks=weeks)


def _date_to_period_key(d: date) -> str:
    return d.isoformat()


def _period_key_to_date(period_key: str) -> date:
    try:
        return date.fromisoformat((period_key or "").strip())
    except Exception:
        raise BadState("Invalid payout period_key. Expected YYYY-MM-DD.")


def _next_period_key(merry: MerryGoRound, period_key: str) -> str:
    return _date_to_period_key(_add_schedule_step(merry, _period_key_to_date(period_key)))


def get_period_date_range(*, merry: MerryGoRound, period_key: str) -> Dict[str, Any]:
    pk = (period_key or "").strip()
    if not pk:
        raise BadState("period_key is required.")

    d = _period_key_to_date(pk)

    if _has_slot_config_schedule(merry):
        return {
            "period_key": pk,
            "label": d.strftime("%A, %d %b %Y"),
            "start_date": d,
            "end_date": d,
            "frequency": "WEEKDAY_SCHEDULE",
        }

    freq = _normalized_frequency(merry)

    if freq == "DAILY":
        return {
            "period_key": pk,
            "label": d.strftime("%d %b %Y"),
            "start_date": d,
            "end_date": d,
            "frequency": freq,
        }

    if freq == "MONTHLY":
        start = date(d.year, d.month, 1)
        end = date(d.year, d.month, monthrange(d.year, d.month)[1])
        return {
            "period_key": pk,
            "label": start.strftime("%B %Y"),
            "start_date": start,
            "end_date": end,
            "frequency": freq,
        }

    start_date = d
    end_date = d + timedelta(days=6)
    return {
        "period_key": pk,
        "label": f"Week of {start_date.strftime('%d %b %Y')}",
        "start_date": start_date,
        "end_date": end_date,
        "frequency": freq,
    }

def _expected_pool_amount(merry: MerryGoRound) -> Decimal:
    if hasattr(merry, "total_pool_per_slot"):
        return q2(merry.total_pool_per_slot())
    active_seat_count = MerrySeat.objects.filter(merry=merry, is_active=True).count()
    return q2((merry.contribution_amount or Decimal("0.00")) * active_seat_count)


def _joined_on(member: MerryMember) -> date:
    joined_at = getattr(member, "joined_at", None)
    if joined_at is None:
        return timezone.localdate()
    if hasattr(joined_at, "date"):
        return joined_at.date()
    return joined_at


def _seat_eligible_for_due(*, seat: MerrySeat, due_date: Optional[date]) -> bool:
    if due_date is None:
        return True
    return _joined_on(seat.member) <= due_date


def _refresh_due_penalty(due: MerryContributionDue, *, save: bool = True) -> MerryContributionDue:
    if hasattr(due, "refresh_penalty"):
        due.refresh_penalty(save=save)
        return due

    merry = due.merry
    today = timezone.localdate()

    if due.status in ["PAID", "CANCELLED"]:
        return due

    due_date = due.due_date
    if not due_date:
        return due

    grace_days = int(getattr(merry, "penalty_grace_days", 0) or 0)
    penalty_start = due_date + timedelta(days=grace_days + 1)

    if today < penalty_start:
        days_overdue = 0
    else:
        days_overdue = (today - penalty_start).days + 1

    penalty_mode = getattr(merry, "penalty_mode", "NONE") or "NONE"
    penalty_amount = Decimal("0.00")

    if penalty_mode == "FLAT" and days_overdue > 0:
        penalty_amount = q2(
            getattr(merry, "flat_penalty_amount", Decimal("0.00")) or Decimal("0.00")
        )
    elif penalty_mode == "DAILY" and days_overdue > 0:
        per_day = q2(
            getattr(merry, "daily_penalty_amount", Decimal("0.00")) or Decimal("0.00")
        )
        penalty_amount = q2(per_day * Decimal(days_overdue))

    cap = getattr(merry, "penalty_cap_amount", None)
    if cap is not None:
        cap = q2(cap)
        if penalty_amount > cap:
            penalty_amount = cap

    base_amount = q2(
        getattr(due, "base_amount", Decimal("0.00"))
        or getattr(due, "due_amount", Decimal("0.00"))
        or Decimal("0.00")
    )
    if base_amount <= 0:
        base_amount = q2(getattr(due, "due_amount", Decimal("0.00")) or Decimal("0.00"))

    if hasattr(due, "base_amount"):
        due.base_amount = base_amount
    if hasattr(due, "penalty_amount"):
        due.penalty_amount = penalty_amount
    if hasattr(due, "days_overdue"):
        due.days_overdue = days_overdue
    due.due_amount = q2(base_amount + penalty_amount)

    if hasattr(due, "penalty_last_calculated_at") and days_overdue > 0:
        due.penalty_last_calculated_at = timezone.now()
    if (
        penalty_amount > 0
        and hasattr(due, "penalty_applied_at")
        and not due.penalty_applied_at
    ):
        due.penalty_applied_at = timezone.now()

    due.recalc_status()
    if save:
        update_fields = ["status", "updated_at", "due_amount"]
        if hasattr(due, "penalty_amount"):
            update_fields.append("penalty_amount")
        if hasattr(due, "days_overdue"):
            update_fields.append("days_overdue")
        if hasattr(due, "base_amount"):
            update_fields.append("base_amount")
        if hasattr(due, "penalty_last_calculated_at") and getattr(due, "penalty_last_calculated_at", None):
            update_fields.append("penalty_last_calculated_at")
        if hasattr(due, "penalty_applied_at") and due.penalty_applied_at:
            update_fields.append("penalty_applied_at")
        due.save(update_fields=update_fields)
    return due


def _refresh_penalties_for_queryset(qs) -> None:
    for due in qs:
        _refresh_due_penalty(due, save=True)


def _outstanding_amount(due: MerryContributionDue) -> Decimal:
    if hasattr(due, "outstanding"):
        return q2(due.outstanding())
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


def _base_amount_for_due(due: MerryContributionDue) -> Decimal:
    if hasattr(due, "effective_base_amount"):
        return q2(due.effective_base_amount())
    return q2(getattr(due, "base_amount", Decimal("0.00")) or getattr(due, "due_amount", Decimal("0.00")))


def _penalty_amount_for_due(due: MerryContributionDue) -> Decimal:
    return q2(getattr(due, "penalty_amount", Decimal("0.00")) or Decimal("0.00"))


def _get_member_next_future_dues(member: MerryMember) -> List[MerryContributionDue]:
    return list(
        MerryContributionDue.objects.filter(
            merry=member.merry,
            seat__member=member,
            seat__is_active=True,
            status__in=["PENDING", "PARTIAL", "OVERDUE"],
            due_date__gt=timezone.localdate(),
        )
        .select_related("seat", "merry")
        .order_by("due_date", "seat__seat_no", "id")
    )


def _select_member_dues_for_breakdown(
    *,
    member: MerryMember,
    include_next: bool = False,
) -> List[MerryContributionDue]:
    ensure_member_dues_up_to_current_turn(member=member)

    qs = (
        MerryContributionDue.objects.filter(
            merry=member.merry,
            seat__member=member,
            seat__is_active=True,
            status__in=["PENDING", "PARTIAL", "OVERDUE"],
        )
        .exclude(
            payout__isnull=True,
            due_date__isnull=True,
        )
        .select_related("seat", "merry")
        .order_by("due_date", "seat__seat_no", "id")
    )

    if not include_next:
        qs = qs.filter(due_date__lte=timezone.localdate())

    dues = list(qs)
    _refresh_penalties_for_queryset(dues)
    return dues


@transaction.atomic
def _select_member_dues_for_payment(
    *,
    member: MerryMember,
    include_next: bool = False,
) -> List[MerryContributionDue]:
    ensure_member_dues_up_to_current_turn(member=member)

    qs = (
        MerryContributionDue.objects.select_for_update()
        .filter(
            merry=member.merry,
            seat__member=member,
            seat__is_active=True,
            status__in=["PENDING", "PARTIAL", "OVERDUE"],
        )
        .exclude(
            payout__isnull=True,
            due_date__isnull=True,
        )
        .select_related("seat", "merry")
        .order_by("due_date", "seat__seat_no", "id")
    )

    if not include_next:
        qs = qs.filter(due_date__lte=timezone.localdate())

    dues = list(qs)
    _refresh_penalties_for_queryset(dues)
    return dues


# -----------------------------
# Wallet helpers
# -----------------------------
@transaction.atomic
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


@transaction.atomic
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
# Turn helpers
# -----------------------------
def _compute_turn_from_schedule(merry: MerryGoRound) -> int:
    today = timezone.localdate()

    anchor = getattr(merry, "next_payout_date", None)
    if not anchor:
        raise BadState("next_payout_date is not set.")

    turn = 1
    current = anchor

    while current < today:
        current = _add_schedule_step(merry, current)
        turn += 1

    return turn

def _current_turn_from_schedule(merry: MerryGoRound) -> int:
    today = timezone.localdate()

    anchor = getattr(merry, "next_payout_date", None)
    if not anchor:
        raise BadState("next_payout_date is not set.")

    turn = 1
    current = anchor

    while True:
        next_date = _add_schedule_step(merry, current)

        if next_date > today:
            return turn  # ✅ stops BEFORE passing today

        current = next_date
        turn += 1

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


def _highest_turn_no(merry: MerryGoRound) -> int:
    mx = MerryPayout.objects.filter(merry=merry).aggregate(m=Max("turn_no")).get("m") or 0
    return int(mx)


def _current_cycle_number(merry: MerryGoRound) -> int:
    seats_count = _cycle_length(merry)
    if seats_count <= 0:
        return 0

    current_payout = get_existing_current_payout(merry_id=merry.id)
    if current_payout:
        turn_no = int(getattr(current_payout, "turn_no", 1) or 1)
        return ((turn_no - 1) // seats_count) + 1

    highest_turn_no = _highest_turn_no(merry)
    if highest_turn_no <= 0:
        return 1
    return ((highest_turn_no - 1) // seats_count) + 1


def _is_cycle_complete(merry: MerryGoRound) -> bool:
    seats_count = _cycle_length(merry)
    if seats_count <= 0:
        return False

    highest_turn_no = _highest_turn_no(merry)
    return highest_turn_no > 0 and (highest_turn_no % seats_count == 0)


def _next_turn_index(merry: MerryGoRound, *, turn_no: Optional[int] = None) -> int:
    seats_count = _cycle_length(merry)
    if seats_count <= 0:
        raise BadState("This merry has no active seats.")

    resolved_turn_no = int(turn_no or _next_turn_no(merry))
    return (resolved_turn_no - 1) % seats_count


def _next_turn_seat(merry: MerryGoRound, *, turn_no: Optional[int] = None) -> MerrySeat:
    seats = _ordered_active_payout_seats(merry)
    return seats[_next_turn_index(merry, turn_no=turn_no)]


def _next_turn_no(merry: MerryGoRound) -> int:
    # return _highest_turn_no(merry) + 1
    return _compute_turn_from_schedule(merry)


def _cycle_number_for_turn(merry: MerryGoRound, turn_no: int) -> int:
    seats_count = _cycle_length(merry)
    if seats_count <= 0:
        return 1
    return ((int(turn_no) - 1) // seats_count) + 1


def _turn_date_for_next_payout(merry: MerryGoRound) -> date:
    slot_date, _ = _next_turn_schedule_for_payout(merry)
    return slot_date


def _next_turn_schedule_for_payout(merry: MerryGoRound) -> Tuple[date, int]:
    last_payout = (
        MerryPayout.objects.filter(merry=merry)
        .order_by("-turn_no", "-id")
        .first()
    )

    if last_payout and getattr(last_payout, "scheduled_date", None):
        if _has_slot_config_schedule(merry):
            return _next_slot_config_candidate_after(merry, last_payout.scheduled_date)
        return _add_schedule_step(merry, last_payout.scheduled_date), 1

    if last_payout and getattr(last_payout, "period_key", None):
        try:
            base_date = _period_key_to_date(last_payout.period_key)
            if _has_slot_config_schedule(merry):
                return _next_slot_config_candidate_after(merry, base_date)
            return _add_schedule_step(merry, base_date), 1
        except Exception:
            pass

    created_anchor = (
        merry.created_at.date()
        if getattr(merry, "created_at", None)
        else timezone.localdate()
    )
    configured_anchor = getattr(merry, "next_payout_date", None)
    anchor = configured_anchor if configured_anchor and configured_anchor >= created_anchor else created_anchor

    if _has_slot_config_schedule(merry):
        return _next_slot_config_candidate_on_or_after(merry, anchor)

    return anchor, 1


def _get_or_create_scheduled_payout_for_turn(
    *,
    merry: MerryGoRound,
    turn_no: int,
    scheduled_date: date,
    slot_no: int,
) -> MerryPayout:
    period_key = _date_to_period_key(scheduled_date)

    existing = (
        MerryPayout.objects.select_for_update()
        .filter(merry=merry, turn_no=turn_no)
        .select_related("seat", "seat__member", "seat__member__user")
        .first()
    )
    if existing:
        changed = False
        if existing.period_key != period_key:
            existing.period_key = period_key
            changed = True
        if int(getattr(existing, "slot_no", 1) or 1) != int(slot_no):
            existing.slot_no = int(slot_no)
            changed = True
        if getattr(existing, "scheduled_date", None) != scheduled_date:
            existing.scheduled_date = scheduled_date
            changed = True
        expected_cycle_no = _cycle_number_for_turn(merry, turn_no)
        if int(getattr(existing, "cycle_no", expected_cycle_no) or expected_cycle_no) != expected_cycle_no:
            existing.cycle_no = expected_cycle_no
            changed = True
        expected_seat = _next_turn_seat(merry, turn_no=turn_no)
        if existing.seat_id != expected_seat.id:
            existing.seat = expected_seat
            changed = True
        expected_amount = _expected_pool_amount(merry)
        if q2(getattr(existing, "amount", Decimal("0.00")) or Decimal("0.00")) != expected_amount:
            existing.amount = expected_amount
            changed = True
        if changed:
            existing.save(update_fields=["period_key", "slot_no", "scheduled_date", "cycle_no", "seat", "amount"])
        return existing

    existing = (
        MerryPayout.objects.select_for_update()
        .filter(merry=merry, status="SCHEDULED", period_key=period_key, slot_no=slot_no)
        .select_related("seat", "seat__member", "seat__member__user")
        .first()
    )
    if existing:
        changed = False
        if int(getattr(existing, "turn_no", turn_no) or turn_no) != int(turn_no):
            existing.turn_no = int(turn_no)
            changed = True
        expected_cycle_no = _cycle_number_for_turn(merry, turn_no)
        if int(getattr(existing, "cycle_no", expected_cycle_no) or expected_cycle_no) != expected_cycle_no:
            existing.cycle_no = expected_cycle_no
            changed = True
        expected_seat = _next_turn_seat(merry, turn_no=turn_no)
        if existing.seat_id != expected_seat.id:
            existing.seat = expected_seat
            changed = True
        expected_amount = _expected_pool_amount(merry)
        if q2(getattr(existing, "amount", Decimal("0.00")) or Decimal("0.00")) != expected_amount:
            existing.amount = expected_amount
            changed = True
        if changed:
            existing.save(update_fields=["turn_no", "cycle_no", "seat", "amount"])
        return existing

    seat = _next_turn_seat(merry, turn_no=turn_no)
    cycle_no = _cycle_number_for_turn(merry, turn_no)
    return MerryPayout.objects.create(
        merry=merry,
        seat=seat,
        turn_no=turn_no,
        cycle_no=cycle_no,
        scheduled_date=scheduled_date,
        period_key=period_key,
        slot_no=slot_no,
        amount=_expected_pool_amount(merry),
        status="SCHEDULED",
        notes=f"Auto-created current ROSCA payout for seat {seat.seat_no}.",
    )

def get_existing_current_payout(*, merry_id: int) -> Optional[MerryPayout]:
    today = timezone.localdate()
    payouts = (
        MerryPayout.objects.filter(merry_id=merry_id, status="SCHEDULED")
        .select_related("seat", "seat__member", "seat__member__user")
        .order_by("turn_no", "id")
    )

    for payout in payouts:
        payout_date = getattr(payout, "scheduled_date", None) or _period_key_to_date(payout.period_key)
        if payout_date >= today:
            return payout

    return payouts.order_by("-turn_no", "-id").first()

def _find_next_open_period_slot_for_seat(
    *,
    merry: MerryGoRound,
    seat: MerrySeat,
    start_period_key: Optional[str] = None,
) -> Tuple[str, int]:
    payout = get_existing_current_payout(merry_id=merry.id)
    if payout:
        return payout.period_key, int(getattr(payout, "slot_no", 1) or 1)
    due_date, slot_no = _next_turn_schedule_for_payout(merry)
    return _date_to_period_key(due_date), slot_no


def _preview_next_payout_meta(merry: MerryGoRound) -> Dict[str, Any]:
    payout = get_existing_current_payout(merry_id=merry.id)
    if payout:
        seat = payout.seat
        due_date = getattr(payout, "scheduled_date", None) or _period_key_to_date(payout.period_key)
        period_key = payout.period_key
        slot_no = int(getattr(payout, "slot_no", 1) or 1)
        turn_no = getattr(payout, "turn_no", 1)
        cycle_number = getattr(payout, "cycle_no", _cycle_number_for_turn(merry, turn_no))
    else:
        seat = _next_turn_seat(merry)
        due_date, slot_no = _next_turn_schedule_for_payout(merry)
        period_key = _date_to_period_key(due_date)
        turn_no = _next_turn_no(merry)
        cycle_number = _cycle_number_for_turn(merry, turn_no)

    return {
        "seat": seat,
        "period_key": period_key,
        "slot_no": slot_no,
        "due_date": due_date,
        "cycle_number": cycle_number,
        "cycle_complete": _is_cycle_complete(merry),
        "turn_no": turn_no,
    }


# def maybe_update_next_payout_date(*, merry: MerryGoRound) -> None:
#     try:
#         payout = get_existing_current_payout(merry_id=merry.id)
#         if payout:
#             next_date = getattr(payout, "scheduled_date", None) or _period_key_to_date(payout.period_key)
#         else:
#             next_date = _turn_date_for_next_payout(merry)
#     except Exception:
#         next_date = None

#     if merry.next_payout_date != next_date:
#         merry.next_payout_date = next_date
#         merry.save(update_fields=["next_payout_date"])
def maybe_update_next_payout_date(*, merry: MerryGoRound) -> None:
    # 🚫 DO NOT update next_payout_date automatically
    # This field must remain the FIRST payout (anchor)
    return

# -----------------------------
# Dues / payout generation
# -----------------------------
@transaction.atomic
def ensure_current_payout_exists(*, merry_id: int) -> MerryPayout:
    merry = get_merry(merry_id)
    today = timezone.localdate()

    scheduled_payouts = list(
        MerryPayout.objects.select_for_update()
        .filter(merry=merry, status="SCHEDULED")
        .select_related("seat", "seat__member", "seat__member__user")
        .order_by("turn_no", "id")
    )

    future_or_today = None
    for payout in scheduled_payouts:
        payout_date = getattr(payout, "scheduled_date", None) or _period_key_to_date(payout.period_key)
        if payout_date >= today:
            future_or_today = payout
            break

    if future_or_today:
        if merry.next_payout_date != future_or_today.scheduled_date:
            merry.next_payout_date = future_or_today.scheduled_date
            merry.save(update_fields=["next_payout_date"])
        return future_or_today

    if scheduled_payouts:
        latest_scheduled = scheduled_payouts[-1]
        last_date = getattr(latest_scheduled, "scheduled_date", None) or _period_key_to_date(latest_scheduled.period_key)
        next_turn_no = int(getattr(latest_scheduled, "turn_no", 0) or 0) + 1
    else:
        latest_payout = (
            MerryPayout.objects.select_for_update()
            .filter(merry=merry)
            .order_by("-turn_no", "-id")
            .select_related("seat", "seat__member", "seat__member__user")
            .first()
        )
        if latest_payout:
            last_date = getattr(latest_payout, "scheduled_date", None) or _period_key_to_date(latest_payout.period_key)
            next_turn_no = int(getattr(latest_payout, "turn_no", 0) or 0) + 1
        else:
            created_anchor = (
                merry.created_at.date()
                if getattr(merry, "created_at", None)
                else today
            )
            configured_anchor = getattr(merry, "next_payout_date", None)
            anchor = configured_anchor if configured_anchor and configured_anchor >= created_anchor else created_anchor

            if _has_slot_config_schedule(merry):
                first_date, first_slot_no = _next_slot_config_candidate_on_or_after(merry, anchor)
            else:
                first_date, first_slot_no = anchor, 1

            payout = _get_or_create_scheduled_payout_for_turn(
                merry=merry,
                turn_no=1,
                scheduled_date=first_date,
                slot_no=first_slot_no,
            )
            if merry.next_payout_date != first_date:
                merry.next_payout_date = first_date
                merry.save(update_fields=["next_payout_date"])
            return payout

    next_date = last_date
    next_slot_no = 1
    while next_date < today:
        if _has_slot_config_schedule(merry):
            next_date, next_slot_no = _next_slot_config_candidate_after(merry, next_date)
        else:
            next_date = _add_schedule_step(merry, next_date)
            next_slot_no = 1

        payout = _get_or_create_scheduled_payout_for_turn(
            merry=merry,
            turn_no=next_turn_no,
            scheduled_date=next_date,
            slot_no=next_slot_no,
        )
        next_turn_no += 1

    if merry.next_payout_date != payout.scheduled_date:
        merry.next_payout_date = payout.scheduled_date
        merry.save(update_fields=["next_payout_date"])

    return payout

@transaction.atomic
def ensure_dues_for_current_payout(*, merry_id: int) -> int:
    merry = get_merry(merry_id)
    payout = ensure_current_payout_exists(merry_id=merry.id)
    due_date = getattr(payout, "scheduled_date", None) or _period_key_to_date(payout.period_key)

    count = 0
    active_seats = list(
        MerrySeat.objects.select_for_update()
        .filter(merry=merry, is_active=True)
        .select_related("member", "member__user")
        .order_by("seat_no", "id")
    )

    for seat in active_seats:
        if not _seat_eligible_for_due(seat=seat, due_date=due_date):
            continue

        due, created = MerryContributionDue.objects.get_or_create(
            merry=merry,
            seat=seat,
            payout=payout,
            defaults={
                "period_key": payout.period_key,
                "slot_no": 1,
                "base_amount": q2(merry.contribution_amount or Decimal("0.00")),
                "due_amount": q2(merry.contribution_amount or Decimal("0.00")),
                "paid_amount": Decimal("0.00"),
                "penalty_amount": Decimal("0.00"),
                "days_overdue": 0,
                "status": "PENDING",
                "due_date": due_date,
                "is_advance_payable": False,
            },
        )
        if created:
            count += 1
        else:
            changed = False
            if due.period_key != payout.period_key:
                due.period_key = payout.period_key
                changed = True
            if due.slot_no != 1:
                due.slot_no = 1
                changed = True
            if due.due_date != due_date:
                due.due_date = due_date
                changed = True
            if hasattr(due, "base_amount") and q2(due.base_amount or Decimal("0.00")) <= 0:
                due.base_amount = q2(merry.contribution_amount or Decimal("0.00"))
                changed = True
            if changed:
                if hasattr(due, "base_amount"):
                    due.due_amount = q2((due.base_amount or Decimal("0.00")) + (getattr(due, "penalty_amount", Decimal("0.00")) or Decimal("0.00")))
                due.recalc_status()
                update_fields = ["period_key", "slot_no", "due_date", "status", "updated_at", "due_amount"]
                if hasattr(due, "base_amount"):
                    update_fields.append("base_amount")
                due.save(update_fields=update_fields)

        _refresh_due_penalty(due, save=True)

    return count


@transaction.atomic
def _ensure_historical_member_dues_until_turn(member: MerryMember, upto_turn_no: int) -> int:
    merry = member.merry
    created = 0

    payouts = list(
        MerryPayout.objects.filter(
            merry=merry,
            turn_no__lte=upto_turn_no,
        )
        .order_by("turn_no", "id")
    )

    seats = list(
        MerrySeat.objects.select_for_update()
        .filter(merry=merry, member=member, is_active=True)
        .order_by("seat_no", "id")
    )

    for payout in payouts:
        due_date = getattr(payout, "scheduled_date", None) or _period_key_to_date(payout.period_key)
        for seat in seats:
            if not _seat_eligible_for_due(seat=seat, due_date=due_date):
                continue

            due, was_created = MerryContributionDue.objects.get_or_create(
                merry=merry,
                seat=seat,
                payout=payout,
                defaults={
                    "period_key": payout.period_key,
                    "slot_no": 1,
                    "base_amount": q2(merry.contribution_amount or Decimal("0.00")),
                    "due_amount": q2(merry.contribution_amount or Decimal("0.00")),
                    "paid_amount": Decimal("0.00"),
                    "penalty_amount": Decimal("0.00"),
                    "days_overdue": 0,
                    "status": "PENDING",
                    "due_date": due_date,
                    "is_advance_payable": False,
                },
            )
            if was_created:
                created += 1
            else:
                changed = False
                if due.period_key != payout.period_key:
                    due.period_key = payout.period_key
                    changed = True
                if due.slot_no != 1:
                    due.slot_no = 1
                    changed = True
                if due.due_date != due_date:
                    due.due_date = due_date
                    changed = True
                if hasattr(due, "base_amount") and q2(due.base_amount or Decimal("0.00")) <= 0:
                    due.base_amount = q2(merry.contribution_amount or Decimal("0.00"))
                    changed = True
                if changed:
                    if hasattr(due, "base_amount"):
                        due.due_amount = q2((due.base_amount or Decimal("0.00")) + (getattr(due, "penalty_amount", Decimal("0.00")) or Decimal("0.00")))
                    due.recalc_status()
                    update_fields = ["period_key", "slot_no", "due_date", "status", "updated_at", "due_amount"]
                    if hasattr(due, "base_amount"):
                        update_fields.append("base_amount")
                    due.save(update_fields=update_fields)

            _refresh_due_penalty(due, save=True)

    return created


@transaction.atomic
def ensure_member_dues_up_to_current_turn(*, member: MerryMember) -> int:
    payout = ensure_current_payout_exists(merry_id=member.merry_id)
    return _ensure_historical_member_dues_until_turn(member, getattr(payout, "turn_no", 1))


@transaction.atomic
def ensure_dues_for_period(*, admin_user, merry_id: int, period_key: Optional[str] = None) -> int:
    if not is_admin(admin_user):
        raise NotAllowed("Admin only.")
    merry = get_merry(merry_id)
    count = ensure_dues_for_current_payout(merry_id=merry.id)

    memberships = MerryMember.objects.filter(merry=merry, is_active=True).select_related("user")
    for membership in memberships:
        ensure_member_dues_up_to_current_turn(member=membership)
        apply_merry_wallet_to_user_open_dues(
            user_id=membership.user_id,
            reference=f"AUTO-{merry.id}",
            narration="Automatic wallet application after current due generation.",
        )

    return count


@transaction.atomic
def ensure_dues_for_member_period(merry: MerryGoRound, member: MerryMember, period_key: str) -> None:
    payout = ensure_current_payout_exists(merry_id=merry.id)
    ensure_member_dues_up_to_current_turn(member=member)

    apply_merry_wallet_to_user_open_dues(
        user_id=member.user_id,
        reference=f"AUTO-{payout.period_key}",
        narration=f"Automatic wallet application for current ROSCA payout {payout.period_key}.",
    )


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
        ensure_member_dues_up_to_current_turn(member=member)
    return members


@transaction.atomic
def _collect_open_dues_for_member_period(member: MerryMember, period_key: str) -> List[MerryContributionDue]:
    ensure_member_dues_up_to_current_turn(member=member)

    dues = list(
        MerryContributionDue.objects.select_for_update()
        .filter(
            merry=member.merry,
            seat__member=member,
            seat__is_active=True,
            status__in=["PENDING", "PARTIAL", "OVERDUE"],
        )
        .exclude(
            payout__isnull=True,
            due_date__isnull=True,
        )
        .select_related("seat", "merry", "seat__member", "seat__member__user")
        .order_by("due_date", "seat__seat_no", "id")
    )
    _refresh_penalties_for_queryset(dues)
    return dues


@transaction.atomic
def _collect_open_dues_for_member(member: MerryMember) -> List[MerryContributionDue]:
    ensure_member_dues_up_to_current_turn(member=member)

    dues = list(
        MerryContributionDue.objects.select_for_update()
        .filter(
            merry=member.merry,
            seat__member=member,
            seat__is_active=True,
            status__in=["PENDING", "PARTIAL", "OVERDUE"],
        )
        .exclude(
            payout__isnull=True,
            due_date__isnull=True,
        )
        .select_related("seat", "merry", "seat__member", "seat__member__user")
        .order_by("due_date", "seat__seat_no", "id")
    )
    _refresh_penalties_for_queryset(dues)
    return dues


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
            getattr(x[1].payout, "turn_no", 10**9) if getattr(x[1], "payout", None) else 10**9,
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
            "payout_id": getattr(due, "payout_id", None),
            "turn_no": getattr(due.payout, "turn_no", None) if getattr(due, "payout", None) else None,
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
            narration=narration or "Wallet used to settle open merry dues.",
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

    created_payments: List[MerryPayment] = []

    for member in members:
        if remaining <= 0:
            break

        dues = _collect_open_dues_for_member(member)

        member_need = Decimal("0.00")
        for due in dues:
            member_need = q2(member_need + _outstanding_amount(due))

        if member_need <= 0:
            continue

        payout = ensure_current_payout_exists(merry_id=member.merry_id)
        pay_amount = q2(min(remaining, member_need))
        payment = _create_confirmed_payment_shell(
            member=member,
            total_amount=pay_amount,
            payer_phone=payer_phone,
            period_key=payout.period_key,
            initiated_by=user,
            mpesa_receipt_number=mpesa_receipt,
            paid_at=paid_at,
        )
        allocate_payment(payment_id=payment.id)
        created_payments.append(payment)
        remaining = q2(remaining - pay_amount)

    # if not created_payments and remaining == total_amount:
    #     raise BadState("No allocatable merry dues were found for this user.")

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

def _calculate_cycle_duration_weeks(*, active_seats: int, payout_days_count: int) -> int:
    active_seats = int(active_seats or 0)
    payout_days_count = int(payout_days_count or 1)

    if payout_days_count < 1:
        payout_days_count = 1

    if active_seats < 1:
        return 1

    return max(1, (active_seats + payout_days_count - 1) // payout_days_count)


def recalculate_merry_cycle_duration(*, merry: MerryGoRound) -> MerryGoRound:
    active_seats = MerrySeat.objects.filter(merry=merry, is_active=True).count()
    payout_days_count = MerrySlotConfig.objects.filter(merry=merry).count()

    if payout_days_count < 1:
        payout_days_count = 1

    new_cycle_weeks = _calculate_cycle_duration_weeks(
        active_seats=active_seats,
        payout_days_count=payout_days_count,
    )

    changed = False

    if merry.payouts_per_period != payout_days_count:
        merry.payouts_per_period = payout_days_count
        changed = True

    if merry.cycle_duration_weeks != new_cycle_weeks:
        merry.cycle_duration_weeks = new_cycle_weeks
        changed = True

    if changed:
        merry.save(update_fields=["payouts_per_period", "cycle_duration_weeks"])

    return merry

# -----------------------------
# Merry lifecycle
# -----------------------------
# @transaction.atomic
# def create_merry(
#     *,
#     creator,
#     name: str,
#     contribution_amount: Decimal,
#     cycle_duration_weeks: int = 1,
#     payout_order_type: str = "manual",
#     next_payout_date=None,
#     payout_frequency: str = "WEEKLY",
#     payouts_per_period: int = 1,
#     is_open: bool = True,
#     max_seats: int = 0,
#     penalty_mode: str = "NONE",
#     flat_penalty_amount: Decimal = Decimal("0.00"),
#     daily_penalty_amount: Decimal = Decimal("0.00"),
#     penalty_grace_days: int = 0,
#     penalty_cap_amount: Optional[Decimal] = None,
# ) -> MerryGoRound:
#     if not is_admin(creator):
#         raise NotAllowed("Admin only.")

#     name = (name or "").strip()
#     if not name:
#         raise BadState("name is required.")

#     amount = parse_decimal(contribution_amount, "contribution_amount")
#     if amount <= 0:
#         raise BadState("contribution_amount must be > 0.")

#     cycle_duration_weeks = parse_int(
#         cycle_duration_weeks,
#         "cycle_duration_weeks",
#         min_value=1,
#         max_value=520,
#     )

#     payout_order_type = (payout_order_type or "manual").strip().lower()
#     if payout_order_type not in ("manual", "random"):
#         raise BadState("payout_order_type must be 'manual' or 'random'.")

#     payout_frequency = (payout_frequency or "WEEKLY").upper().strip()
#     if payout_frequency not in ("DAILY", "WEEKLY", "MONTHLY"):
#         raise BadState("payout_frequency must be 'DAILY', 'WEEKLY' or 'MONTHLY'.")

#     payouts_per_period = parse_int(
#         payouts_per_period,
#         "payouts_per_period",
#         min_value=1,
#         max_value=14,
#     )

#     is_open = parse_bool(is_open, default=True)
#     max_seats = parse_int(max_seats or 0, "max_seats", min_value=0)

#     penalty_mode = (penalty_mode or "NONE").upper().strip()
#     if penalty_mode not in ("NONE", "FLAT", "DAILY"):
#         raise BadState("penalty_mode must be NONE, FLAT or DAILY.")

#     flat_penalty_amount = parse_decimal(flat_penalty_amount or 0, "flat_penalty_amount")
#     daily_penalty_amount = parse_decimal(daily_penalty_amount or 0, "daily_penalty_amount")
#     penalty_grace_days = parse_int(penalty_grace_days or 0, "penalty_grace_days", min_value=0)

#     if penalty_cap_amount not in (None, ""):
#         penalty_cap_amount = parse_decimal(penalty_cap_amount, "penalty_cap_amount")
#     else:
#         penalty_cap_amount = None

#     merry = MerryGoRound.objects.create(
#         name=name,
#         contribution_amount=amount,
#         cycle_duration_weeks=cycle_duration_weeks,
#         payout_order_type=payout_order_type,
#         next_payout_date=next_payout_date or None,
#         created_by=creator,
#         payout_frequency=payout_frequency,
#         payouts_per_period=payouts_per_period,
#         is_open=is_open,
#         max_seats=max_seats,
#         penalty_mode=penalty_mode,
#         flat_penalty_amount=flat_penalty_amount,
#         daily_penalty_amount=daily_penalty_amount,
#         penalty_grace_days=penalty_grace_days,
#         penalty_cap_amount=penalty_cap_amount,
#     )
#     merry.full_clean()
#     merry.save()
#     return merry
@transaction.atomic
def create_merry(
    *,
    creator,
    name: str,
    contribution_amount: Decimal,
    payout_order_type: str = "manual",
    next_payout_date=None,
    payout_frequency: str = "WEEKLY",
    payout_weekdays: Optional[List[int]] = None,
    is_open: bool = True,
    max_seats: int = 0,
    penalty_mode: str = "NONE",
    flat_penalty_amount: Decimal = Decimal("0.00"),
    daily_penalty_amount: Decimal = Decimal("0.00"),
    penalty_grace_days: int = 0,
    penalty_cap_amount: Optional[Decimal] = None,
) -> MerryGoRound:
    if not is_admin(creator):
        raise NotAllowed("Admin only.")

    name = (name or "").strip()
    if not name:
        raise BadState("name is required.")

    amount = parse_decimal(contribution_amount, "contribution_amount")
    if amount <= 0:
        raise BadState("contribution_amount must be > 0.")

    payout_order_type = (payout_order_type or "manual").strip().lower()
    if payout_order_type not in ("manual", "random"):
        raise BadState("payout_order_type must be 'manual' or 'random'.")

    payout_frequency = (payout_frequency or "WEEKLY").upper().strip()
    if payout_frequency not in ("DAILY", "WEEKLY", "MONTHLY"):
        raise BadState("payout_frequency must be 'DAILY', 'WEEKLY' or 'MONTHLY'.")

    if payout_weekdays is None:
        payout_weekdays = [0]

    if not isinstance(payout_weekdays, list) or not payout_weekdays:
        raise BadState("payout_weekdays must be a non-empty list.")

    normalized_weekdays = []
    for weekday in payout_weekdays:
        day = parse_int(weekday, "weekday", min_value=0, max_value=6)
        if day in normalized_weekdays:
            raise BadState("Duplicate payout weekday is not allowed.")
        normalized_weekdays.append(day)

    normalized_weekdays.sort()

    payouts_per_period = len(normalized_weekdays)

    is_open = parse_bool(is_open, default=True)
    max_seats = parse_int(max_seats or 0, "max_seats", min_value=0)

    penalty_mode = (penalty_mode or "NONE").upper().strip()
    if penalty_mode not in ("NONE", "FLAT", "DAILY"):
        raise BadState("penalty_mode must be NONE, FLAT or DAILY.")

    flat_penalty_amount = parse_decimal(flat_penalty_amount or 0, "flat_penalty_amount")
    daily_penalty_amount = parse_decimal(daily_penalty_amount or 0, "daily_penalty_amount")
    penalty_grace_days = parse_int(penalty_grace_days or 0, "penalty_grace_days", min_value=0)

    if penalty_cap_amount not in (None, ""):
        penalty_cap_amount = parse_decimal(penalty_cap_amount, "penalty_cap_amount")
    else:
        penalty_cap_amount = None

    cycle_duration_weeks = _calculate_cycle_duration_weeks(
        active_seats=0,
        payout_days_count=payouts_per_period,
    )

    merry = MerryGoRound.objects.create(
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
        penalty_mode=penalty_mode,
        flat_penalty_amount=flat_penalty_amount,
        daily_penalty_amount=daily_penalty_amount,
        penalty_grace_days=penalty_grace_days,
        penalty_cap_amount=penalty_cap_amount,
    )

    for index, weekday in enumerate(normalized_weekdays, start=1):
        MerrySlotConfig.objects.create(
            merry=merry,
            slot_no=index,
            weekday=weekday,
        )

    recalculate_merry_cycle_duration(merry=merry)

    merry.full_clean()
    merry.save()

    return merry


# @transaction.atomic
# def set_slot_config_bulk(*, admin_user, merry_id: int, items: List[dict]) -> List[MerrySlotConfig]:
#     if not is_admin(admin_user):
#         raise NotAllowed("Admin only.")

#     merry = get_merry(merry_id)

#     if not isinstance(items, list) or not items:
#         raise BadState("items must be a non-empty list.")

#     seen = set()
#     for it in items:
#         slot_no = parse_int(it.get("slot_no"), "slot_no", min_value=1)
#         weekday = parse_int(it.get("weekday"), "weekday", min_value=0, max_value=6)

#         validate_slot(merry, slot_no)

#         if slot_no in seen:
#             raise BadState("Duplicate slot_no in payload.")
#         seen.add(slot_no)

#     out: List[MerrySlotConfig] = []
#     for it in items:
#         slot_no = int(it["slot_no"])
#         weekday = int(it["weekday"])
#         obj, _ = MerrySlotConfig.objects.get_or_create(
#             merry=merry,
#             slot_no=slot_no,
#             defaults={"weekday": weekday},
#         )
#         if obj.weekday != weekday:
#             obj.weekday = weekday
#             obj.full_clean()
#             obj.save(update_fields=["weekday"])
#         out.append(obj)

#     return out
@transaction.atomic
def set_slot_config_bulk(*, admin_user, merry_id: int, items: List[dict]) -> List[MerrySlotConfig]:
    if not is_admin(admin_user):
        raise NotAllowed("Admin only.")

    merry = get_merry(merry_id)

    if not isinstance(items, list) or not items:
        raise BadState("items must be a non-empty list.")

    normalized_weekdays = []

    for it in items:
        weekday = parse_int(it.get("weekday"), "weekday", min_value=0, max_value=6)

        if weekday in normalized_weekdays:
            raise BadState("Duplicate payout weekday is not allowed.")

        normalized_weekdays.append(weekday)

    normalized_weekdays.sort()

    MerrySlotConfig.objects.filter(merry=merry).delete()

    out: List[MerrySlotConfig] = []

    for index, weekday in enumerate(normalized_weekdays, start=1):
        obj = MerrySlotConfig.objects.create(
            merry=merry,
            slot_no=index,
            weekday=weekday,
        )
        out.append(obj)

    recalculate_merry_cycle_duration(merry=merry)
    maybe_update_next_payout_date(merry=merry)

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

    recalculate_merry_cycle_duration(merry=merry)
    maybe_update_next_payout_date(merry=merry)

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

    # ✅ ADD THESE TWO LINES HERE
    recalculate_merry_cycle_duration(merry=merry)
    maybe_update_next_payout_date(merry=merry)

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

    payout = ensure_current_payout_exists(merry_id=merry.id)
    ensure_member_dues_up_to_current_turn(member=member)

    return MerryPayment.objects.create(
        merry=merry,
        beneficiary_member=member,
        initiated_by=user,
        payer_phone=payer_phone,
        period_key=payout.period_key,
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

    current_payout = ensure_current_payout_exists(merry_id=merry.id)
    ensure_member_dues_up_to_current_turn(member=member)

    if (payment.period_key or "").strip() != current_payout.period_key:
        payment.period_key = current_payout.period_key
        payment.save(update_fields=["period_key"])

    remaining = q2(payment.amount or Decimal("0.00"))
    if remaining <= 0:
        raise BadState("Payment amount must be > 0.")

    dues = list(
        MerryContributionDue.objects.select_for_update()
        .filter(
            merry=merry,
            seat__member=member,
            seat__is_active=True,
            status__in=["PENDING", "PARTIAL", "OVERDUE"],
        )
        .exclude(
            payout__isnull=True,
            due_date__isnull=True,
        )
        .select_related("seat")
        .order_by("due_date", "seat__seat_no", "id")
    )
    _refresh_penalties_for_queryset(dues)

    for due in dues:
        if remaining <= 0:
            break

        due_amount = q2(due.due_amount or Decimal("0.00"))
        paid_amount = q2(due.paid_amount or Decimal("0.00"))
        need = q2(due_amount - paid_amount)

        if need <= 0:
            continue

        alloc = q2(min(remaining, need))
        if alloc <= 0:
            continue

        allocation, _ = MerryPaymentAllocation.objects.get_or_create(
            payment=payment,
            due=due,
            defaults={"amount_allocated": Decimal("0.00")},
        )
        allocation.amount_allocated = q2(
            (allocation.amount_allocated or Decimal("0.00")) + alloc
        )
        allocation.full_clean()
        allocation.save(update_fields=["amount_allocated"])

        due.paid_amount = q2((due.paid_amount or Decimal("0.00")) + alloc)
        due.recalc_status()
        due.save(update_fields=["paid_amount", "status", "updated_at"])

        remaining = q2(remaining - alloc)

    if remaining > 0:
        add_merry_wallet_credit(
            user=member.user,
            amount=remaining,
            reference=f"PAY-{payment.id}",
            narration=(
                f"Excess merry payment saved to wallet after payment allocation "
                f"#{payment.id}."
            ),
            mpesa_receipt_number=payment.mpesa_receipt_number,
        )

    return payment

# -----------------------------
# Readiness helpers
# -----------------------------
def _slot_due_total(*, merry: MerryGoRound, period_key: str, slot_no: int) -> Decimal:
    payout = MerryPayout.objects.filter(merry=merry, period_key=period_key, slot_no=slot_no).first()
    if payout:
        dues = list(MerryContributionDue.objects.filter(merry=merry, payout=payout))
    else:
        dues = list(MerryContributionDue.objects.filter(merry=merry, period_key=period_key, slot_no=slot_no))

    _refresh_penalties_for_queryset(dues)
    total = sum((q2(d.due_amount or Decimal("0.00")) for d in dues), Decimal("0.00"))
    return q2(total)


def _slot_paid_total(*, merry: MerryGoRound, period_key: str, slot_no: int) -> Decimal:
    payout = MerryPayout.objects.filter(merry=merry, period_key=period_key, slot_no=slot_no).first()
    qs = MerryContributionDue.objects.filter(merry=merry, payout=payout) if payout else MerryContributionDue.objects.filter(
        merry=merry,
        period_key=period_key,
        slot_no=slot_no,
    )
    total = qs.aggregate(s=Sum("paid_amount")).get("s") or Decimal("0.00")
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
    payout = MerryPayout.objects.filter(merry=merry, period_key=period_key, slot_no=slot_no).first()

    dues = list(
        (MerryContributionDue.objects.filter(merry=merry, payout=payout) if payout else MerryContributionDue.objects.filter(
            merry=merry,
            period_key=period_key,
            slot_no=slot_no,
        ))
        .select_related("seat", "seat__member", "seat__member__user", "payout")
        .order_by("seat__seat_no", "id")
    )

    _refresh_penalties_for_queryset(dues)

    rows: List[Dict[str, Any]] = []
    for due in dues:
        user = due.seat.member.user
        outstanding = _outstanding_amount(due)

        rows.append({
            "due_id": due.id,
            "payout_id": getattr(due, "payout_id", None),
            "turn_no": getattr(due.payout, "turn_no", None) if getattr(due, "payout", None) else None,
            "seat_id": due.seat_id,
            "seat_no": due.seat.seat_no,
            "member_id": due.seat.member_id,
            "user_id": user.id,
            "username": getattr(user, "username", None),
            "phone": getattr(user, "phone", None),
            "base_amount": _base_amount_for_due(due),
            "penalty_amount": _penalty_amount_for_due(due),
            "due_amount": q2(due.due_amount or Decimal("0.00")),
            "paid_amount": q2(due.paid_amount or Decimal("0.00")),
            "outstanding": outstanding,
            "status": due.status,
            "due_date": due.due_date,
            "days_overdue": int(getattr(due, "days_overdue", 0) or 0),
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

    payout = ensure_current_payout_exists(merry_id=merry.id)
    ensure_dues_for_current_payout(merry_id=merry.id)

    due_total = _slot_due_total(merry=merry, period_key=payout.period_key, slot_no=1)
    paid_total = _slot_paid_total(merry=merry, period_key=payout.period_key, slot_no=1)
    outstanding_total = _slot_outstanding_total(merry=merry, period_key=payout.period_key, slot_no=1)
    ready_for_payout = _slot_is_ready_for_payout(merry=merry, period_key=payout.period_key, slot_no=1)
    period_meta = get_period_date_range(merry=merry, period_key=payout.period_key)

    next_turn = {
        "seat_id": payout.seat.id,
        "seat_no": payout.seat.seat_no,
        "member_id": payout.seat.member_id,
        "user_id": payout.seat.member.user_id,
        "username": getattr(payout.seat.member.user, "username", None),
        "payout_position": payout.seat.payout_position,
        "cycle_number": getattr(payout, "cycle_no", _current_cycle_number(merry)),
        "cycle_complete": _is_cycle_complete(merry),
        "turn_no": getattr(payout, "turn_no", 1),
        "scheduled_date": getattr(payout, "scheduled_date", None) or _period_key_to_date(payout.period_key),
    }

    rows = _slot_member_status_rows(merry=merry, period_key=payout.period_key, slot_no=1)

    return {
        "merry_id": merry.id,
        "merry_name": merry.name,
        "payout_id": payout.id,
        "turn_no": getattr(payout, "turn_no", 1),
        "cycle_no": getattr(payout, "cycle_no", _current_cycle_number(merry)),
        "period_key": payout.period_key,
        "period_label": period_meta["label"],
        "period_start_date": period_meta["start_date"],
        "period_end_date": period_meta["end_date"],
        "scheduled_date": getattr(payout, "scheduled_date", None) or _period_key_to_date(payout.period_key),
        "slot_no": 1,
        "due_total": due_total,
        "paid_total": paid_total,
        "outstanding_total": outstanding_total,
        "ready_for_payout": ready_for_payout,
        "payout_already_exists": True,
        "can_admin_create_payout": False,
        "next_turn": next_turn,
        "rows": rows,
    }


@transaction.atomic
def compute_payout_amount_for_slot(
    *,
    merry_id: int,
    period_key: Optional[str] = None,
    slot_no: int = 1
) -> Decimal:
    merry = get_merry(merry_id)

    payout = ensure_current_payout_exists(merry_id=merry.id)
    pk = (period_key or "").strip() or payout.period_key

    if slot_no != 1:
        raise BadState("Only slot_no=1 is supported in current payout model.")

    payout_row = MerryPayout.objects.filter(merry=merry, period_key=pk, slot_no=1).first()
    dues = list(
        (MerryContributionDue.objects.filter(merry=merry, payout=payout_row) if payout_row else MerryContributionDue.objects.filter(
            merry=merry,
            period_key=pk,
            slot_no=1,
        ))
    )
    _refresh_penalties_for_queryset(dues)

    total = sum((q2(d.paid_amount or Decimal("0.00")) for d in dues), Decimal("0.00"))
    return q2(total)


@transaction.atomic
def get_next_payout_turn(*, merry_id: int) -> Dict[str, Any]:
    merry = MerryGoRound.objects.select_for_update().filter(id=merry_id).first()
    if not merry:
        raise NotFound("Merry not found.")

    payout = ensure_current_payout_exists(merry_id=merry.id)
    seat = payout.seat
    period_meta = get_period_date_range(merry=merry, period_key=payout.period_key)
    due_date = getattr(payout, "scheduled_date", None) or _period_key_to_date(payout.period_key)

    return {
        "merry_id": merry.id,
        "merry_name": merry.name,
        "payout_id": payout.id,
        "turn_no": getattr(payout, "turn_no", 1),
        "cycle_no": getattr(payout, "cycle_no", _current_cycle_number(merry)),
        "seat_id": seat.id,
        "seat_no": seat.seat_no,
        "member_id": seat.member_id,
        "user_id": seat.member.user_id,
        "username": getattr(seat.member.user, "username", None),
        "payout_position": seat.payout_position,
        "period_key": payout.period_key,
        "period_label": period_meta["label"],
        "period_start_date": period_meta["start_date"],
        "period_end_date": period_meta["end_date"],
        "slot_no": 1,
        "due_date": due_date,
        "scheduled_date": due_date,
        "cycle_number": getattr(payout, "cycle_no", _current_cycle_number(merry)),
        "cycle_complete": _is_cycle_complete(merry),
        "expected_amount": q2(
            payout.amount
            if getattr(payout, "amount", None) is not None
            else (_expected_pool_amount(merry))
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

    payout = ensure_current_payout_exists(merry_id=merry_id)

    updates = []
    if amount is not None and amount != "":
        payout.amount = parse_decimal(amount, "amount")
        updates.append("amount")
    if notes is not None:
        payout.notes = (notes or payout.notes or "").strip()[:255]
        updates.append("notes")
    if updates:
        payout.save(update_fields=updates)

    return payout


@transaction.atomic
def create_next_cycle_payout_record(
    *,
    admin_user,
    merry_id: int,
    notes: str = "",
) -> MerryPayout:
    if not is_admin(admin_user):
        raise NotAllowed("Admin only.")
    payout = ensure_current_payout_exists(merry_id=merry_id)
    if notes:
        payout.notes = (notes or payout.notes or "").strip()[:255]
        payout.save(update_fields=["notes"])
    return payout


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

    ensure_dues_for_current_payout(merry_id=p.merry_id)
    if not _slot_is_ready_for_payout(merry=p.merry, period_key=p.period_key, slot_no=1):
        raise BadState("Current payout is not fully funded yet.")

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

    ensure_current_payout_exists(merry_id=p.merry_id)
    maybe_update_next_payout_date(merry=p.merry)
    return p


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

    with transaction.atomic():
        ensure_member_dues_up_to_current_turn(member=member)

    dues = list(
        MerryContributionDue.objects.filter(
            merry=merry,
            seat__member=member,
            seat__is_active=True,
        )
        .exclude(status__in=["PAID", "CANCELLED"])
        .select_related("seat", "payout")
        .order_by("due_date", "seat__seat_no", "id")
    )
    _refresh_penalties_for_queryset(dues)
    return dues


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

        with transaction.atomic():
            payout = ensure_current_payout_exists(merry_id=merry.id)
            ensure_member_dues_up_to_current_turn(member=membership)

        seats = membership.seats.filter(is_active=True).order_by("seat_no", "id")
        seat_numbers = list(seats.values_list("seat_no", flat=True))
        seat_ids = list(seats.values_list("id", flat=True))

        dues = list(
            MerryContributionDue.objects
            .filter(
                merry=merry,
                seat_id__in=seat_ids,
            )
            .exclude(status__in=["PAID", "CANCELLED"])
            .select_related("seat", "payout")
            .order_by("due_date", "seat__seat_no", "id")
        )

        _refresh_penalties_for_queryset(dues)

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
            elif bucket == "future":
                next_total += outstanding
                if next_due_date is None or (due.due_date and due.due_date < next_due_date):
                    next_due_date = due.due_date

        if next_due_date:
            next_due_rows = [d for d in dues if d.due_date == next_due_date and _outstanding_amount(d) > 0]

        breakdown_rows = []
        for due in dues:
            outstanding = _outstanding_amount(due)
            if outstanding <= 0:
                continue
            breakdown_rows.append({
                "due_id": due.id,
                "payout_id": getattr(due, "payout_id", None),
                "turn_no": getattr(due.payout, "turn_no", None) if getattr(due, "payout", None) else None,
                "cycle_no": getattr(due.payout, "cycle_no", None) if getattr(due, "payout", None) else None,
                "seat_no": due.seat.seat_no,
                "period_key": due.period_key,
                "base_amount": _base_amount_for_due(due),
                "penalty_amount": _penalty_amount_for_due(due),
                "due_amount": q2(due.due_amount or Decimal("0.00")),
                "paid_amount": q2(due.paid_amount or Decimal("0.00")),
                "outstanding": outstanding,
                "status": due.status,
                "due_date": due.due_date,
                "days_overdue": int(getattr(due, "days_overdue", 0) or 0),
                "bucket": _due_bucket(due, today=today),
            })

        current_turn = {
            "payout_id": payout.id,
            "turn_no": getattr(payout, "turn_no", 1),
            "cycle_no": getattr(payout, "cycle_no", _current_cycle_number(merry)),
            "seat_no": payout.seat.seat_no,
            "scheduled_date": getattr(payout, "scheduled_date", None) or _period_key_to_date(payout.period_key),
            "expected_amount": q2(payout.amount or Decimal("0.00")),
            "period_key": payout.period_key,
        }

        summary_items.append({
            "merry_id": merry.id,
            "merry_name": merry.name,
            "seat_numbers": seat_numbers,
            "seat_count": len(seat_numbers),
            "amount_per_seat": q2(merry.contribution_amount or Decimal("0.00")),
            "current_turn": current_turn,
            "overdue_total": q2(overdue_total),
            "current_total": q2(current_total),
            "next_total": q2(next_total),
            "total_due_now": q2(overdue_total + current_total),
            "next_due_date": next_due_date,
            "next_due_rows_count": len(next_due_rows),
            "wallet_balance": get_user_merry_wallet_balance(user=user),
            "breakdown": breakdown_rows,
        })

        grand_overdue += overdue_total
        grand_current += current_total
        grand_next += next_total

    return {
        "active_merries": len(summary_items),
        "total_seats": total_seats,
        "overdue_total": q2(grand_overdue),
        "current_total": q2(grand_current),
        "next_total": q2(grand_next),
        "total_due_now": q2(grand_overdue + grand_current),
        "wallet_balance": get_user_merry_wallet_balance(user=user),
        "items": summary_items,
    }


def get_member_merry_dashboard(*, user, merry_id: int) -> Dict[str, Any]:
    merry = get_merry(merry_id)
    member = get_active_member(merry, user)

    ensure_member_dues_up_to_current_turn(member=member)
    payout = ensure_current_payout_exists(merry_id=merry.id)
    dues = _select_member_dues_for_breakdown(member=member, include_next=True)

    overdue_rows = [d for d in dues if _due_bucket(d) == "overdue" and _outstanding_amount(d) > 0]
    current_rows = [d for d in dues if _due_bucket(d) == "current" and _outstanding_amount(d) > 0]
    future_rows = [d for d in dues if _due_bucket(d) == "future" and _outstanding_amount(d) > 0]

    def row(due: MerryContributionDue) -> Dict[str, Any]:
        return {
            "due_id": due.id,
            "payout_id": getattr(due, "payout_id", None),
            "turn_no": getattr(due.payout, "turn_no", None) if getattr(due, "payout", None) else None,
            "cycle_no": getattr(due.payout, "cycle_no", None) if getattr(due, "payout", None) else None,
            "seat_no": due.seat.seat_no,
            "period_key": due.period_key,
            "base_amount": _base_amount_for_due(due),
            "penalty_amount": _penalty_amount_for_due(due),
            "due_amount": q2(due.due_amount or Decimal("0.00")),
            "paid_amount": q2(due.paid_amount or Decimal("0.00")),
            "outstanding": _outstanding_amount(due),
            "status": due.status,
            "due_date": due.due_date,
            "days_overdue": int(getattr(due, "days_overdue", 0) or 0),
        }

    return {
        "merry_id": merry.id,
        "merry_name": merry.name,
        "member_id": member.id,
        "seat_numbers": list(member.seats.filter(is_active=True).values_list("seat_no", flat=True)),
        "wallet_balance": get_user_merry_wallet_balance(user=user),
        "current_turn": {
            "payout_id": payout.id,
            "turn_no": getattr(payout, "turn_no", 1),
            "cycle_no": getattr(payout, "cycle_no", _current_cycle_number(merry)),
            "seat_no": payout.seat.seat_no,
            "scheduled_date": getattr(payout, "scheduled_date", None) or _period_key_to_date(payout.period_key),
            "expected_amount": q2(payout.amount or Decimal("0.00")),
        },
        "totals": {
            "overdue_total": q2(sum((_outstanding_amount(d) for d in overdue_rows), Decimal("0.00"))),
            "current_total": q2(sum((_outstanding_amount(d) for d in current_rows), Decimal("0.00"))),
            "future_total": q2(sum((_outstanding_amount(d) for d in future_rows), Decimal("0.00"))),
        },
        "overdue_rows": [row(d) for d in overdue_rows],
        "current_rows": [row(d) for d in current_rows],
        "future_rows": [row(d) for d in future_rows],
    }


def get_merry_detail(*, merry_id: int, user=None) -> Dict[str, Any]:
    merry = get_merry(merry_id)
    payout = ensure_current_payout_exists(merry_id=merry.id)
    readiness = get_payout_readiness_status(merry_id=merry.id)

    data = {
        "id": merry.id,
        "name": merry.name,
        "contribution_amount": q2(merry.contribution_amount or Decimal("0.00")),
        "payout_frequency": merry.payout_frequency,
        "cycle_duration_weeks": merry.cycle_duration_weeks,
        "payout_order_type": merry.payout_order_type,
        "is_open": merry.is_open,
        "max_seats": merry.max_seats,
        "active_seats": MerrySeat.objects.filter(merry=merry, is_active=True).count(),
        "penalty_mode": getattr(merry, "penalty_mode", "NONE"),
        "flat_penalty_amount": q2(getattr(merry, "flat_penalty_amount", Decimal("0.00")) or Decimal("0.00")),
        "daily_penalty_amount": q2(getattr(merry, "daily_penalty_amount", Decimal("0.00")) or Decimal("0.00")),
        "penalty_grace_days": int(getattr(merry, "penalty_grace_days", 0) or 0),
        "penalty_cap_amount": getattr(merry, "penalty_cap_amount", None),
        "next_turn": {
            "payout_id": payout.id,
            "turn_no": getattr(payout, "turn_no", 1),
            "cycle_no": getattr(payout, "cycle_no", _current_cycle_number(merry)),
            "seat_no": payout.seat.seat_no,
            "scheduled_date": getattr(payout, "scheduled_date", None) or _period_key_to_date(payout.period_key),
            "expected_amount": q2(payout.amount or Decimal("0.00")),
        },
        "readiness": readiness,
    }

    if user and getattr(user, "id", None):
        try:
            member = get_active_member(merry, user)
            data["member_dashboard"] = get_member_merry_dashboard(user=user, merry_id=merry.id)
            data["is_member"] = True
            data["member_id"] = member.id
        except Exception:
            data["is_member"] = False

    return data
