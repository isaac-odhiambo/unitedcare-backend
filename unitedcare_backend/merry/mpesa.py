import requests
from django.conf import settings
from datetime import datetime
from base64 import b64encode


def stk_push(phone, amount, reference):
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

    password = b64encode(
        (
            settings.MPESA_SHORTCODE +
            settings.MPESA_PASSKEY +
            timestamp
        ).encode()
    ).decode()

    payload = {
        "BusinessShortCode": settings.MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": amount,
        "PartyA": phone,
        "PartyB": settings.MPESA_SHORTCODE,
        "PhoneNumber": phone,
        "CallBackURL": settings.MPESA_CALLBACK_URL,
        "AccountReference": reference,
        "TransactionDesc": "Chama Contribution"
    }

    response = requests.post(
        settings.MPESA_STK_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {settings.MPESA_ACCESS_TOKEN}"
        }
    )

    return response.json()