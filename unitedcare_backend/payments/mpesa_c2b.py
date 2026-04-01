# payments/mpesa_c2b.py
import base64
import requests
from django.conf import settings


if settings.MPESA_ENV == "production":
    BASE_URL = "https://api.safaricom.co.ke"
else:
    BASE_URL = "https://sandbox.safaricom.co.ke"

OAUTH_URL = f"{BASE_URL}/oauth/v1/generate?grant_type=client_credentials"
REGISTER_URL = f"{BASE_URL}/mpesa/c2b/v2/registerurl"


def get_mpesa_access_token() -> str:
    consumer_key = settings.MPESA_CONSUMER_KEY
    consumer_secret = settings.MPESA_CONSUMER_SECRET

    if not consumer_key or not consumer_secret:
        raise ValueError("MPESA_CONSUMER_KEY and MPESA_CONSUMER_SECRET must be set")

    credentials = f"{consumer_key}:{consumer_secret}"
    encoded_credentials = base64.b64encode(credentials.encode()).decode()

    headers = {
        "Authorization": f"Basic {encoded_credentials}",
    }

    response = requests.get(OAUTH_URL, headers=headers, timeout=30)

    try:
        response.raise_for_status()
    except requests.HTTPError:
        raise Exception(
            f"Failed to get M-Pesa access token. "
            f"Status: {response.status_code}. Body: {response.text}"
        )

    data = response.json()
    access_token = data.get("access_token")
    if not access_token:
        raise Exception(f"No access token returned. Body: {data}")

    return access_token


def register_c2b_urls() -> dict:
    access_token = get_mpesa_access_token()

    shortcode = str(settings.MPESA_SHORTCODE).strip()
    validation_url = str(getattr(settings, "MPESA_C2B_VALIDATION_URL", "")).strip()
    confirmation_url = str(getattr(settings, "MPESA_C2B_CONFIRMATION_URL", "")).strip()

    if not shortcode:
        raise ValueError("MPESA_SHORTCODE must be set")

    if not validation_url or not confirmation_url:
        raise ValueError("C2B callback URLs are not configured")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    payload = {
        "ShortCode": shortcode,
        "ResponseType": "Completed",
        "ConfirmationURL": confirmation_url,
        "ValidationURL": validation_url,
    }

    print("=== C2B Register Debug ===")
    print("MPESA_ENV:", settings.MPESA_ENV)
    print("REGISTER_URL:", REGISTER_URL)
    print("Payload:", payload)

    response = requests.post(
        REGISTER_URL,
        json=payload,
        headers=headers,
        timeout=30,
    )

    try:
        response.raise_for_status()
    except requests.HTTPError:
        raise Exception(
            f"Failed to register C2B URLs. "
            f"Status: {response.status_code}. "
            f"Body: {response.text}. "
            f"Payload: {payload}"
        )

    return response.json()