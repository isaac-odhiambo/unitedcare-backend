# payments/services.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .balances import get_user_balance
from .models import MpesaTransaction, PaymentLedger, WithdrawalRequest
from .utils import calculate_b2c_fee

# Runtime model class (do NOT use for type expressions in Pylance)
UserModel = get_user_model()

# ============================================================
# Constants / Fees
# ============================================================
MERRY_STK_FEE = Decimal("50")  # flat fee added on top of merry contribution


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
    return mapping.get(purpose, "OTHER")


def _withdrawal_source_to_category(source: str) -> str:
    """
    Withdrawal must reduce the SOURCE bucket balance.
    """
    s = (source or "").upper()
    mapping = {"SAVINGS": "SAVINGS", "MERRY": "MERRY", "GROUP": "GROUP"}
    return mapping.get(s, "SAVINGS")


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
        amount=amount,
        narration=narration,
        reference=reference,
        mpesa_tx=mpesa_tx,
        target_content_type=ct,
        target_object_id=oid,
        created_at=timezone.now(),
    )


# ============================================================
# Daraja Client (plug your implementation here)
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
    """
    Replace these with your real implementation.
    IMPORTANT: stk_query() is required for security verification.
    """

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
        """
        Must call Daraja STK Query API.
        """
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
    """
    Uses your real Daraja client from payments/daraja.py
    """
    from .daraja import DarajaClient as RealClient  # avoids circular import at module load
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
# STK VERIFICATION (SECURITY)
# ============================================================
def _stk_query_is_success(data: Dict[str, Any]) -> bool:
    rc = data.get("ResultCode")
    if rc is None:
        return False
    return str(rc) == "0"


def _verify_stk_with_query(client, tx: MpesaTransaction) -> Dict[str, Any]:
    if not tx.checkout_request_id:
        raise ValueError("Cannot verify STK without checkout_request_id")
    return client.stk_query(checkout_request_id=tx.checkout_request_id)


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
    phone_n = normalize_phone(phone)
    base_amount = _safe_decimal(amount)
    if base_amount <= Decimal("0"):
        raise ValueError("Amount must be greater than 0")

    # Apply Merry fee rule: pay (amount + 50)
    fee = Decimal("0")
    total_amount = base_amount
    if purpose == "MERRY_CONTRIBUTION":
        fee = MERRY_STK_FEE
        total_amount = base_amount + fee

    ct, oid = _set_generic_target(target_object)

    # Rush guard: reuse a recent INITIATED/PENDING tx within 60s
    recent = (
        MpesaTransaction.objects.filter(
            user=user,
            phone=phone_n,
            amount=total_amount,
            purpose=purpose,
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

    tx = MpesaTransaction.objects.create(
        user=user,
        phone=phone_n,
        amount=total_amount,
        direction="IN",
        channel="STK",
        purpose=purpose,
        status="INITIATED",
        reference=reference or "",
        request_payload={
            "base_amount": str(base_amount),
            "fee": str(fee),
            "total_amount": str(total_amount),
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
        transaction_desc=narration or purpose,
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

    # audit store
    tx.merchant_request_id = tx.merchant_request_id or merchant_id
    tx.callback_payload = callback_payload
    tx.result_code = callback_result_code
    tx.result_desc = callback_result_desc
    tx.updated_at = timezone.now()
    tx.save(update_fields=["merchant_request_id", "callback_payload", "result_code", "result_desc", "updated_at"])

    # cancelled/failed
    if callback_result_code != "0":
        tx.status = "CANCELLED" if callback_result_code in ("1032",) else "FAILED"
        tx.save(update_fields=["status"])
        return tx

    # idempotency
    if tx.ledger_posted:
        tx.status = "SUCCESS"
        tx.save(update_fields=["status"])
        return tx

    enable_verify = getattr(settings, "MPESA_ENABLE_STK_QUERY_VERIFICATION", True)

    if enable_verify:
        client = get_daraja_client()
        try:
            q = _verify_stk_with_query(client, tx)
        except Exception as e:
            # do not credit if verification fails
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

        # ✅ STRICT AMOUNT CHECK (MAX SECURITY)
        if amount_q > Decimal("0") and amount_q != tx.amount:
            if getattr(settings, "MPESA_STRICT_AMOUNT_MATCH", True):
                tx.status = "FAILED"
                tx.result_desc = "Amount mismatch detected during STK verification."[:255]
                tx.save(update_fields=["status", "result_desc", "mpesa_receipt_number"])
                return tx
            # non-strict mode: continue (not recommended)

        if tx_date and not tx.transaction_date:
            tx.transaction_date = timezone.now()

    # credit
    tx.status = "SUCCESS"
    tx.updated_at = timezone.now()
    tx.save(update_fields=["status", "updated_at", "mpesa_receipt_number", "transaction_date"])

    if tx.user_id:
        category = _purpose_to_ledger_category(tx.purpose)
        payload = tx.request_payload if isinstance(tx.request_payload, dict) else {}

        base_amount = _safe_decimal(payload.get("base_amount", tx.amount))
        fee = _safe_decimal(payload.get("fee", "0"))
        total = _safe_decimal(payload.get("total_amount", tx.amount))

        ref = tx.mpesa_receipt_number or tx.reference or f"STK#{tx.id}"

        if tx.purpose == "MERRY_CONTRIBUTION":
            create_ledger_entry(
                user=tx.user,
                entry_type="CREDIT",
                category="MERRY",
                amount=base_amount,
                narration="Merry contribution via STK",
                reference=ref,
                mpesa_tx=tx,
                target_object=tx.target_object,
            )
            if fee > Decimal("0"):
                create_ledger_entry(
                    user=tx.user,
                    entry_type="DEBIT",
                    category="TRANSACTION_FEE",
                    amount=fee,
                    narration="Merry contribution transaction fee",
                    reference=f"FEE-{ref}",
                    mpesa_tx=tx,
                    target_object=tx.target_object,
                )
        else:
            create_ledger_entry(
                user=tx.user,
                entry_type="CREDIT",
                category=category,
                amount=total,
                narration=f"{tx.purpose.replace('_', ' ').title()} via STK",
                reference=ref,
                mpesa_tx=tx,
                target_object=tx.target_object,
            )

    tx.ledger_posted = True
    tx.save(update_fields=["ledger_posted"])
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
    amt = _safe_decimal(amount)
    if amt <= Decimal("0"):
        raise ValueError("Amount must be greater than 0")

    ct, oid = _set_generic_target(target_object)
    wd = WithdrawalRequest.objects.create(
        user=user,
        phone=phone_n,
        amount=amt,
        source=source,
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
    client = get_daraja_client()

    with transaction.atomic():
        wd = WithdrawalRequest.objects.select_for_update().select_related("user").get(id=withdrawal_id)

        if wd.status != "APPROVED":
            raise ValueError(f"Withdrawal must be APPROVED to payout. Current: {wd.status}")

        if wd.is_final:
            raise ValueError("Withdrawal already finalized")

        if wd.source == "MERRY" and not wd.can_withdraw_merry:
            raise ValueError("Merry withdrawal not allowed yet (not payout date).")

        # ✅ HARDEN: CHECK BALANCE BEFORE PAYOUT
        source_category = _withdrawal_source_to_category(wd.source)
        available = get_user_balance(user=wd.user, category=source_category)
        if wd.amount > available:
            raise ValidationError(f"Insufficient {source_category} balance. Available: {available}")

        fee = calculate_b2c_fee(wd.amount)
        payout_amount = wd.amount - fee

        if payout_amount <= Decimal("0"):
            raise ValueError("Withdrawal amount too small after fee")

        ct, oid = _set_generic_target(wd.target_object)

        tx = MpesaTransaction.objects.create(
            user=wd.user,
            phone=wd.phone,
            amount=payout_amount,
            direction="OUT",
            channel="B2C",
            purpose="WITHDRAWAL",
            status="INITIATED",
            reference=f"WD#{wd.id}",
            request_payload={
                "withdrawal_id": wd.id,
                "requested_amount": str(wd.amount),
                "fee": str(fee),
                "payout_amount": str(payout_amount),
                "source": wd.source,
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
    fee = _safe_decimal(payload.get("fee", "0"))

    # ✅ Deduct from SOURCE bucket, not WITHDRAWAL bucket
    source_category = _withdrawal_source_to_category(payload.get("source", "SAVINGS"))
    requested_amount = _safe_decimal(payload.get("requested_amount", "0"))
    if requested_amount <= Decimal("0"):
        # fallback: requested_amount = payout + fee
        requested_amount = tx.amount + fee

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

    if fee > Decimal("0"):
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