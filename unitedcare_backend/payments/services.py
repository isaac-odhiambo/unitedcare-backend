from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional, Tuple

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .balances import get_user_balance
from .models import (
    MpesaConfig,
    MpesaTransaction,
    PaymentLedger,
    TransactionFeeConfig,
    WithdrawalRequest,
)

UserModel = get_user_model()

# ============================================================
# Constants
# ============================================================
DECIMAL_2 = Decimal("0.01")


# ============================================================
# Helpers
# ============================================================
def normalize_phone(phone: str) -> str:
    p = (phone or "").strip().replace(" ", "").replace("-", "")
    if p.startswith("+254"):
        p = "0" + p[4:]
    elif p.startswith("254"):
        p = "0" + p[3:]
    return p


def _safe_decimal(v: Any) -> Decimal:
    try:
        d = Decimal(str(v))
        return d if d.is_finite() else Decimal("0")
    except Exception:
        return Decimal("0")


def _money(v: Any) -> Decimal:
    return _safe_decimal(v).quantize(DECIMAL_2, rounding=ROUND_HALF_UP)


def _set_generic_target(obj: Optional[object]) -> Tuple[Optional[ContentType], Optional[int]]:
    if not obj:
        return None, None
    return ContentType.objects.get_for_model(obj.__class__), int(obj.pk)


def _purpose_to_ledger_category(purpose: str) -> str:
    mapping = {
        "SAVINGS_DEPOSIT": "SAVINGS",
        "LOAN_REPAYMENT": "LOANS",
        "MERRY_CONTRIBUTION": "MERRY",
        "GROUP_CONTRIBUTION": "GROUP",
        "WITHDRAWAL": "WITHDRAWAL",
        "LOAN_DISBURSEMENT": "LOANS",
    }
    return mapping.get((purpose or "").upper(), "OTHER")


def _withdrawal_source_to_category(source: str) -> str:
    s = (source or "").upper()
    mapping = {"SAVINGS": "SAVINGS", "MERRY": "MERRY", "GROUP": "GROUP"}
    return mapping.get(s, "SAVINGS")


def _normalize_reference_token(reference: str) -> str:
    """
    Normalized version for parsing:
    - strip spaces
    - remove hyphens/underscores
    - uppercase
    """
    ref = (reference or "").strip().upper()
    ref = ref.replace(" ", "").replace("-", "").replace("_", "")
    return ref


def get_active_mpesa_config() -> Optional[MpesaConfig]:
    return (
        MpesaConfig.objects.filter(is_active=True)
        .order_by("-updated_at", "-id")
        .first()
    )


@dataclass
class ParsedReference:
    raw: str
    normalized: str
    kind: str
    entity_id: Optional[int]
    purpose: str
    valid: bool
    matched_reference_type: str


def _parse_reference(reference: str) -> ParsedReference:
    """
    Supported reference styles:

    SIMPLE / PRIMARY
    - mus11      => merry contribution for USER id 11
    - saving23   => savings deposit for USER id 23
    - sav23      => savings deposit for USER id 23
    - loan35     => loan repayment for USER id 35
    - grp9       => group contribution for GROUP id 9
    - group9     => group contribution for GROUP id 9

    LEGACY
    - MERRY-PAYMENT-99
    - LOAN-12
    - GROUP-7
    """
    raw = (reference or "").strip()
    norm = _normalize_reference_token(raw)

    if not raw:
        return ParsedReference(
            raw=raw,
            normalized=norm,
            kind="EMPTY",
            entity_id=None,
            purpose="OTHER",
            valid=False,
            matched_reference_type="UNKNOWN",
        )

    m = re.match(r"^MUS(\d+)$", norm)
    if m:
        return ParsedReference(
            raw=raw,
            normalized=norm,
            kind="MERRY_USER",
            entity_id=int(m.group(1)),
            purpose="MERRY_CONTRIBUTION",
            valid=True,
            matched_reference_type="MERRY",
        )

    m = re.match(r"^SAVING(\d+)$", norm)
    if m:
        return ParsedReference(
            raw=raw,
            normalized=norm,
            kind="SAVINGS_USER",
            entity_id=int(m.group(1)),
            purpose="SAVINGS_DEPOSIT",
            valid=True,
            matched_reference_type="SAVINGS",
        )

    m = re.match(r"^SAV(\d+)$", norm)
    if m:
        return ParsedReference(
            raw=raw,
            normalized=norm,
            kind="SAVINGS_USER",
            entity_id=int(m.group(1)),
            purpose="SAVINGS_DEPOSIT",
            valid=True,
            matched_reference_type="SAVINGS",
        )

    m = re.match(r"^LOAN(\d+)$", norm)
    if m:
        return ParsedReference(
            raw=raw,
            normalized=norm,
            kind="LOAN_USER",
            entity_id=int(m.group(1)),
            purpose="LOAN_REPAYMENT",
            valid=True,
            matched_reference_type="LOAN",
        )

    m = re.match(r"^GRP(\d+)$", norm)
    if m:
        return ParsedReference(
            raw=raw,
            normalized=norm,
            kind="GROUP",
            entity_id=int(m.group(1)),
            purpose="GROUP_CONTRIBUTION",
            valid=True,
            matched_reference_type="GROUP",
        )

    m = re.match(r"^GROUP(\d+)$", norm)
    if m:
        return ParsedReference(
            raw=raw,
            normalized=norm,
            kind="GROUP",
            entity_id=int(m.group(1)),
            purpose="GROUP_CONTRIBUTION",
            valid=True,
            matched_reference_type="GROUP",
        )

    # -------------------------
    # Legacy references
    # -------------------------
    m = re.match(r"^MERRYPAYMENT(\d+)$", norm)
    if m:
        return ParsedReference(
            raw=raw,
            normalized=norm,
            kind="MERRY_PAYMENT",
            entity_id=int(m.group(1)),
            purpose="MERRY_CONTRIBUTION",
            valid=True,
            matched_reference_type="MERRY",
        )

    m = re.match(r"^LOAN(\d+)$", norm)
    if m:
        return ParsedReference(
            raw=raw,
            normalized=norm,
            kind="LOAN_USER",
            entity_id=int(m.group(1)),
            purpose="LOAN_REPAYMENT",
            valid=True,
            matched_reference_type="LOAN",
        )

    m = re.match(r"^GROUP(\d+)$", norm)
    if m:
        return ParsedReference(
            raw=raw,
            normalized=norm,
            kind="GROUP",
            entity_id=int(m.group(1)),
            purpose="GROUP_CONTRIBUTION",
            valid=True,
            matched_reference_type="GROUP",
        )

    return ParsedReference(
        raw=raw,
        normalized=norm,
        kind="UNKNOWN",
        entity_id=None,
        purpose="OTHER",
        valid=False,
        matched_reference_type="UNKNOWN",
    )


def _extract_id(reference: str, prefix: str) -> Optional[int]:
    ref = (reference or "").strip()
    if not ref.startswith(prefix):
        return None
    try:
        return int(ref.replace(prefix, "").strip())
    except Exception:
        return None


def _create_mpesa_tx(**kwargs) -> MpesaTransaction:
    model_fields = {f.name for f in MpesaTransaction._meta.get_fields()}
    clean = {k: v for k, v in kwargs.items() if k in model_fields}
    return MpesaTransaction.objects.create(**clean)


def _update_tx_allocation(
    tx: MpesaTransaction,
    *,
    status: str,
    notes: str = "",
    allocated_by: Optional[AbstractBaseUser] = None,
) -> None:
    tx.allocation_status = status
    tx.allocation_notes = (notes or "")[:255]

    update_fields = ["allocation_status", "allocation_notes"]

    if status in ("AUTO_ALLOCATED", "MANUALLY_ALLOCATED", "PARTIALLY_ALLOCATED"):
        tx.allocated_at = timezone.now()
        update_fields.append("allocated_at")

    if allocated_by is not None:
        tx.allocated_by = allocated_by
        update_fields.append("allocated_by")

    tx.save(update_fields=update_fields)


def _mark_callback_received(tx: MpesaTransaction, *, callback_payload: Dict[str, Any]) -> None:
    tx.callback_payload = callback_payload
    tx.callback_received_at = timezone.now()
    tx.updated_at = timezone.now()
    tx.save(update_fields=["callback_payload", "callback_received_at", "updated_at"])


def _extract_stk_metadata_items(stk: Dict[str, Any]) -> Dict[str, Any]:
    """
    Safely flattens STK CallbackMetadata.Item list into a dict.

    Example source:
    [
        {"Name": "Amount", "Value": 2},
        {"Name": "MpesaReceiptNumber", "Value": "UCMI2A8PWR"},
        {"Name": "Balance"},
        {"Name": "TransactionDate", "Value": 20260322130923},
        {"Name": "PhoneNumber", "Value": 254701956902},
    ]
    """
    items = ((stk or {}).get("CallbackMetadata") or {}).get("Item") or []
    parsed: Dict[str, Any] = {}

    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("Name")
        if not name:
            continue
        parsed[name] = item.get("Value")

    return parsed


# ============================================================
# Central Fee Logic
# ============================================================
def get_fee_config(purpose: str) -> Optional[TransactionFeeConfig]:
    purpose_u = (purpose or "").upper().strip()
    if not purpose_u:
        return None

    return (
        TransactionFeeConfig.objects.filter(purpose=purpose_u, is_active=True)
        .order_by("-updated_at", "-id")
        .first()
    )


def calculate_transaction_fee(*, purpose: str, base_amount: Decimal) -> Decimal:
    amount = _money(base_amount)
    if amount <= Decimal("0"):
        return Decimal("0.00")

    cfg = get_fee_config(purpose)
    if not cfg:
        return Decimal("0.00")

    fixed_fee = _money(getattr(cfg, "fixed_fee", Decimal("0.00")))
    pct = _safe_decimal(getattr(cfg, "percentage_fee", Decimal("0.00")))

    percentage_part = Decimal("0.00")
    if pct > 0:
        percentage_part = (amount * pct / Decimal("100")).quantize(
            DECIMAL_2, rounding=ROUND_HALF_UP
        )

    return (fixed_fee + percentage_part).quantize(DECIMAL_2, rounding=ROUND_HALF_UP)


def calculate_total_charge(*, purpose: str, base_amount: Decimal) -> Decimal:
    amount = _money(base_amount)
    fee = calculate_transaction_fee(purpose=purpose, base_amount=amount)
    return (amount + fee).quantize(DECIMAL_2, rounding=ROUND_HALF_UP)


def split_incoming_total_amount(*, purpose: str, total_amount: Decimal) -> Tuple[Decimal, Decimal]:
    """
    Reverse fee calculation for manual paybill/C2B where callback only gives the
    final total amount paid.

    Returns:
      (base_amount, fee)

    If no fee config exists:
      base_amount = total_amount
      fee = 0
    """
    total = _money(total_amount)
    if total <= Decimal("0.00"):
        return Decimal("0.00"), Decimal("0.00")

    cfg = get_fee_config(purpose)
    if not cfg:
        return total, Decimal("0.00")

    fixed_fee = _money(getattr(cfg, "fixed_fee", Decimal("0.00")))
    pct = _safe_decimal(getattr(cfg, "percentage_fee", Decimal("0.00")))

    if pct <= Decimal("0.00"):
        base = (total - fixed_fee).quantize(DECIMAL_2, rounding=ROUND_HALF_UP)
        if base < Decimal("0.00"):
            base = Decimal("0.00")
        fee = (total - base).quantize(DECIMAL_2, rounding=ROUND_HALF_UP)
        return base, fee

    divisor = Decimal("1.00") + (pct / Decimal("100"))
    base = ((total - fixed_fee) / divisor).quantize(DECIMAL_2, rounding=ROUND_HALF_UP)
    if base < Decimal("0.00"):
        base = Decimal("0.00")

    fee = (total - base).quantize(DECIMAL_2, rounding=ROUND_HALF_UP)
    if fee < Decimal("0.00"):
        fee = Decimal("0.00")

    return base, fee


# ============================================================
# Group membership guard for GROUP_CONTRIBUTION
# ============================================================
def _require_active_group_membership(*, user: AbstractBaseUser, group_id: int) -> None:
    try:
        from groups.models import GroupMembership
    except Exception:
        return

    is_member = GroupMembership.objects.filter(
        group_id=group_id,
        user_id=user.id,
        is_active=True,
    ).exists()

    if not is_member:
        raise ValidationError("You must be an active member of this group to contribute.")


def create_ledger_entry(
    *,
    user: AbstractBaseUser,
    entry_type: str,
    category: str,
    amount: Decimal,
    narration: str = "",
    reference: str = "",
    mpesa_tx: Optional[MpesaTransaction] = None,
    target_object: Optional[object] = None,
) -> PaymentLedger:
    ct, oid = _set_generic_target(target_object)
    return PaymentLedger.objects.create(
        user=user,
        entry_type=entry_type,
        category=category,
        amount=_money(amount),
        narration=narration or "",
        reference=reference or "",
        mpesa_tx=mpesa_tx,
        target_content_type=ct,
        target_object_id=oid,
        created_at=timezone.now(),
    )


# ============================================================
# Daraja Client
# ============================================================
@dataclass
class STKPushResult:
    merchant_request_id: str
    checkout_request_id: str
    customer_message: str = ""


@dataclass
class B2CResult:
    conversation_id: str
    originator_conversation_id: str = ""
    response_description: str = ""


class DarajaClient:
    def stk_push(
        self,
        *,
        phone: str,
        amount: Decimal,
        account_reference: str,
        transaction_desc: str,
        callback_url: str,
    ) -> STKPushResult:
        raise NotImplementedError("Connect your Daraja STK push here")

    def stk_query(self, *, checkout_request_id: str) -> Dict[str, Any]:
        raise NotImplementedError("Connect your Daraja STK query here")

    def b2c_payout(
        self,
        *,
        phone: str,
        amount: Decimal,
        remarks: str,
        occasion: str,
        result_url: str,
        timeout_url: str,
    ) -> B2CResult:
        raise NotImplementedError("Connect your Daraja B2C payout here")


def get_daraja_client():
    from .daraja import DarajaClient as RealClient
    return RealClient()


def _build_callback_url(path: str) -> str:
    base = getattr(settings, "MPESA_CALLBACK_BASE_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("MPESA_CALLBACK_BASE_URL missing in settings")

    token = getattr(settings, "MPESA_CALLBACK_TOKEN", "")
    url = f"{base}{path}"
    if token:
        url += f"?token={token}"
    return url


# ============================================================
# STK VERIFICATION
# ============================================================
def _stk_query_is_success(data: Dict[str, Any]) -> bool:
    rc = data.get("ResultCode")
    if rc is None:
        return False
    return str(rc) == "0"


def _verify_stk_with_query(client: DarajaClient, tx: MpesaTransaction) -> Dict[str, Any]:
    if not tx.checkout_request_id:
        raise ValueError("Cannot verify STK without checkout_request_id")
    return client.stk_query(checkout_request_id=tx.checkout_request_id)


# ============================================================
# BUSINESS ROUTING (after SUCCESS)
# ============================================================
@transaction.atomic
def _apply_merry_contribution(tx: MpesaTransaction) -> None:
    """
    IMPORTANT:
    - mus11 means merry allocation for USER id 11
    - reference is authoritative
    - phone mismatch should not block allocation
    """
    parsed = _parse_reference(tx.reference or "")

    if parsed.valid and parsed.kind == "MERRY_USER" and parsed.entity_id:
        try:
            from merry import services as merry_services
        except Exception:
            _update_tx_allocation(
                tx,
                status="MANUAL_REVIEW",
                notes="Merry services unavailable for mus reference allocation.",
            )
            return

        payload = tx.request_payload if isinstance(tx.request_payload, dict) else {}
        base_amount = _money(payload.get("base_amount", tx.base_amount or tx.amount))

        fn = getattr(merry_services, "apply_mpesa_contribution_by_user_reference", None)
        if callable(fn):
            try:
                fn(
                    user_id=parsed.entity_id,
                    amount=base_amount,
                    mpesa_tx=tx,
                    reference=tx.reference or "",
                )

                note = "Merry contribution auto-allocated by user reference."
                if tx.user_id and tx.user_id != parsed.entity_id:
                    note = "Allocated by merry user reference; payer phone/user differs from beneficiary."

                _update_tx_allocation(
                    tx,
                    status="AUTO_ALLOCATED",
                    notes=note,
                )
                return
            except Exception as e:
                _update_tx_allocation(
                    tx,
                    status="MANUAL_REVIEW",
                    notes=f"Merry allocation error: {str(e)}"[:255],
                )
                return

        fallback_names = (
            "apply_mpesa_contribution_by_user",
            "apply_mpesa_contribution",
        )

        for fn_name in fallback_names:
            fallback = getattr(merry_services, fn_name, None)
            if not callable(fallback):
                continue

            try:
                if fn_name == "apply_mpesa_contribution_by_user":
                    fallback(
                        user_id=parsed.entity_id,
                        amount=base_amount,
                        mpesa_tx=tx,
                        reference=tx.reference or "",
                    )
                else:
                    beneficiary_user = UserModel.objects.filter(id=parsed.entity_id).first()
                    if not beneficiary_user:
                        raise ValueError("Beneficiary user not found for merry reference.")
                    fallback(
                        user=beneficiary_user,
                        amount=base_amount,
                        mpesa_tx=tx,
                        reference=tx.reference or "",
                    )

                _update_tx_allocation(
                    tx,
                    status="AUTO_ALLOCATED",
                    notes="Merry contribution auto-allocated by fallback user reference flow.",
                )
                return
            except Exception as e:
                _update_tx_allocation(
                    tx,
                    status="MANUAL_REVIEW",
                    notes=f"Merry allocation error: {str(e)}"[:255],
                )
                return

        _update_tx_allocation(
            tx,
            status="MANUAL_REVIEW",
            notes="No supported merry allocation function found.",
        )
        return

    # --------------------------------------------
    # Legacy style: MERRY-PAYMENT-99
    # --------------------------------------------
    if parsed.valid and parsed.kind == "MERRY_PAYMENT" and parsed.entity_id:
        payment_id = parsed.entity_id
    else:
        payment_id = _extract_id(tx.reference or "", "MERRY-PAYMENT-")

    if not payment_id:
        _update_tx_allocation(
            tx,
            status="INVALID_REFERENCE",
            notes="Merry reference not mapped automatically.",
        )
        return

    try:
        from merry.models import MerryPayment
        from merry.views import allocate_payment
    except Exception:
        _update_tx_allocation(
            tx,
            status="MANUAL_REVIEW",
            notes="Legacy merry payment allocation service unavailable.",
        )
        return

    pay = MerryPayment.objects.select_for_update().filter(id=payment_id).first()
    if not pay:
        _update_tx_allocation(
            tx,
            status="MANUAL_REVIEW",
            notes="Legacy merry payment record not found.",
        )
        return

    if pay.status == "CONFIRMED":
        _update_tx_allocation(
            tx,
            status="AUTO_ALLOCATED",
            notes="Legacy merry payment already confirmed.",
        )
        return

    pay.status = "CONFIRMED"
    pay.paid_at = timezone.now()
    if tx.mpesa_receipt_number and not pay.mpesa_receipt_number:
        pay.mpesa_receipt_number = tx.mpesa_receipt_number
        pay.save(update_fields=["status", "paid_at", "mpesa_receipt_number"])
    else:
        pay.save(update_fields=["status", "paid_at"])

    allocate_payment(pay.id)
    _update_tx_allocation(
        tx,
        status="AUTO_ALLOCATED",
        notes="Legacy merry payment confirmed and allocated.",
    )


@transaction.atomic
def _apply_loan_repayment(tx: MpesaTransaction) -> None:
    """
    Canonical loan repayment allocation.

    Rule:
      loan19 => LOAN_REPAYMENT for borrower USER id 19

    Important:
    - The numeric part of loan reference is always treated as borrower user id
    - It must NOT be treated as loan record id
    - Borrower is expected to have only one active repayable loan
    """
    parsed = _parse_reference(tx.reference or "")
    borrower_user_id = (
        parsed.entity_id
        if parsed.valid and parsed.kind == "LOAN_USER"
        else _extract_id(tx.reference or "", "LOAN-")
    )

    if not borrower_user_id:
        _update_tx_allocation(
            tx,
            status="INVALID_REFERENCE",
            notes="Invalid loan reference.",
        )
        return

    try:
        from loans import services as loan_services
    except Exception:
        _update_tx_allocation(
            tx,
            status="MANUAL_REVIEW",
            notes="Loan services unavailable.",
        )
        return

    payload = tx.request_payload if isinstance(tx.request_payload, dict) else {}
    base_amount = _money(payload.get("base_amount", tx.base_amount or tx.amount))

    preferred_names = (
        "apply_mpesa_repayment_by_user_reference",
        "apply_mpesa_repayment_by_user_id",
        "apply_mpesa_repayment_by_user",
    )

    last_error = ""

    for fn_name in preferred_names:
        fn = getattr(loan_services, fn_name, None)
        if not callable(fn):
            continue

        try:
            if fn_name == "apply_mpesa_repayment_by_user":
                borrower_user = UserModel.objects.filter(id=borrower_user_id).first()
                if not borrower_user:
                    raise ValueError("Borrower user not found for loan reference.")

                fn(
                    user=borrower_user,
                    amount=base_amount,
                    mpesa_tx=tx,
                    reference=tx.reference or "",
                )
            else:
                fn(
                    user_id=borrower_user_id,
                    amount=base_amount,
                    mpesa_tx=tx,
                    reference=tx.reference or "",
                )

            note = "Loan repayment auto-applied by borrower user reference."
            if tx.user_id and tx.user_id != borrower_user_id:
                note = (
                    "Allocated by loan borrower reference; "
                    "payer phone/user differs from beneficiary."
                )

            _update_tx_allocation(
                tx,
                status="AUTO_ALLOCATED",
                notes=note,
            )
            return

        except Exception as e:
            last_error = str(e)

    _update_tx_allocation(
        tx,
        status="MANUAL_REVIEW",
        notes=(
            f"Loan allocation error: {last_error}"[:255]
            if last_error
            else "Loan repayment function not found."
        ),
    )

@transaction.atomic
def _apply_savings_deposit(tx: MpesaTransaction) -> None:
    """
    saving19 / sav19 => USER id 19
    """
    parsed = _parse_reference(tx.reference or "")

    try:
        from savings import services as savings_services
    except Exception:
        _update_tx_allocation(tx, status="MANUAL_REVIEW", notes="Savings services unavailable.")
        return

    payload = tx.request_payload if isinstance(tx.request_payload, dict) else {}
    base_amount = _money(payload.get("base_amount", tx.base_amount or tx.amount))

    if parsed.valid and parsed.kind == "SAVINGS_USER" and parsed.entity_id:
        preferred_names = (
            "apply_mpesa_deposit_by_user_reference",
            "apply_mpesa_deposit_by_user_id",
            "apply_mpesa_deposit_by_user",
        )

        for fn_name in preferred_names:
            fn = getattr(savings_services, fn_name, None)
            if not callable(fn):
                continue
            try:
                if fn_name == "apply_mpesa_deposit_by_user":
                    beneficiary_user = UserModel.objects.filter(id=parsed.entity_id).first()
                    if not beneficiary_user:
                        raise ValueError("Beneficiary user not found for savings reference.")
                    fn(
                        user=beneficiary_user,
                        amount=base_amount,
                        mpesa_tx=tx,
                        reference=tx.reference or "",
                    )
                else:
                    fn(
                        user_id=parsed.entity_id,
                        amount=base_amount,
                        mpesa_tx=tx,
                        reference=tx.reference or "",
                    )

                note = "Savings deposit auto-applied by user reference."
                if tx.user_id and tx.user_id != parsed.entity_id:
                    note = "Allocated by savings user reference; payer phone/user differs from beneficiary."

                _update_tx_allocation(
                    tx,
                    status="AUTO_ALLOCATED",
                    notes=note,
                )
                return
            except Exception as e:
                _update_tx_allocation(
                    tx,
                    status="MANUAL_REVIEW",
                    notes=f"Savings allocation error: {str(e)}"[:255],
                )
                return

    fn = getattr(savings_services, "apply_mpesa_deposit", None)
    if callable(fn):
        try:
            if not tx.user_id:
                raise ValueError("No user attached to transaction for fallback savings allocation.")
            fn(user=tx.user, amount=base_amount, mpesa_tx=tx, reference=tx.reference or "")
            _update_tx_allocation(tx, status="AUTO_ALLOCATED", notes="Savings deposit auto-applied.")
            return
        except Exception as e:
            _update_tx_allocation(
                tx,
                status="MANUAL_REVIEW",
                notes=f"Savings allocation error: {str(e)}"[:255],
            )
            return

    _update_tx_allocation(
        tx,
        status="MANUAL_REVIEW",
        notes="Savings deposit function not found.",
    )


@transaction.atomic
def _apply_group_contribution(tx: MpesaTransaction) -> None:
    try:
        from groups import services as group_services
    except Exception:
        _update_tx_allocation(tx, status="MANUAL_REVIEW", notes="Group services unavailable.")
        return

    fn = getattr(group_services, "apply_mpesa_contribution", None)
    if callable(fn):
        payload = tx.request_payload if isinstance(tx.request_payload, dict) else {}
        base_amount = _money(payload.get("base_amount", tx.base_amount or tx.amount))
        try:
            fn(user=tx.user, amount=base_amount, mpesa_tx=tx, reference=tx.reference or "")
            _update_tx_allocation(tx, status="AUTO_ALLOCATED", notes="Group contribution auto-applied.")
        except Exception as e:
            _update_tx_allocation(tx, status="MANUAL_REVIEW", notes=f"Group allocation error: {str(e)}"[:255])
    else:
        _update_tx_allocation(tx, status="MANUAL_REVIEW", notes="Group contribution function not found.")


def _route_success_tx(tx: MpesaTransaction) -> None:
    purpose = (tx.purpose or "").upper()

    if purpose == "MERRY_CONTRIBUTION":
        _apply_merry_contribution(tx)
    elif purpose == "LOAN_REPAYMENT":
        _apply_loan_repayment(tx)
    elif purpose == "SAVINGS_DEPOSIT":
        _apply_savings_deposit(tx)
    elif purpose == "GROUP_CONTRIBUTION":
        _apply_group_contribution(tx)


# ============================================================
# SHARED SUCCESS POSTING (STK / C2B)
# ============================================================
@transaction.atomic
def _finalize_successful_incoming_tx(tx: MpesaTransaction, *, channel_label: str) -> MpesaTransaction:
    """
    Shared finalization for successful incoming transactions.
    Posts ledger once and routes to business modules.
    """
    if tx.ledger_posted:
        if tx.status != "SUCCESS":
            tx.status = "SUCCESS"
            tx.updated_at = timezone.now()
            tx.save(update_fields=["status", "updated_at"])
        _route_success_tx(tx)
        return tx

    if tx.user_id:
        category = _purpose_to_ledger_category(tx.purpose)
        payload = tx.request_payload if isinstance(tx.request_payload, dict) else {}

        base_amount = _money(payload.get("base_amount", tx.base_amount or tx.amount))
        fee = _money(payload.get("fee", tx.transaction_fee or "0"))

        business_ref = (tx.reference or "").strip()
        receipt_ref = (tx.mpesa_receipt_number or "").strip()
        ref = business_ref or (receipt_ref or f"{channel_label}#{tx.id}")
        receipt_note = f" ({receipt_ref})" if receipt_ref else ""

        create_ledger_entry(
            user=tx.user,
            entry_type="CREDIT",
            category=category,
            amount=base_amount,
            narration=f"{tx.purpose.replace('_', ' ').title()} via {channel_label}" + receipt_note,
            reference=ref,
            mpesa_tx=tx,
            target_object=tx.target_object,
        )

        if fee > Decimal("0.00"):
            create_ledger_entry(
                user=tx.user,
                entry_type="DEBIT",
                category="TRANSACTION_FEE",
                amount=fee,
                narration=f"{tx.purpose.replace('_', ' ').title()} transaction fee" + receipt_note,
                reference=f"FEE-{ref}",
                mpesa_tx=tx,
                target_object=tx.target_object,
            )

    tx.ledger_posted = True
    tx.status = "SUCCESS"
    tx.updated_at = timezone.now()
    tx.save(update_fields=["ledger_posted", "status", "updated_at"])

    _route_success_tx(tx)
    return tx


# ============================================================
# C2B HELPERS
# ============================================================

# ============================================================
# C2B HELPERS
# ============================================================

def _looks_like_normal_phone(value: str) -> bool:
    v = normalize_phone(value or "")
    return bool(re.match(r"^(07|01)\d{8}$", v))


def _find_existing_pending_c2b_tx(
    *,
    phone: str,
    total_amount: Decimal,
    raw_reference: str,
    purpose: str,
) -> Optional[MpesaTransaction]:
    """
    Clean matching:
    - ALWAYS trust phone from Safaricom
    - match by reference + amount + phone
    """

    phone_n = normalize_phone(phone)
    amount_n = _money(total_amount)
    norm_ref = _normalize_reference_token(raw_reference)

    recent_qs = (
        MpesaTransaction.objects.select_for_update()
        .filter(
            direction__in=("IN", "INCOMING"),
            channel__in=("STK", "C2B"),
            payment_method__in=("PAYBILL", "STK"),
            status__in=("INITIATED", "PENDING", "UNALLOCATED"),
            created_at__gte=timezone.now() - timezone.timedelta(hours=12),
        )
        .order_by("-created_at", "-id")
    )

    for tx in recent_qs:
        tx_ref = _normalize_reference_token(tx.reference or tx.external_reference_raw or "")
        tx_phone = normalize_phone(tx.phone or "")
        tx_amount = _money(tx.amount)

        if norm_ref and tx_ref != norm_ref:
            continue

        if tx_amount != amount_n:
            continue

        if purpose and (tx.purpose or "").upper() != (purpose or "").upper():
            continue

        # ✅ ALWAYS enforce phone match
        if tx_phone and tx_phone != phone_n:
            continue

        return tx

    return None


# ============================================================
# C2B SERVICES
# ============================================================
def handle_c2b_validation_callback(*, callback_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Basic C2B validation.
    Accepts simple references like:
      - mus11
      - saving19
      - loan19
      - grp9
    """
    reference = (
        callback_payload.get("BillRefNumber")
        or callback_payload.get("BillRef")
        or callback_payload.get("AccountReference")
        or ""
    ).strip()

    amount = _money(callback_payload.get("TransAmount", "0"))

    if amount <= Decimal("0.00"):
        return {"ResultCode": "C2B00012", "ResultDesc": "Invalid amount"}

    if not reference:
        return {"ResultCode": "C2B00012", "ResultDesc": "Missing account reference"}

    parsed = _parse_reference(reference)
    if not parsed.valid:
        return {"ResultCode": "C2B00012", "ResultDesc": "Invalid account reference"}

    return {"ResultCode": "0", "ResultDesc": "Accepted"}


@transaction.atomic
def handle_c2b_confirmation_callback(*, callback_payload: Dict[str, Any]) -> MpesaTransaction:
    """
    Handles manual Paybill C2B confirmation callback.

    Supported references:
      - mus11      => merry for user 11
      - saving19   => savings for user 19
      - loan19     => loan repayment for user 19
      - grp9       => group 9
    """
    receipt = (
        callback_payload.get("TransID")
        or callback_payload.get("TransactionID")
        or callback_payload.get("MpesaReceiptNumber")
        or ""
    ).strip()

    if not receipt:
        raise ValueError("Invalid C2B callback: missing receipt/TransID")

    phone_raw = str(
        callback_payload.get("MSISDN")
        or callback_payload.get("MSISDNNumber")
        or callback_payload.get("PhoneNumber")
        or callback_payload.get("MobileNumber")
        or ""
    ).strip()
    phone = normalize_phone(phone_raw)

    total_amount = _money(
        callback_payload.get("TransAmount")
        or callback_payload.get("Amount")
        or "0"
    )
    if total_amount <= Decimal("0.00"):
        raise ValueError("Invalid C2B callback: amount must be greater than 0")

    raw_reference = (
        callback_payload.get("BillRefNumber")
        or callback_payload.get("BillRef")
        or callback_payload.get("AccountReference")
        or ""
    ).strip()

    parsed = _parse_reference(raw_reference)
    purpose = parsed.purpose if parsed.valid else "OTHER"

    # 1. Exact receipt match first
    existing = MpesaTransaction.objects.select_for_update().filter(
        mpesa_receipt_number=receipt
    ).first()

    if existing:
        if existing.status != "SUCCESS":
            existing.status = "SUCCESS"

        existing.channel = "C2B"
        existing.payment_method = "PAYBILL"
        existing.origin = existing.origin or "EXTERNAL"
        existing.direction = existing.direction or "IN"
        existing.reference = parsed.normalized or raw_reference or existing.reference
        existing.external_reference_raw = raw_reference or existing.external_reference_raw
        existing.matched_reference_type = parsed.matched_reference_type
        existing.result_code = "0"
        existing.result_desc = "C2B confirmed"
        existing.callback_payload = callback_payload
        existing.callback_received_at = timezone.now()
        existing.updated_at = timezone.now()

        if phone:
            existing.phone = phone

        existing.save()
        return _finalize_successful_incoming_tx(existing, channel_label="C2B")

    # 2. Match an existing pending tx
    matched_pending = _find_existing_pending_c2b_tx(
        phone=phone,
        total_amount=total_amount,
        raw_reference=raw_reference,
        purpose=purpose,
    )

    matched_user = None
    matched_user_phone = ""

    # Only use direct phone-to-user matching if callback phone looks normal.
    if _looks_like_normal_phone(phone):
        matched_user = UserModel.objects.filter(phone=phone).first()
        matched_user_phone = (
            matched_user.phone
            if matched_user and getattr(matched_user, "phone", "")
            else ""
        )

    if parsed.valid and parsed.kind == "GROUP" and matched_user and parsed.entity_id:
        _require_active_group_membership(user=matched_user, group_id=parsed.entity_id)

    base_amount, fee = split_incoming_total_amount(
        purpose=purpose,
        total_amount=total_amount,
    )

    resolved_user = matched_user
    allocation_note = ""

    # Reference should take priority over phone for beneficiary resolution.
    if parsed.valid and parsed.kind in ("MERRY_USER", "SAVINGS_USER", "LOAN_USER") and parsed.entity_id:
        beneficiary = UserModel.objects.filter(id=parsed.entity_id).first()
        if beneficiary:
            resolved_user = beneficiary
            if matched_user and beneficiary.id != matched_user.id:
                allocation_note = "Reference beneficiary differs from payer phone user."

    if matched_pending:
        matched_pending.user = resolved_user or matched_pending.user

        if phone:
            matched_pending.phone = phone

        if matched_user_phone:
            matched_pending.matched_user_phone = matched_user_phone

        matched_pending.amount = total_amount
        matched_pending.base_amount = base_amount
        matched_pending.transaction_fee = fee
        matched_pending.direction = matched_pending.direction or "IN"
        matched_pending.channel = "C2B"
        matched_pending.payment_method = "PAYBILL"
        matched_pending.origin = matched_pending.origin or "EXTERNAL"
        matched_pending.purpose = purpose or matched_pending.purpose
        matched_pending.status = "SUCCESS"
        matched_pending.reference = parsed.normalized or raw_reference or matched_pending.reference
        matched_pending.external_reference_raw = raw_reference or matched_pending.external_reference_raw
        matched_pending.matched_reference_type = parsed.matched_reference_type
        matched_pending.mpesa_receipt_number = receipt
        matched_pending.result_code = "0"
        matched_pending.result_desc = "C2B confirmed"
        matched_pending.transaction_date = timezone.now()
        matched_pending.callback_received_at = timezone.now()
        matched_pending.callback_payload = callback_payload

        existing_payload = (
            matched_pending.request_payload
            if isinstance(matched_pending.request_payload, dict)
            else {}
        )
        existing_payload.update(
            {
                "base_amount": str(base_amount),
                "fee": str(fee),
                "total_amount": str(total_amount),
                "source": "C2B",
                "payment_method": "PAYBILL",
                "origin": matched_pending.origin or "EXTERNAL",
                "parsed_reference": {
                    "kind": parsed.kind,
                    "entity_id": parsed.entity_id,
                    "purpose": parsed.purpose,
                    "valid": parsed.valid,
                    "matched_reference_type": parsed.matched_reference_type,
                },
            }
        )
        matched_pending.request_payload = existing_payload

        if parsed.valid:
            if not matched_pending.allocation_notes:
                matched_pending.allocation_notes = allocation_note
        else:
            matched_pending.allocation_status = "INVALID_REFERENCE"
            matched_pending.allocation_notes = "Invalid or unsupported account reference."

        matched_pending.save()

        if not parsed.valid:
            return matched_pending

        return _finalize_successful_incoming_tx(matched_pending, channel_label="C2B")

    # 3. Create a fresh C2B tx
    tx = _create_mpesa_tx(
        user=resolved_user,
        phone=phone,
        matched_user_phone=matched_user_phone,
        amount=total_amount,
        base_amount=base_amount,
        transaction_fee=fee,
        direction="IN",
        channel="C2B",
        payment_method="PAYBILL",
        origin="EXTERNAL",
        purpose=purpose,
        status="SUCCESS",
        reference=parsed.normalized or raw_reference,
        external_reference_raw=raw_reference,
        matched_reference_type=parsed.matched_reference_type,
        mpesa_receipt_number=receipt,
        result_code="0",
        result_desc="C2B confirmed",
        transaction_date=timezone.now(),
        callback_received_at=timezone.now(),
        allocation_status="UNALLOCATED" if parsed.valid else "INVALID_REFERENCE",
        allocation_notes=allocation_note if parsed.valid else "Invalid or unsupported account reference.",
        request_payload={
            "base_amount": str(base_amount),
            "fee": str(fee),
            "total_amount": str(total_amount),
            "source": "C2B",
            "payment_method": "PAYBILL",
            "origin": "EXTERNAL",
            "parsed_reference": {
                "kind": parsed.kind,
                "entity_id": parsed.entity_id,
                "purpose": parsed.purpose,
                "valid": parsed.valid,
                "matched_reference_type": parsed.matched_reference_type,
            },
        },
        callback_payload=callback_payload,
    )

    if not parsed.valid:
        return tx

    return _finalize_successful_incoming_tx(tx, channel_label="C2B")


# ============================================================
# STK SERVICES
# ============================================================
def initiate_stk_push(
    *,
    user: AbstractBaseUser,
    phone: str,
    amount: Decimal,
    purpose: str,
    target_object: Optional[object] = None,
    reference: str = "",
    narration: str = "",
) -> MpesaTransaction:
    """
    Starts STK push and creates MpesaTransaction.

    IMPORTANT:
    - amount here is the BASE amount from frontend
    - fee is calculated centrally
    - total_amount = base_amount + fee
    - saving19 / loan19 / mus19 use USER ids
    """
    phone_n = normalize_phone(phone)
    base_amount = _money(amount)
    if base_amount <= Decimal("0.00"):
        raise ValueError("Amount must be greater than 0")

    purpose_u = (purpose or "OTHER").upper()
    parsed = _parse_reference(reference or "")

    if purpose_u == "GROUP_CONTRIBUTION":
        group_id = None
        if parsed.valid and parsed.kind == "GROUP":
            group_id = parsed.entity_id
        else:
            group_id = _extract_id(reference or "", "GROUP-")

        if not group_id:
            raise ValidationError("GROUP_CONTRIBUTION requires reference like 'grp9' or 'GROUP-9'")
        _require_active_group_membership(user=user, group_id=group_id)

    fee = calculate_transaction_fee(purpose=purpose_u, base_amount=base_amount)
    total_amount = (base_amount + fee).quantize(DECIMAL_2, rounding=ROUND_HALF_UP)

    ct, oid = _set_generic_target(target_object)

    recent = (
        MpesaTransaction.objects.filter(
            user=user,
            phone=phone_n,
            amount=total_amount,
            purpose=purpose_u,
            channel="STK",
            direction="IN",
            status__in=("INITIATED", "PENDING"),
            created_at__gte=timezone.now() - timezone.timedelta(seconds=60),
        )
        .order_by("-id")
        .first()
    )
    if recent:
        return recent

    normalized_ref = parsed.normalized if parsed.valid else (reference or "")

    tx = _create_mpesa_tx(
        user=user,
        phone=phone_n,
        matched_user_phone=getattr(user, "phone", "") or "",
        amount=total_amount,
        base_amount=base_amount,
        transaction_fee=fee,
        direction="IN",
        channel="STK",
        payment_method="STK",
        origin="APP",
        purpose=purpose_u,
        status="INITIATED",
        reference=normalized_ref,
        external_reference_raw=reference or "",
        matched_reference_type=parsed.matched_reference_type if parsed.valid else "UNKNOWN",
        allocation_status="UNALLOCATED",
        request_payload={
            "base_amount": str(base_amount),
            "fee": str(fee),
            "total_amount": str(total_amount),
            "narration": narration or "",
            "payment_method": "STK",
            "origin": "APP",
        },
        callback_payload=None,
        target_content_type=ct,
        target_object_id=oid,
    )

    callback_url = _build_callback_url("/payments/mpesa/stk/callback/")
    client = get_daraja_client()

    res = client.stk_push(
        phone=phone_n,
        amount=total_amount,
        account_reference=normalized_ref or f"TX{tx.id}",
        transaction_desc=narration or purpose_u,
        callback_url=callback_url,
    )

    MpesaTransaction.objects.filter(id=tx.id).update(
        merchant_request_id=res.merchant_request_id,
        checkout_request_id=res.checkout_request_id,
        status="PENDING",
        updated_at=timezone.now(),
    )
    tx.refresh_from_db()
    return tx


@transaction.atomic
def handle_stk_callback(*, callback_payload: Dict[str, Any]) -> MpesaTransaction:
    """
    Handles STK callback:
    - updates MpesaTransaction status fields
    - treats callback ResultCode=0 as SUCCESS immediately
    - optional STK query is only enrichment, not a blocker
    - posts ledger entries (idempotent)
    - routes to business modules
    """
    stk = (((callback_payload or {}).get("Body") or {}).get("stkCallback")) or {}
    checkout_id = stk.get("CheckoutRequestID") or ""
    merchant_id = stk.get("MerchantRequestID") or ""
    callback_result_code = (
        str(stk.get("ResultCode")) if stk.get("ResultCode") is not None else ""
    )
    callback_result_desc = stk.get("ResultDesc") or ""

    if not checkout_id:
        raise ValueError("Invalid STK callback: missing CheckoutRequestID")

    tx = (
        MpesaTransaction.objects.select_for_update()
        .filter(checkout_request_id=checkout_id)
        .first()
    )
    if not tx:
        raise ValueError("Unknown CheckoutRequestID (no matching transaction)")

    tx.merchant_request_id = tx.merchant_request_id or merchant_id
    tx.result_code = callback_result_code
    tx.result_desc = callback_result_desc
    tx.updated_at = timezone.now()

    # NEW: extract metadata directly from callback payload and persist it
    metadata_items = _extract_stk_metadata_items(stk)

    receipt = metadata_items.get("MpesaReceiptNumber")
    transaction_date_raw = metadata_items.get("TransactionDate")
    phone_from_callback = metadata_items.get("PhoneNumber")

    if receipt and not tx.mpesa_receipt_number:
        tx.mpesa_receipt_number = str(receipt)

    if transaction_date_raw and not tx.transaction_date:
        # Keep minimal safe behavior consistent with your current model usage.
        tx.transaction_date = timezone.now()

    if phone_from_callback:
        tx.phone = normalize_phone(str(phone_from_callback))

    _mark_callback_received(tx, callback_payload=callback_payload)

    if callback_result_code != "0":
        tx.status = "CANCELLED" if callback_result_code in ("1032",) else "FAILED"
        tx.updated_at = timezone.now()
        tx.save(
            update_fields=[
                "merchant_request_id",
                "result_code",
                "result_desc",
                "mpesa_receipt_number",
                "transaction_date",
                "phone",
                "status",
                "updated_at",
            ]
        )
        return tx

    # Main fix: callback success is authoritative
    tx.status = "SUCCESS"
    tx.callback_received_at = timezone.now()
    tx.updated_at = timezone.now()

    enable_verify = getattr(settings, "MPESA_ENABLE_STK_QUERY_VERIFICATION", True)

    if enable_verify:
        try:
            client = get_daraja_client()
            q = _verify_stk_with_query(client, tx)

            receipt_q = q.get("MpesaReceiptNumber") or q.get("mpesaReceiptNumber")
            amount_q = _safe_decimal(q.get("Amount"))
            tx_date_q = q.get("TransactionDate")

            if receipt_q and not tx.mpesa_receipt_number:
                tx.mpesa_receipt_number = str(receipt_q)

            if amount_q > Decimal("0") and amount_q != tx.amount:
                if getattr(settings, "MPESA_STRICT_AMOUNT_MATCH", True):
                    tx.status = "FAILED"
                    tx.result_desc = "Amount mismatch detected during STK verification."[:255]
                    tx.updated_at = timezone.now()
                    tx.save(
                        update_fields=[
                            "merchant_request_id",
                            "result_code",
                            "result_desc",
                            "status",
                            "updated_at",
                            "mpesa_receipt_number",
                            "transaction_date",
                            "phone",
                            "callback_received_at",
                        ]
                    )
                    return tx

            if tx_date_q and not tx.transaction_date:
                tx.transaction_date = timezone.now()

        except Exception as e:
            # Do not revert paid callback to pending
            warn = f"Verification warning: {str(e)}"
            tx.result_desc = f"{tx.result_desc or ''} | {warn}"[:255]

    tx.save(
        update_fields=[
            "merchant_request_id",
            "result_code",
            "result_desc",
            "status",
            "updated_at",
            "mpesa_receipt_number",
            "transaction_date",
            "phone",
            "callback_received_at",
        ]
    )

    return _finalize_successful_incoming_tx(tx, channel_label="STK")


# ============================================================
# WITHDRAWAL / B2C SERVICES
# ============================================================
@transaction.atomic
def create_withdrawal_request(
    *,
    user: AbstractBaseUser,
    phone: str,
    amount: Decimal,
    source: str = "SAVINGS",
    target_object: Optional[object] = None,
) -> WithdrawalRequest:
    phone_n = normalize_phone(phone)
    amt = _money(amount)
    if amt <= Decimal("0"):
        raise ValueError("Amount must be greater than 0")

    ct, oid = _set_generic_target(target_object)
    wd = WithdrawalRequest.objects.create(
        user=user,
        phone=phone_n,
        amount=amt,
        source=(source or "SAVINGS").upper(),
        target_content_type=ct,
        target_object_id=oid,
        status="PENDING",
    )
    return wd


@transaction.atomic
def approve_withdrawal_request(*, withdrawal_id: int, approved_by: AbstractBaseUser) -> WithdrawalRequest:
    wd = WithdrawalRequest.objects.select_for_update().get(id=withdrawal_id)
    if wd.status != "PENDING":
        return wd

    wd.status = "APPROVED"
    wd.approved_by = approved_by
    wd.approved_at = timezone.now()
    wd.save(update_fields=["status", "approved_by", "approved_at"])
    return wd


def initiate_b2c_payout_for_withdrawal(*, withdrawal_id: int) -> MpesaTransaction:
    """
    Creates an OUT/B2C MpesaTransaction and triggers Daraja B2C payout.

    Logic:
    - wd.amount is the requested/base payout amount
    - WITHDRAWAL fee is calculated centrally
    - user receives payout_amount = requested_amount - fee
    - later ledger deducts requested amount + separate fee entry
    """
    client = get_daraja_client()

    with transaction.atomic():
        wd = WithdrawalRequest.objects.select_for_update().select_related("user").get(id=withdrawal_id)

        if wd.status != "APPROVED":
            raise ValueError(f"Withdrawal must be APPROVED to payout. Current: {wd.status}")

        if wd.is_final:
            raise ValueError("Withdrawal already finalized")

        if wd.source == "MERRY" and not wd.can_withdraw_merry:
            raise ValueError("Merry withdrawal not allowed yet (not payout date).")

        source_category = _withdrawal_source_to_category(wd.source)
        available = get_user_balance(user=wd.user, category=source_category)
        requested_amount = _money(wd.amount)
        fee = calculate_transaction_fee(purpose="WITHDRAWAL", base_amount=requested_amount)
        total_deduction = requested_amount + fee

        if total_deduction > available:
            raise ValidationError(
                f"Insufficient {source_category} balance. "
                f"Available: {available}. Required: {total_deduction}."
            )

        payout_amount = (requested_amount - fee).quantize(DECIMAL_2, rounding=ROUND_HALF_UP)
        if payout_amount <= Decimal("0.00"):
            raise ValueError("Withdrawal amount too small after fee")

        ct, oid = _set_generic_target(wd.target_object)

        tx = _create_mpesa_tx(
            user=wd.user,
            phone=wd.phone,
            matched_user_phone=getattr(wd.user, "phone", "") or "",
            amount=payout_amount,
            base_amount=requested_amount,
            transaction_fee=fee,
            direction="OUT",
            channel="B2C",
            payment_method="B2C",
            origin="SYSTEM",
            purpose="WITHDRAWAL",
            status="INITIATED",
            reference=f"WD#{wd.id}",
            external_reference_raw=f"WD#{wd.id}",
            matched_reference_type="WITHDRAWAL",
            request_payload={
                "withdrawal_id": wd.id,
                "requested_amount": str(requested_amount),
                "fee": str(fee),
                "payout_amount": str(payout_amount),
                "source": wd.source,
                "total_deduction": str(total_deduction),
                "payment_method": "B2C",
                "origin": "SYSTEM",
            },
            callback_payload=None,
            target_content_type=ct,
            target_object_id=oid,
        )

        wd.status = "PROCESSING"
        wd.mpesa_tx = tx
        wd.save(update_fields=["status", "mpesa_tx"])

    result_url = _build_callback_url("/payments/mpesa/b2c/result/")
    timeout_url = _build_callback_url("/payments/mpesa/b2c/timeout/")

    res = client.b2c_payout(
        phone=wd.phone,
        amount=payout_amount,
        remarks=f"Withdrawal WD#{wd.id}",
        occasion="Withdrawal",
        result_url=result_url,
        timeout_url=timeout_url,
    )

    MpesaTransaction.objects.filter(id=tx.id).update(
        conversation_id=res.conversation_id,
        originator_conversation_id=res.originator_conversation_id or "",
        status="PENDING",
        updated_at=timezone.now(),
    )

    tx.refresh_from_db()
    return tx


@transaction.atomic
def handle_b2c_result_callback(*, callback_payload: Dict[str, Any]) -> MpesaTransaction:
    """
    B2C result:
    - updates tx + withdrawal status
    - posts ledger once (DEBIT source + fee)
    - applies real SavingsAccount deduction when source=SAVINGS
    """
    result = (callback_payload or {}).get("Result") or {}
    conversation_id = result.get("ConversationID") or result.get("ConversationId") or ""
    result_code = str(result.get("ResultCode")) if result.get("ResultCode") is not None else ""
    result_desc = result.get("ResultDesc") or ""

    if not conversation_id:
        raise ValueError("Invalid B2C callback: missing ConversationID")

    tx = MpesaTransaction.objects.select_for_update().filter(conversation_id=conversation_id).first()
    if not tx:
        raise ValueError("Unknown ConversationID (no matching transaction)")

    _mark_callback_received(tx, callback_payload=callback_payload)

    tx.result_code = result_code
    tx.result_desc = result_desc
    tx.status = "SUCCESS" if result_code == "0" else "FAILED"
    tx.updated_at = timezone.now()
    tx.save(update_fields=["result_code", "result_desc", "status", "updated_at"])

    wd = WithdrawalRequest.objects.select_for_update().filter(mpesa_tx=tx).first()
    if wd:
        wd.status = "PAID" if tx.status == "SUCCESS" else "FAILED"
        wd.save(update_fields=["status"])

    if tx.status != "SUCCESS" or tx.ledger_posted:
        return tx

    if not tx.user_id:
        tx.ledger_posted = True
        tx.save(update_fields=["ledger_posted"])
        return tx

    payload = tx.request_payload if isinstance(tx.request_payload, dict) else {}
    fee = _money(payload.get("fee", "0"))
    source_category = _withdrawal_source_to_category(payload.get("source", "SAVINGS"))
    requested_amount = _money(payload.get("requested_amount", "0"))

    if requested_amount <= Decimal("0.00"):
        requested_amount = _money(tx.amount + fee)

    if wd and wd.status == "PAID" and source_category == "SAVINGS":
        from savings import services as savings_services

        fn = getattr(savings_services, "apply_mpesa_withdrawal_payout", None)
        if callable(fn):
            fn(
                user=tx.user,
                requested_amount=requested_amount,
                withdrawal_ref=tx.reference or f"WD#{wd.id}",
                target_object=wd.target_object,
                mpesa_tx=tx,
            )

    create_ledger_entry(
        user=tx.user,
        entry_type="DEBIT",
        category=source_category,
        amount=requested_amount,
        narration=f"Withdrawal ({tx.reference})",
        reference=tx.reference or f"B2C#{tx.id}",
        mpesa_tx=tx,
        target_object=tx.target_object,
    )

    if fee > Decimal("0.00"):
        create_ledger_entry(
            user=tx.user,
            entry_type="DEBIT",
            category="WITHDRAWAL_FEE",
            amount=fee,
            narration=f"Withdrawal fee ({tx.reference})",
            reference=f"FEE-{tx.reference or tx.id}",
            mpesa_tx=tx,
            target_object=tx.target_object,
        )

    tx.ledger_posted = True
    tx.save(update_fields=["ledger_posted"])
    return tx


@transaction.atomic
def handle_b2c_timeout_callback(*, callback_payload: Dict[str, Any]) -> MpesaTransaction:
    """
    Marks tx TIMEOUT and withdrawal FAILED (if not final).
    """
    result = (callback_payload or {}).get("Result") or {}
    conversation_id = result.get("ConversationID") or result.get("ConversationId") or ""

    if not conversation_id:
        raise ValueError("Invalid B2C timeout callback: missing ConversationID")

    tx = MpesaTransaction.objects.select_for_update().filter(conversation_id=conversation_id).first()
    if not tx:
        raise ValueError("Unknown ConversationID (no matching transaction)")

    _mark_callback_received(tx, callback_payload=callback_payload)

    tx.status = "TIMEOUT"
    tx.updated_at = timezone.now()
    tx.save(update_fields=["status", "updated_at"])

    wd = WithdrawalRequest.objects.select_for_update().filter(mpesa_tx=tx).first()
    if wd and not wd.is_final:
        wd.status = "FAILED"
        wd.save(update_fields=["status"])

    return tx