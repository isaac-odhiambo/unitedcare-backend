# payments/services.py

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.sexceptions import ValidationError

from .models import MpesaTransaction, PaymentLedger, WithdrawalRequest



# ============================================================
# Money helpers
# ============================================================

MONEY_QUANT = Decimal("0.01")


def q2(x: Decimal) -> Decimal:
    return Decimal(x).quantize(MONEY_QUANT)


def normalize_ke_phone(phone: str) -> str:
    """
    Accept: 07.. / 01.. / 254.. / +254..
    Store: 2547.. / 2541..
    """
    if not phone:
        raise ValidationError({"phone": "Phone is required."})

    p = str(phone).strip().replace(" ", "")
    if p.startswith("+"):
        p = p[1:]

    if p.startswith("0"):
        p = "254" + p[1:]

    # 2547XXXXXXXX or 2541XXXXXXXX => length 12
    if not p.startswith("254") or len(p) != 12:
        raise ValidationError({"phone": "Phone must be Kenyan format (07.. / 01.. / 254..)."})

    return p


def _now_timestamp() -> str:
    return timezone.now().strftime("%Y%m%d%H%M%S")


def _stk_password(shortcode: str, passkey: str, timestamp: str) -> str:
    raw = f"{shortcode}{passkey}{timestamp}".encode("utf-8")
    return base64.b64encode(raw).decode("utf-8")


def _stk_callback_meta(payload: dict) -> dict:
    try:
        items = payload["Body"]["stkCallback"]["CallbackMetadata"]["Item"]
    except Exception:
        return {}

    out = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        name = it.get("Name")
        if name:
            out[name] = it.get("Value")
    return out


def _parse_mpesa_datetime(v: Any):
    if not v:
        return None
    s = str(v)
    try:
        dt = datetime.strptime(s, "%Y%m%d%H%M%S")
        return timezone.make_aware(dt, timezone.get_current_timezone())
    except Exception:
        return None


def purpose_to_ledger_category(purpose: str) -> str:
    p = (purpose or "").upper()
    if p == "SAVINGS_DEPOSIT":
        return "SAVINGS"
    if p == "MERRY_CONTRIBUTION":
        return "MERRY"
    if p == "LOAN_REPAYMENT":
        return "LOANS"
    if p == "WITHDRAWAL":
        return "WITHDRAWAL"
    return "OTHER"


# ============================================================
# Mpesa config + token
# ============================================================

@dataclass(frozen=True)
class MpesaConfig:
    env: str
    consumer_key: str
    consumer_secret: str

    stk_shortcode: str
    stk_passkey: str
    stk_callback_url: str

    b2c_shortcode: str
    b2c_initiator_name: str
    b2c_security_credential: str
    b2c_result_url: str
    b2c_timeout_url: str

    base_url: str


def get_mpesa_config() -> MpesaConfig:
    env = getattr(settings, "MPESA_ENV", "sandbox")
    base_url = "https://sandbox.safaricom.co.ke" if env == "sandbox" else "https://api.safaricom.co.ke"

    required = [
        "MPESA_CONSUMER_KEY",
        "MPESA_CONSUMER_SECRET",
        "MPESA_STK_SHORTCODE",
        "MPESA_STK_PASSKEY",
        "MPESA_STK_CALLBACK_URL",
        "MPESA_B2C_SHORTCODE",
        "MPESA_B2C_INITIATOR_NAME",
        "MPESA_B2C_SECURITY_CREDENTIAL",
        "MPESA_B2C_RESULT_URL",
        "MPESA_B2C_TIMEOUT_URL",
    ]
    missing = [k for k in required if not getattr(settings, k, None)]
    if missing:
        raise ValidationError(f"Missing Mpesa settings: {', '.join(missing)}")

    return MpesaConfig(
        env=env,
        consumer_key=settings.MPESA_CONSUMER_KEY,
        consumer_secret=settings.MPESA_CONSUMER_SECRET,
        stk_shortcode=str(settings.MPESA_STK_SHORTCODE),
        stk_passkey=str(settings.MPESA_STK_PASSKEY),
        stk_callback_url=str(settings.MPESA_STK_CALLBACK_URL),
        b2c_shortcode=str(settings.MPESA_B2C_SHORTCODE),
        b2c_initiator_name=str(settings.MPESA_B2C_INITIATOR_NAME),
        b2c_security_credential=str(settings.MPESA_B2C_SECURITY_CREDENTIAL),
        b2c_result_url=str(settings.MPESA_B2C_RESULT_URL),
        b2c_timeout_url=str(settings.MPESA_B2C_TIMEOUT_URL),
        base_url=base_url,
    )


_TOKEN_CACHE: Dict[str, Any] = {"token": None, "expires_at": None}


def mpesa_access_token(force_refresh: bool = False) -> str:
    now = timezone.now().timestamp()
    token = _TOKEN_CACHE.get("token")
    exp = _TOKEN_CACHE.get("expires_at")

    if (not force_refresh) and token and exp and now < exp - 30:
        return str(token)

    cfg = get_mpesa_config()
    url = f"{cfg.base_url}/oauth/v1/generate?grant_type=client_credentials"
    r = requests.get(url, auth=(cfg.consumer_key, cfg.consumer_secret), timeout=25)

    if r.status_code != 200:
        raise ValidationError(f"Mpesa auth failed: {r.text}")

    data = r.json()
    token = data.get("access_token")
    expires_in = int(data.get("expires_in") or 3599)

    if not token:
        raise ValidationError("Mpesa auth failed: missing access_token.")

    _TOKEN_CACHE["token"] = token
    _TOKEN_CACHE["expires_at"] = now + expires_in
    return str(token)


def _auth_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {mpesa_access_token()}", "Content-Type": "application/json"}


# ============================================================
# STK PUSH (Customer -> Paybill)
# ============================================================

@transaction.atomic
def initiate_stk_push(
    *,
    user,
    phone: str,
    amount: Decimal,
    purpose: str,
    reference: str = "",
    raw_request: Optional[dict] = None,
) -> MpesaTransaction:
    """
    ✅ EXACT signature your view uses.
    Creates MpesaTransaction first, then calls Safaricom STK Push.
    Updates tx with checkout_request_id / merchant_request_id for callback matching.
    """
    cfg = get_mpesa_config()

    amt = q2(Decimal(str(amount or "0")))
    if amt <= 0:
        raise ValidationError({"amount": "Amount must be greater than 0."})

    phone_norm = normalize_ke_phone(phone)

    timestamp = _now_timestamp()
    password = _stk_password(cfg.stk_shortcode, cfg.stk_passkey, timestamp)

    # Create tx first (audit trail)
    tx = MpesaTransaction.objects.create(
        user=user,
        phone=phone_norm,
        amount=amt,
        direction="IN",
        channel="STK",
        purpose=(purpose or "OTHER").upper(),
        status="INITIATED",
        reference=(reference or "").strip(),
        request_payload={"client_request": raw_request or {}, "reference": (reference or "").strip()},
    )

    url = f"{cfg.base_url}/mpesa/stkpush/v1/processrequest"
    payload = {
        "BusinessShortCode": cfg.stk_shortcode,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": int(amt),
        "PartyA": phone_norm,
        "PartyB": cfg.stk_shortcode,
        "PhoneNumber": phone_norm,
        "CallBackURL": cfg.stk_callback_url,
        "AccountReference": reference or "UNITEDCARE",
        "TransactionDesc": "UNITED CARE",
    }

    r = requests.post(url, headers=_auth_headers(), data=json.dumps(payload), timeout=25)
    try:
        resp = r.json()
    except Exception:
        resp = {"raw": r.text}

    # store payloads no matter what
    tx.request_payload = {**(tx.request_payload or {}), "mpesa_request": payload, "mpesa_response": resp}

    if r.status_code != 200:
        tx.status = "FAILED"
        tx.result_desc = "STK initiation failed"
        tx.save(update_fields=["status", "result_desc", "request_payload", "updated_at"])
        raise ValidationError(f"STK push failed: {resp}")

    checkout_id = resp.get("CheckoutRequestID")
    merchant_id = resp.get("MerchantRequestID")

    if not checkout_id:
        tx.status = "FAILED"
        tx.result_desc = "Missing CheckoutRequestID"
        tx.save(update_fields=["status", "result_desc", "request_payload", "updated_at"])
        raise ValidationError("STK push failed: missing CheckoutRequestID.")

    tx.checkout_request_id = checkout_id
    tx.merchant_request_id = merchant_id
    tx.status = "PENDING"
    tx.save(update_fields=["checkout_request_id", "merchant_request_id", "status", "request_payload", "updated_at"])

    return tx


@transaction.atomic
def handle_stk_callback(payload: dict) -> Optional[MpesaTransaction]:
    """
    ✅ EXACT signature your view uses.
    Idempotent:
      - safely updates tx
      - creates ONLY ONE ledger entry for that mpesa tx
    """
    try:
        cb = payload["Body"]["stkCallback"]
    except Exception:
        return None

    checkout_id = cb.get("CheckoutRequestID")
    merchant_id = cb.get("MerchantRequestID")

    tx = None
    if checkout_id:
        tx = MpesaTransaction.objects.select_for_update().filter(checkout_request_id=checkout_id).first()
    if not tx and merchant_id:
        tx = MpesaTransaction.objects.select_for_update().filter(merchant_request_id=merchant_id).first()
    if not tx:
        return None

    # Always store callback
    tx.callback_payload = payload

    result_code = str(cb.get("ResultCode")) if cb.get("ResultCode") is not None else ""
    result_desc = cb.get("ResultDesc", "") or ""
    tx.result_code = result_code
    tx.result_desc = result_desc

    # If already final, don't double post
    if tx.status in ("SUCCESS", "FAILED", "CANCELLED"):
        tx.save(update_fields=["callback_payload", "result_code", "result_desc", "updated_at"])
        return tx

    # Failed / cancelled
    if result_code != "0":
        tx.status = "CANCELLED" if result_code == "1032" else "FAILED"
        tx.save(update_fields=["status", "callback_payload", "result_code", "result_desc", "updated_at"])
        return tx

    # Success
    meta = _stk_callback_meta(payload)
    receipt = meta.get("MpesaReceiptNumber")
    trx_date = _parse_mpesa_datetime(meta.get("TransactionDate"))

    if receipt:
        tx.mpesa_receipt_number = str(receipt)
    if trx_date:
        tx.transaction_date = trx_date

    tx.status = "SUCCESS"
    tx.save(update_fields=[
        "status",
        "callback_payload",
        "result_code",
        "result_desc",
        "mpesa_receipt_number",
        "transaction_date",
        "updated_at",
    ])

    # ✅ Create ledger CREDIT once
    if not PaymentLedger.objects.filter(mpesa_tx=tx).exists():
        PaymentLedger.objects.create(
            user=tx.user,
            entry_type="CREDIT",
            category=purpose_to_ledger_category(tx.purpose),
            amount=tx.amount,
            narration=f"M-Pesa payment ({tx.purpose})",
            reference=tx.mpesa_receipt_number or f"MPESA_TX_{tx.id}",
            mpesa_tx=tx,
            target_content_type=tx.target_content_type,
            target_object_id=tx.target_object_id,
        )

    return tx


# ============================================================
# WITHDRAWALS: Admin approve -> B2C payout
# ============================================================

@transaction.atomic
def approve_withdrawal_and_start_payout(*, withdrawal: WithdrawalRequest, admin_user, data: Optional[dict] = None) -> WithdrawalRequest:
    """
    ✅ EXACT function name + signature your view imports/calls:
      approve_withdrawal_and_start_payout(withdrawal=w, admin_user=request.user, data=ser.validated_data)

    data is optional (you can use it later e.g. remarks, override phone, etc.)
    """
    cfg = get_mpesa_config()

    w = WithdrawalRequest.objects.select_for_update().select_related("user").get(id=withdrawal.id)

    if w.status != "PENDING":
        raise ValidationError("Only PENDING withdrawals can be approved.")

    # Optional override phone from validated data if you ever add it to serializer
    if data and data.get("phone"):
        w.phone = str(data["phone"])

    phone_norm = normalize_ke_phone(w.phone)
    w.phone = phone_norm

    # Mark approved
    w.status = "APPROVED"
    w.approved_by = admin_user
    w.approved_at = timezone.now()
    w.save(update_fields=["status", "approved_by", "approved_at", "phone", "updated_at"])

    # Create outgoing tx
    tx = MpesaTransaction.objects.create(
        user=w.user,
        phone=phone_norm,
        amount=w.amount,
        direction="OUT",
        channel="B2C",
        purpose="WITHDRAWAL",
        status="PENDING",
        reference=f"WITHDRAWAL:{w.id}",
        request_payload={"withdrawal_id": w.id, "approved_by": getattr(admin_user, "id", None), "data": data or {}},
        target_content_type=w.target_content_type,
        target_object_id=w.target_object_id,
    )

    # Call B2C API
    url = f"{cfg.base_url}/mpesa/b2c/v1/paymentrequest"

    # You can use serializer validated data to customize remarks if you add it later
    remarks = f"Withdrawal {w.id}"

    payload = {
        "InitiatorName": cfg.b2c_initiator_name,
        "SecurityCredential": cfg.b2c_security_credential,
        "CommandID": "BusinessPayment",
        "Amount": int(q2(w.amount)),
        "PartyA": cfg.b2c_shortcode,
        "PartyB": phone_norm,
        "Remarks": remarks,
        "QueueTimeOutURL": cfg.b2c_timeout_url,
        "ResultURL": cfg.b2c_result_url,
        "Occasion": f"W{w.id}",
    }

    r = requests.post(url, headers=_auth_headers(), data=json.dumps(payload), timeout=25)
    try:
        resp = r.json()
    except Exception:
        resp = {"raw": r.text}

    tx.request_payload = {**(tx.request_payload or {}), "mpesa_request": payload, "mpesa_response": resp}

    if r.status_code != 200:
        tx.status = "FAILED"
        tx.result_desc = "B2C initiation failed"
        tx.save(update_fields=["status", "result_desc", "request_payload", "updated_at"])

        w.status = "FAILED"
        w.save(update_fields=["status", "updated_at"])
        raise ValidationError(f"B2C initiation failed: {resp}")

    tx.conversation_id = resp.get("ConversationID") or ""
    tx.originator_conversation_id = resp.get("OriginatorConversationID") or ""
    tx.save(update_fields=["conversation_id", "originator_conversation_id", "request_payload", "updated_at"])

    # Link withdrawal and mark processing
    w.mpesa_tx = tx
    w.status = "PROCESSING"
    w.save(update_fields=["mpesa_tx", "status", "updated_at"])

    return w


@transaction.atomic
def handle_b2c_result(payload: dict) -> Optional[MpesaTransaction]:
    """
    ✅ EXACT signature your view uses.
    Updates:
      - MpesaTransaction
      - WithdrawalRequest
      - Ledger DEBIT once on success
    """
    result = payload.get("Result") or {}

    conv_id = result.get("ConversationID") or ""
    orig_id = result.get("OriginatorConversationID") or ""

    tx = None
    if conv_id:
        tx = MpesaTransaction.objects.select_for_update().filter(channel="B2C", conversation_id=conv_id).first()
    if not tx and orig_id:
        tx = MpesaTransaction.objects.select_for_update().filter(channel="B2C", originator_conversation_id=orig_id).first()
    if not tx:
        return None

    tx.callback_payload = payload

    result_code = str(result.get("ResultCode")) if result.get("ResultCode") is not None else ""
    result_desc = result.get("ResultDesc", "") or ""
    tx.result_code = result_code
    tx.result_desc = result_desc

    transaction_id = result.get("TransactionID")
    if transaction_id:
        tx.mpesa_receipt_number = str(transaction_id)

    # if already final, keep payload only
    if tx.status in ("SUCCESS", "FAILED", "CANCELLED"):
        tx.save(update_fields=["callback_payload", "result_code", "result_desc", "mpesa_receipt_number", "updated_at"])
        return tx

    tx.status = "SUCCESS" if result_code == "0" else "FAILED"
    tx.save(update_fields=["status", "callback_payload", "result_code", "result_desc", "mpesa_receipt_number", "updated_at"])

    w = WithdrawalRequest.objects.select_for_update().filter(mpesa_tx=tx).first()
    if not w:
        return tx

    if tx.status == "SUCCESS":
        w.status = "PAID"
        w.save(update_fields=["status", "updated_at"])

        if not PaymentLedger.objects.filter(mpesa_tx=tx).exists():
            PaymentLedger.objects.create(
                user=w.user,
                entry_type="DEBIT",
                category="WITHDRAWAL",
                amount=w.amount,
                narration="M-Pesa withdrawal payout",
                reference=tx.mpesa_receipt_number or f"B2C_TX_{tx.id}",
                mpesa_tx=tx,
                target_content_type=w.target_content_type,
                target_object_id=w.target_object_id,
            )
    else:
        w.status = "FAILED"
        w.save(update_fields=["status", "updated_at"])

    return tx


@transaction.atomic
def handle_b2c_timeout(payload: dict) -> Optional[MpesaTransaction]:
    """
    ✅ EXACT signature your view uses.

    NOTE: Your MpesaTransaction.STATUS_CHOICES does NOT include "TIMEOUT".
    So we mark FAILED and set result_desc="TIMEOUT".
    """
    result = payload.get("Result") or {}

    conv_id = (result.get("ConversationID") or payload.get("ConversationID") or "")
    orig_id = (result.get("OriginatorConversationID") or payload.get("OriginatorConversationID") or "")

    tx = None
    if conv_id:
        tx = MpesaTransaction.objects.select_for_update().filter(channel="B2C", conversation_id=conv_id).first()
    if not tx and orig_id:
        tx = MpesaTransaction.objects.select_for_update().filter(channel="B2C", originator_conversation_id=orig_id).first()
    if not tx:
        return None

    # final? just store payload
    if tx.status in ("SUCCESS", "FAILED", "CANCELLED"):
        tx.callback_payload = payload
        if not tx.result_desc:
            tx.result_desc = "TIMEOUT"
        tx.save(update_fields=["callback_payload", "result_desc", "updated_at"])
        return tx

    tx.callback_payload = payload
    tx.status = "FAILED"
    tx.result_desc = "TIMEOUT"
    tx.save(update_fields=["callback_payload", "status", "result_desc", "updated_at"])

    w = WithdrawalRequest.objects.select_for_update().filter(mpesa_tx=tx).first()
    if w and w.status == "PROCESSING":
        w.status = "FAILED"
        w.save(update_fields=["status", "updated_at"])

    return tx