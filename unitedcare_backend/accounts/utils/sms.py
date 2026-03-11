import africastalking
from django.conf import settings


# Initialize Africa's Talking
africastalking.initialize(
    username=settings.AFRICASTALKING_USERNAME,
    api_key=settings.AFRICASTALKING_API_KEY,
)

sms = africastalking.SMS


def normalize_kenyan_phone(phone: str) -> str:
    """
    Normalize Kenyan phone numbers to international format.
    Examples:
    0712345678   -> +254712345678
    0112345678   -> +254112345678
    254712345678 -> +254712345678
    +254712345678 -> +254712345678
    """
    if not phone:
        raise ValueError("Phone number is required.")

    phone = str(phone).strip().replace(" ", "").replace("-", "")

    if phone.startswith("+254") and len(phone) == 13:
        return phone

    if phone.startswith("254") and len(phone) == 12:
        return f"+{phone}"

    if phone.startswith("0") and len(phone) == 10:
        return f"+254{phone[1:]}"

    raise ValueError(f"Invalid Kenyan phone number format: {phone}")


def send_sms(phone: str, message: str):
    """
    Send SMS using Africa's Talking with proper validation and logging.

    Returns:
        dict: Africa's Talking response on success

    Raises:
        ValueError: for invalid input
        Exception: when provider rejects or fails to send SMS
    """
    print("📌 SMS DEBUG START")
    print("Raw phone:", phone)
    print("Message:", message)

    if not message or not str(message).strip():
        print("❌ SMS FAILED")
        print("❌ ERROR: SMS message is empty.")
        print("📌 SMS DEBUG END\n")
        raise ValueError("SMS message cannot be empty.")

    normalized_phone = normalize_kenyan_phone(phone)

    sender_id = getattr(settings, "AFRICASTALKING_SENDER_ID", "") or ""
    sender_id = sender_id.strip()

    print("Normalized phone:", normalized_phone)
    print("Sender ID:", sender_id if sender_id else "(none)")
    print("AT Username:", settings.AFRICASTALKING_USERNAME)

    try:
        print("📤 Sending SMS...")

        # Match the working curl behavior:
        # only include sender_id if it is present and intended
        if sender_id:
            response = sms.send(
                message,
                [normalized_phone],
                sender_id=sender_id,
            )
        else:
            response = sms.send(
                message,
                [normalized_phone],
            )

        print("📨 Africa's Talking Response:", response)

        data = response.get("SMSMessageData", {}) if isinstance(response, dict) else {}
        provider_message = data.get("Message", "")
        recipients = data.get("Recipients", []) or []

        sent_ok = any(
            str(recipient.get("status", "")).strip().lower() == "success"
            for recipient in recipients
        )

        if not sent_ok:
            error_msg = provider_message or "SMS sending failed."
            print("❌ SMS FAILED")
            print("❌ ERROR:", error_msg)
            print("📌 SMS DEBUG END\n")
            raise Exception(f"Africa's Talking SMS failed: {error_msg}")

        print("✅ SMS SENT SUCCESSFULLY")
        print("📌 SMS DEBUG END\n")
        return response

    except Exception as e:
        print("❌ SMS FAILED")
        print("❌ ERROR:", str(e))
        print("📌 SMS DEBUG END\n")
        raise


# import africastalking
# from django.conf import settings

# # Initialize Africa's Talking
# africastalking.initialize(
#     settings.AFRICASTALKING_USERNAME,
#     settings.AFRICASTALKING_API_KEY
# )

# sms = africastalking.SMS


# def send_sms(phone: str, message: str):
#     """
#     Send SMS using Africa's Talking
#     """
#     try:
#         response = sms.send(
#             message,
#             [phone],
#             sender_id=settings.AFRICASTALKING_SENDER_ID
#         )
#         return response
#     except Exception as e:
#         # Log this properly in production
#         print("SMS ERROR:", e)
#         return None
