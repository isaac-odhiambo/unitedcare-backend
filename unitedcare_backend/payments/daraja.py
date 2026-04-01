from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict

import requests
from django.conf import settings
from django.core.cache import cache


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


class DarajaError(Exception):
    pass


class DarajaClient:
    """
    Supports:
      - STK Push
      - STK Query
      - B2C payout

    Required settings:
      MPESA_ENV = "sandbox" | "production"
      MPESA_CONSUMER_KEY
      MPESA_CONSUMER_SECRET

      MPESA_SHORTCODE
      MPESA_PASSKEY

      B2C_SHORTCODE
      B2C_INITIATOR_NAME
      B2C_SECURITY_CREDENTIAL
      B2C_COMMAND_ID
    """

    TOKEN_CACHE_KEY = "daraja_access_token"

    def __init__(self):
        env = str(getattr(settings, "MPESA_ENV", "sandbox") or "sandbox").lower().strip()

        if env == "production":
            self.base_url = "https://api.safaricom.co.ke"
        else:
            self.base_url = "https://sandbox.safaricom.co.ke"

        self.consumer_key = str(getattr(settings, "MPESA_CONSUMER_KEY", "") or "").strip()
        self.consumer_secret = str(getattr(settings, "MPESA_CONSUMER_SECRET", "") or "").strip()
        self.timeout = int(getattr(settings, "DARAJA_TIMEOUT", 30) or 30)

        if not self.consumer_key or not self.consumer_secret:
            raise DarajaError("Missing MPESA_CONSUMER_KEY or MPESA_CONSUMER_SECRET")

    def _get_access_token(self) -> str:
        cached_token = cache.get(self.TOKEN_CACHE_KEY)
        if cached_token:
            return str(cached_token).strip()

        url = f"{self.base_url}/oauth/v1/generate?grant_type=client_credentials"

        auth_string = f"{self.consumer_key}:{self.consumer_secret}"
        encoded_auth = base64.b64encode(auth_string.encode("utf-8")).decode("utf-8")

        headers = {
            "Authorization": f"Basic {encoded_auth}",
            "Accept": "application/json",
        }

        try:
            r = requests.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise DarajaError(f"Access token request error: {e}")

        if r.status_code != 200:
            raise DarajaError(
                f"Access token failed: {r.status_code} | response={r.text!r} | "
                f"env={getattr(settings, 'MPESA_ENV', '')!r} | "
                f"base_url={self.base_url!r}"
            )

        try:
            data = r.json()
        except ValueError:
            raise DarajaError(f"Access token response was not valid JSON: {r.text!r}")

        token = str(data.get("access_token", "") or "").strip()
        expires_in_raw = data.get("expires_in", 3599)

        if not token:
            raise DarajaError(f"Access token missing in response: {data}")

        try:
            expires_in = int(expires_in_raw)
        except (TypeError, ValueError):
            expires_in = 3599

        cache_timeout = max(expires_in - 60, 60)
        cache.set(self.TOKEN_CACHE_KEY, token, timeout=cache_timeout)

        return token

    def _headers(self) -> Dict[str, str]:
        token = self._get_access_token().strip()
        if not token:
            raise DarajaError("Bearer token is empty")

        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def _normalize_msisdn(phone: str) -> str:
        p = str(phone or "").strip()

        if p.startswith("+"):
            p = p[1:]

        if p.startswith("0") and len(p) == 10:
            p = "254" + p[1:]
        elif p.startswith("7") and len(p) == 9:
            p = "254" + p

        if not (p.isdigit() and len(p) == 12 and p.startswith("254")):
            raise DarajaError(f"Invalid Kenyan phone format for MPesa: {phone!r}")

        return p

    @staticmethod
    def _timestamp() -> str:
        return datetime.now().strftime("%Y%m%d%H%M%S")

    @staticmethod
    def _amount_to_int(amount: Decimal) -> int:
        try:
            amt = Decimal(amount)
        except Exception:
            raise DarajaError(f"Invalid amount: {amount!r}")

        if amt <= 0:
            raise DarajaError("Amount must be greater than zero")

        return int(amt)

    def _stk_password(self) -> tuple[str, str, str]:
        shortcode = str(getattr(settings, "MPESA_SHORTCODE", "") or "").strip()
        passkey = str(getattr(settings, "MPESA_PASSKEY", "") or "").strip()

        if not shortcode or not passkey:
            raise DarajaError("Missing MPESA_SHORTCODE or MPESA_PASSKEY")

        timestamp = self._timestamp()
        password_raw = f"{shortcode}{passkey}{timestamp}"
        password = base64.b64encode(password_raw.encode("utf-8")).decode("utf-8")

        return shortcode, timestamp, password

    def stk_push(
        self,
        *,
        phone: str,
        amount: Decimal,
        account_reference: str,
        transaction_desc: str,
        callback_url: str,
    ) -> STKPushResult:

        shortcode, timestamp, password = self._stk_password()
        msisdn = self._normalize_msisdn(phone)

        callback_url = str(callback_url or "").strip()
        if not callback_url:
            raise DarajaError("Callback URL is required")

        payload: Dict[str, Any] = {
            "BusinessShortCode": shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "TransactionType": str(
                getattr(settings, "STK_TRANSACTION_TYPE", "CustomerPayBillOnline")
                or "CustomerPayBillOnline"
            ).strip(),
            "Amount": self._amount_to_int(Decimal(amount)),
            "PartyA": msisdn,
            "PartyB": shortcode,
            "PhoneNumber": msisdn,
            "CallBackURL": callback_url,
            "AccountReference": str(account_reference or "")[:32],
            "TransactionDesc": str(transaction_desc or "Payment")[:100],
        }

        url = f"{self.base_url}/mpesa/stkpush/v1/processrequest"

        try:
            r = requests.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as e:
            raise DarajaError(f"STK push request error: {e}")

        if r.status_code != 200:
            raise DarajaError(f"STK push failed: {r.status_code} {r.text}")

        try:
            data = r.json()
        except ValueError:
            raise DarajaError(f"STK push response was not valid JSON: {r.text!r}")

        if data.get("ResponseCode") != "0":
            raise DarajaError(f"STK push rejected: {data}")

        return STKPushResult(
            merchant_request_id=str(data.get("MerchantRequestID", "") or ""),
            checkout_request_id=str(data.get("CheckoutRequestID", "") or ""),
            customer_message=str(data.get("CustomerMessage", "") or ""),
        )

    def stk_query(self, *, checkout_request_id: str) -> Dict[str, Any]:
        shortcode, timestamp, password = self._stk_password()

        checkout_request_id = str(checkout_request_id or "").strip()
        if not checkout_request_id:
            raise DarajaError("CheckoutRequestID is required")

        payload = {
            "BusinessShortCode": shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "CheckoutRequestID": checkout_request_id,
        }

        url = f"{self.base_url}/mpesa/stkpushquery/v1/query"

        try:
            r = requests.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as e:
            raise DarajaError(f"STK query request error: {e}")

        if r.status_code != 200:
            raise DarajaError(f"STK query failed: {r.status_code} {r.text}")

        try:
            data = r.json()
        except ValueError:
            raise DarajaError(f"STK query response was not valid JSON: {r.text!r}")

        return data

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

        shortcode = str(getattr(settings, "B2C_SHORTCODE", "") or "").strip()
        initiator = str(getattr(settings, "B2C_INITIATOR_NAME", "") or "").strip()
        security_credential = str(getattr(settings, "B2C_SECURITY_CREDENTIAL", "") or "").strip()
        command_id = str(getattr(settings, "B2C_COMMAND_ID", "BusinessPayment") or "BusinessPayment").strip()

        if not shortcode or not security_credential or not initiator:
            raise DarajaError("Missing B2C configuration")

        result_url = str(result_url or "").strip()
        timeout_url = str(timeout_url or "").strip()
        if not result_url or not timeout_url:
            raise DarajaError("Both result_url and timeout_url are required for B2C")

        msisdn = self._normalize_msisdn(phone)

        payload: Dict[str, Any] = {
            "InitiatorName": initiator,
            "SecurityCredential": security_credential,
            "CommandID": command_id,
            "Amount": self._amount_to_int(Decimal(amount)),
            "PartyA": shortcode,
            "PartyB": msisdn,
            "Remarks": str(remarks or "")[:100],
            "QueueTimeOutURL": timeout_url,
            "ResultURL": result_url,
            "Occasion": str(occasion or "")[:100],
        }

        url = f"{self.base_url}/mpesa/b2c/v1/paymentrequest"

        try:
            r = requests.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as e:
            raise DarajaError(f"B2C request error: {e}")

        if r.status_code != 200:
            raise DarajaError(f"B2C failed: {r.status_code} {r.text}")

        try:
            data = r.json()
        except ValueError:
            raise DarajaError(f"B2C response was not valid JSON: {r.text!r}")

        return B2CResult(
            conversation_id=str(data.get("ConversationID", "") or ""),
            originator_conversation_id=str(data.get("OriginatorConversationID", "") or ""),
            response_description=str(data.get("ResponseDescription", "") or ""),
        )