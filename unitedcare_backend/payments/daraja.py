# payments/daraja.py
from __future__ import annotations

import base64
import requests
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime
from typing import Dict, Any

from django.conf import settings


# ============================================================
# Data Classes
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


# ============================================================
# Custom Exception
# ============================================================

class DarajaError(Exception):
    pass


# ============================================================
# Daraja Client
# ============================================================

class DarajaClient:
    """
    Supports:
      - STK Push
      - STK Query (IMPORTANT for security)
      - B2C payout

    Required settings:
      DARAJA_ENV = "sandbox" | "production"
      DARAJA_CONSUMER_KEY
      DARAJA_CONSUMER_SECRET

      STK_SHORTCODE
      STK_PASSKEY

      B2C_SHORTCODE
      B2C_INITIATOR_NAME
      B2C_SECURITY_CREDENTIAL
      B2C_COMMAND_ID
    """

    def __init__(self):
        env = getattr(settings, "DARAJA_ENV", "sandbox").lower().strip()

        if env == "production":
            self.base_url = "https://api.safaricom.co.ke"
        else:
            self.base_url = "https://sandbox.safaricom.co.ke"

        self.consumer_key = getattr(settings, "DARAJA_CONSUMER_KEY", "")
        self.consumer_secret = getattr(settings, "DARAJA_CONSUMER_SECRET", "")

        if not self.consumer_key or not self.consumer_secret:
            raise DarajaError("Missing DARAJA_CONSUMER_KEY or DARAJA_CONSUMER_SECRET")

    # ============================================================
    # Access Token
    # ============================================================

    def _get_access_token(self) -> str:
        url = f"{self.base_url}/oauth/v1/generate?grant_type=client_credentials"

        r = requests.get(
            url,
            auth=(self.consumer_key, self.consumer_secret),
            timeout=30,
        )

        if r.status_code != 200:
            raise DarajaError(f"Access token failed: {r.status_code} {r.text}")

        data = r.json()
        token = data.get("access_token")

        if not token:
            raise DarajaError("Access token missing in response")

        return token

    def _headers(self) -> Dict[str, str]:
        token = self._get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # ============================================================
    # Phone Normalization
    # ============================================================

    @staticmethod
    def _normalize_msisdn(phone: str) -> str:
        p = (phone or "").strip()

        if p.startswith("+"):
            p = p[1:]

        if p.startswith("0"):
            p = "254" + p[1:]

        return p

    # ============================================================
    # STK PUSH
    # ============================================================

    def stk_push(
        self,
        *,
        phone: str,
        amount: Decimal,
        account_reference: str,
        transaction_desc: str,
        callback_url: str,
    ) -> STKPushResult:

        shortcode = str(getattr(settings, "STK_SHORTCODE", "")).strip()
        passkey = str(getattr(settings, "STK_PASSKEY", "")).strip()

        if not shortcode or not passkey:
            raise DarajaError("Missing STK_SHORTCODE or STK_PASSKEY")

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

        password_raw = f"{shortcode}{passkey}{timestamp}"
        password = base64.b64encode(password_raw.encode()).decode()

        msisdn = self._normalize_msisdn(phone)

        payload: Dict[str, Any] = {
            "BusinessShortCode": shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": int(Decimal(amount)),
            "PartyA": msisdn,
            "PartyB": shortcode,
            "PhoneNumber": msisdn,
            "CallBackURL": callback_url,
            "AccountReference": account_reference[:32],
            "TransactionDesc": transaction_desc[:32],
        }

        url = f"{self.base_url}/mpesa/stkpush/v1/processrequest"

        r = requests.post(url, json=payload, headers=self._headers(), timeout=30)

        if r.status_code != 200:
            raise DarajaError(f"STK push failed: {r.status_code} {r.text}")

        data = r.json()

        if data.get("ResponseCode") != "0":
            raise DarajaError(f"STK push rejected: {data}")

        return STKPushResult(
            merchant_request_id=data.get("MerchantRequestID", ""),
            checkout_request_id=data.get("CheckoutRequestID", ""),
            customer_message=data.get("CustomerMessage", "") or "",
        )

    # ============================================================
    # 🔐 STK QUERY (SECURITY CRITICAL)
    # ============================================================

    def stk_query(self, *, checkout_request_id: str) -> Dict[str, Any]:
        """
        Verifies STK transaction status directly with Safaricom.
        Used to prevent fake callback crediting.
        """

        shortcode = str(getattr(settings, "STK_SHORTCODE", "")).strip()
        passkey = str(getattr(settings, "STK_PASSKEY", "")).strip()

        if not shortcode or not passkey:
            raise DarajaError("Missing STK_SHORTCODE or STK_PASSKEY")

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        password_raw = f"{shortcode}{passkey}{timestamp}"
        password = base64.b64encode(password_raw.encode()).decode()

        payload = {
            "BusinessShortCode": shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "CheckoutRequestID": checkout_request_id,
        }

        url = f"{self.base_url}/mpesa/stkpushquery/v1/query"

        r = requests.post(url, json=payload, headers=self._headers(), timeout=30)

        if r.status_code != 200:
            raise DarajaError(f"STK query failed: {r.status_code} {r.text}")

        data = r.json()

        # Expected response:
        # {
        #   "ResponseCode": "0",
        #   "ResultCode": "0",
        #   "ResultDesc": "The service request is processed successfully.",
        #   ...
        # }

        return data

    # ============================================================
    # B2C PAYOUT
    # ============================================================

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

        shortcode = str(getattr(settings, "B2C_SHORTCODE", "")).strip()
        initiator = str(getattr(settings, "B2C_INITIATOR_NAME", "")).strip()
        security_credential = str(getattr(settings, "B2C_SECURITY_CREDENTIAL", "")).strip()
        command_id = str(getattr(settings, "B2C_COMMAND_ID", "BusinessPayment")).strip()

        if not shortcode or not security_credential or not initiator:
            raise DarajaError("Missing B2C configuration")

        msisdn = self._normalize_msisdn(phone)

        payload: Dict[str, Any] = {
            "InitiatorName": initiator,
            "SecurityCredential": security_credential,
            "CommandID": command_id,
            "Amount": int(Decimal(amount)),
            "PartyA": shortcode,
            "PartyB": msisdn,
            "Remarks": remarks[:100],
            "QueueTimeOutURL": timeout_url,
            "ResultURL": result_url,
            "Occasion": occasion[:100],
        }

        url = f"{self.base_url}/mpesa/b2c/v1/paymentrequest"

        r = requests.post(url, json=payload, headers=self._headers(), timeout=30)

        if r.status_code != 200:
            raise DarajaError(f"B2C failed: {r.status_code} {r.text}")

        data = r.json()

        return B2CResult(
            conversation_id=data.get("ConversationID", ""),
            originator_conversation_id=data.get("OriginatorConversationID", ""),
            response_description=data.get("ResponseDescription", ""),
        )