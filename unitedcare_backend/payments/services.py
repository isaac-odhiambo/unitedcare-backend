# payments/services.py
from __future__ import annotations

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
from .models import MpesaTransaction, PaymentLedger, TransactionFeeConfig, WithdrawalRequest

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


def _extract_id(reference: str, prefix: str) -> Optional[int]:
    """
    Extract integer id from a reference like:
      "LOAN-12"  with prefix "LOAN-"
      "MERRY-PAYMENT-99" with prefix "MERRY-PAYMENT-"
      "GROUP-7" with prefix "GROUP-"
    """
    ref = (reference or "").strip()
    if not ref.startswith(prefix):
        return None
    try:
        return int(ref.replace(prefix, "").strip())
    except Exception:
        return None


def _create_mpesa_tx(**kwargs) -> MpesaTransaction:
    """
    Backward-safe create:
    if your MpesaTransaction model already has base_amount / transaction_fee,
    they will be stored; if not, they will be ignored without crashing.
    """
    model_fields = {f.name for f in MpesaTransaction._meta.get_fields()}
    clean = {k: v for k, v in kwargs.items() if k in model_fields}
    return MpesaTransaction.objects.create(**clean)


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
    """
    Central fee calculator.
    - fixed_fee applies directly
    - percentage_fee is % of base amount
    - both are allowed together
    """
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


# ============================================================
# Group membership guard for GROUP_CONTRIBUTION
# ============================================================
def _require_active_group_membership(*, user: AbstractBaseUser, group_id: int) -> None:
    """
    Prevent non-members from paying into a group.
    """
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
    payment_id = _extract_id(tx.reference or "", "MERRY-PAYMENT-")
    if not payment_id:
        return

    try:
        from merry.models import MerryPayment
        from merry.views import allocate_payment
    except Exception:
        return

    pay = MerryPayment.objects.select_for_update().filter(id=payment_id).first()
    if not pay:
        return

    if pay.status == "CONFIRMED":
        return

    pay.status = "CONFIRMED"
    pay.paid_at = timezone.now()
    if tx.mpesa_receipt_number and not pay.mpesa_receipt_number:
        pay.mpesa_receipt_number = tx.mpesa_receipt_number
    pay.save(update_fields=["status", "paid_at", "mpesa_receipt_number"])

    allocate_payment(pay.id)


@transaction.atomic
def _apply_loan_repayment(tx: MpesaTransaction) -> None:
    loan_id = _extract_id(tx.reference or "", "LOAN-")
    if not loan_id:
        return

    try:
        from loans import services as loan_services
    except Exception:
        return

    fn = getattr(loan_services, "apply_mpesa_repayment", None)
    if callable(fn):
        payload = tx.request_payload if isinstance(tx.request_payload, dict) else {}
        base_amount = _money(payload.get("base_amount", tx.amount))
        fn(loan_id=loan_id, amount=base_amount, mpesa_tx=tx)


@transaction.atomic
def _apply_savings_deposit(tx: MpesaTransaction) -> None:
    try:
        from savings import services as savings_services
    except Exception:
        return

    fn = getattr(savings_services, "apply_mpesa_deposit", None)
    if callable(fn):
        payload = tx.request_payload if isinstance(tx.request_payload, dict) else {}
        base_amount = _money(payload.get("base_amount", tx.amount))
        fn(user=tx.user, amount=base_amount, mpesa_tx=tx, reference=tx.reference or "")


@transaction.atomic
def _apply_group_contribution(tx: MpesaTransaction) -> None:
    try:
        from groups import services as group_services
    except Exception:
        return

    fn = getattr(group_services, "apply_mpesa_contribution", None)
    if callable(fn):
        payload = tx.request_payload if isinstance(tx.request_payload, dict) else {}
        base_amount = _money(payload.get("base_amount", tx.amount))
        fn(user=tx.user, amount=base_amount, mpesa_tx=tx, reference=tx.reference or "")


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
    - fee is calculated centrally from TransactionFeeConfig
    - total_amount = base_amount + fee
    """
    phone_n = normalize_phone(phone)
    base_amount = _money(amount)
    if base_amount <= Decimal("0.00"):
        raise ValueError("Amount must be greater than 0")

    purpose_u = (purpose or "OTHER").upper()

    if purpose_u == "GROUP_CONTRIBUTION":
        group_id = _extract_id(reference or "", "GROUP-")
        if not group_id:
            raise ValidationError("GROUP_CONTRIBUTION requires reference='GROUP-<group_id>'")
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

    tx = _create_mpesa_tx(
        user=user,
        phone=phone_n,
        amount=total_amount,            # total charged to phone
        base_amount=base_amount,        # stored if field exists
        transaction_fee=fee,            # stored if field exists
        direction="IN",
        channel="STK",
        purpose=purpose_u,
        status="INITIATED",
        reference=reference or "",
        request_payload={
            "base_amount": str(base_amount),
            "fee": str(fee),
            "total_amount": str(total_amount),
            "narration": narration or "",
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
        account_reference=reference or f"TX{tx.id}",
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
    - verifies SUCCESS using STK Query
    - posts ledger entries (idempotent)
    - routes to business modules
    """
    stk = (((callback_payload or {}).get("Body") or {}).get("stkCallback")) or {}
    checkout_id = stk.get("CheckoutRequestID") or ""
    merchant_id = stk.get("MerchantRequestID") or ""
    callback_result_code = str(stk.get("ResultCode")) if stk.get("ResultCode") is not None else ""
    callback_result_desc = stk.get("ResultDesc") or ""

    if not checkout_id:
        raise ValueError("Invalid STK callback: missing CheckoutRequestID")

    tx = MpesaTransaction.objects.select_for_update().filter(checkout_request_id=checkout_id).first()
    if not tx:
        raise ValueError("Unknown CheckoutRequestID (no matching transaction)")

    tx.merchant_request_id = tx.merchant_request_id or merchant_id
    tx.callback_payload = callback_payload
    tx.result_code = callback_result_code
    tx.result_desc = callback_result_desc
    tx.updated_at = timezone.now()
    tx.save(update_fields=["merchant_request_id", "callback_payload", "result_code", "result_desc", "updated_at"])

    if callback_result_code != "0":
        tx.status = "CANCELLED" if callback_result_code in ("1032",) else "FAILED"
        tx.save(update_fields=["status"])
        return tx

    if tx.ledger_posted:
        tx.status = "SUCCESS"
        tx.save(update_fields=["status"])
        _route_success_tx(tx)
        return tx

    enable_verify = getattr(settings, "MPESA_ENABLE_STK_QUERY_VERIFICATION", True)

    if enable_verify:
        client = get_daraja_client()
        try:
            q = _verify_stk_with_query(client, tx)
        except Exception as e:
            tx.status = "PENDING"
            tx.result_desc = f"{tx.result_desc or ''} | Verification error: {str(e)}"[:255]
            tx.save(update_fields=["status", "result_desc"])
            return tx

        if not _stk_query_is_success(q):
            tx.status = "PENDING"
            tx.result_desc = f"{tx.result_desc or ''} | STK Query not confirmed"[:255]
            tx.save(update_fields=["status", "result_desc"])
            return tx

        receipt = q.get("MpesaReceiptNumber") or q.get("mpesaReceiptNumber")
        amount_q = _safe_decimal(q.get("Amount"))
        tx_date = q.get("TransactionDate")

        if receipt and not tx.mpesa_receipt_number:
            tx.mpesa_receipt_number = str(receipt)

        if amount_q > Decimal("0") and amount_q != tx.amount:
            if getattr(settings, "MPESA_STRICT_AMOUNT_MATCH", True):
                tx.status = "FAILED"
                tx.result_desc = "Amount mismatch detected during STK verification."[:255]
                tx.save(update_fields=["status", "result_desc", "mpesa_receipt_number"])
                return tx

        if tx_date and not tx.transaction_date:
            tx.transaction_date = timezone.now()

    tx.status = "SUCCESS"
    tx.updated_at = timezone.now()
    tx.save(update_fields=["status", "updated_at", "mpesa_receipt_number", "transaction_date"])

    if tx.user_id:
        category = _purpose_to_ledger_category(tx.purpose)
        payload = tx.request_payload if isinstance(tx.request_payload, dict) else {}

        base_amount = _money(payload.get("base_amount", tx.amount))
        fee = _money(payload.get("fee", "0"))
        total = _money(payload.get("total_amount", tx.amount))

        business_ref = (tx.reference or "").strip()
        receipt_ref = (tx.mpesa_receipt_number or "").strip()
        ref = business_ref or (receipt_ref or f"STK#{tx.id}")
        receipt_note = f" ({receipt_ref})" if receipt_ref else ""

        # Credit business/base amount only
        create_ledger_entry(
            user=tx.user,
            entry_type="CREDIT",
            category=category,
            amount=base_amount,
            narration=f"{tx.purpose.replace('_', ' ').title()} via STK" + receipt_note,
            reference=ref,
            mpesa_tx=tx,
            target_object=tx.target_object,
        )

        # Post fee separately if present
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
    tx.save(update_fields=["ledger_posted"])

    _route_success_tx(tx)
    return tx


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
        amount=amt,  # requested/base payout amount
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
            amount=payout_amount,           # actual B2C sent to phone
            base_amount=requested_amount,   # requested/base amount
            transaction_fee=fee,
            direction="OUT",
            channel="B2C",
            purpose="WITHDRAWAL",
            status="INITIATED",
            reference=f"WD#{wd.id}",
            request_payload={
                "withdrawal_id": wd.id,
                "requested_amount": str(requested_amount),
                "fee": str(fee),
                "payout_amount": str(payout_amount),
                "source": wd.source,
                "total_deduction": str(total_deduction),
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

    tx.result_code = result_code
    tx.result_desc = result_desc
    tx.callback_payload = callback_payload
    tx.status = "SUCCESS" if result_code == "0" else "FAILED"
    tx.updated_at = timezone.now()
    tx.save(update_fields=["result_code", "result_desc", "callback_payload", "status", "updated_at"])

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
        try:
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
        except Exception:
            raise

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

    tx.status = "TIMEOUT"
    tx.callback_payload = callback_payload
    tx.updated_at = timezone.now()
    tx.save(update_fields=["status", "callback_payload", "updated_at"])

    wd = WithdrawalRequest.objects.select_for_update().filter(mpesa_tx=tx).first()
    if wd and not wd.is_final:
        wd.status = "FAILED"
        wd.save(update_fields=["status"])

    return tx