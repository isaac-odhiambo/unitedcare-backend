# payments/mpesa_c2b.py
from __future__ import annotations

import base64
from urllib.parse import urlencode

import requests
from django.conf import settings


class MpesaC2BError(Exception):
    pass


if str(getattr(settings, "MPESA_ENV", "sandbox")).strip().lower() == "production":
    BASE_URL = "https://api.safaricom.co.ke"
else:
    BASE_URL = "https://sandbox.safaricom.co.ke"

OAUTH_URL = f"{BASE_URL}/oauth/v1/generate?grant_type=client_credentials"
REGISTER_URL = f"{BASE_URL}/mpesa/c2b/v2/registerurl"


def _clean(value: object) -> str:
    return str(value or "").strip()


def _get_required_setting(name: str) -> str:
    value = _clean(getattr(settings, name, ""))
    if not value:
        raise MpesaC2BError(f"{name} must be set")
    return value


def _get_callback_base_url() -> str:
    """
    Preferred source:
      MPESA_CALLBACK_BASE_URL=https://unitedcare-backend.onrender.com

    Falls back to deriving base URL from explicit callback URLs if present.
    """
    base = _clean(getattr(settings, "MPESA_CALLBACK_BASE_URL", "")).rstrip("/")
    if base:
        return base

    # Fallback from explicit validation/confirmation URLs if already configured
    validation_url = _clean(getattr(settings, "MPESA_C2B_VALIDATION_URL", ""))
    confirmation_url = _clean(getattr(settings, "MPESA_C2B_CONFIRMATION_URL", ""))

    sample = validation_url or confirmation_url
    if sample:
        # trim path part if one of the explicit URLs exists
        for suffix in (
            "/payments/c2b/validation/",
            "/payments/c2b/confirmation/",
            "/payments/mpesa/c2b/validation/",
            "/payments/mpesa/c2b/confirmation/",
        ):
            idx = sample.find(suffix)
            if idx != -1:
                return sample[:idx].rstrip("/")

    raise MpesaC2BError(
        "Set MPESA_CALLBACK_BASE_URL or provide explicit MPESA_C2B_VALIDATION_URL / MPESA_C2B_CONFIRMATION_URL"
    )


def _get_callback_token() -> str:
    return _clean(getattr(settings, "MPESA_CALLBACK_TOKEN", ""))


def _build_url(path: str) -> str:
    """
    Builds callback URL using the CORRECT Django routes:
      /payments/c2b/validation/
      /payments/c2b/confirmation/
    """
    base = _get_callback_base_url()
    token = _get_callback_token()

    url = f"{base}{path}"
    if token:
        url = f"{url}?{urlencode({'token': token})}"
    return url


def get_validation_url() -> str:
    """
    Uses explicit setting if provided; otherwise builds the correct route.
    """
    explicit = _clean(getattr(settings, "MPESA_C2B_VALIDATION_URL", ""))
    if explicit:
        return explicit
    return _build_url("/payments/c2b/validation/")


def get_confirmation_url() -> str:
    """
    Uses explicit setting if provided; otherwise builds the correct route.
    """
    explicit = _clean(getattr(settings, "MPESA_C2B_CONFIRMATION_URL", ""))
    if explicit:
        return explicit
    return _build_url("/payments/c2b/confirmation/")


def get_mpesa_access_token() -> str:
    consumer_key = _get_required_setting("MPESA_CONSUMER_KEY")
    consumer_secret = _get_required_setting("MPESA_CONSUMER_SECRET")

    credentials = f"{consumer_key}:{consumer_secret}"
    encoded_credentials = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")

    headers = {
        "Authorization": f"Basic {encoded_credentials}",
        "Accept": "application/json",
    }

    try:
        response = requests.get(OAUTH_URL, headers=headers, timeout=30)
    except requests.RequestException as e:
        raise MpesaC2BError(f"Failed to request M-Pesa access token: {e}")

    try:
        response.raise_for_status()
    except requests.HTTPError:
        raise MpesaC2BError(
            f"Failed to get M-Pesa access token. "
            f"Status: {response.status_code}. Body: {response.text}"
        )

    try:
        data = response.json()
    except ValueError:
        raise MpesaC2BError(f"Access token response was not valid JSON: {response.text!r}")

    access_token = _clean(data.get("access_token"))
    if not access_token:
        raise MpesaC2BError(f"No access token returned. Body: {data}")

    return access_token


def register_c2b_urls() -> dict:
    access_token = get_mpesa_access_token()
    shortcode = _get_required_setting("MPESA_SHORTCODE")

    validation_url = get_validation_url()
    confirmation_url = get_confirmation_url()

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload = {
        "ShortCode": shortcode,
        "ResponseType": "Completed",
        "ConfirmationURL": confirmation_url,
        "ValidationURL": validation_url,
    }

    print("=== C2B Register Debug ===")
    print("MPESA_ENV:", _clean(getattr(settings, "MPESA_ENV", "sandbox")))
    print("REGISTER_URL:", REGISTER_URL)
    print("Validation URL:", validation_url)
    print("Confirmation URL:", confirmation_url)
    print("Payload:", payload)

    try:
        response = requests.post(
            REGISTER_URL,
            json=payload,
            headers=headers,
            timeout=30,
        )
    except requests.RequestException as e:
        raise MpesaC2BError(f"Failed to register C2B URLs: {e}")

    try:
        response.raise_for_status()
    except requests.HTTPError:
        raise MpesaC2BError(
            f"Failed to register C2B URLs. "
            f"Status: {response.status_code}. "
            f"Body: {response.text}. "
            f"Payload: {payload}"
        )

    try:
        data = response.json()
    except ValueError:
        raise MpesaC2BError(
            f"C2B register response was not valid JSON. "
            f"Status: {response.status_code}. Body: {response.text}"
        )

    return {
        "request_payload": payload,
        "response": data,
    }