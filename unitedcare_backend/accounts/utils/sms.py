import africastalking
from django.conf import settings


# Initialize Africa's Talking
africastalking.initialize(
    username=settings.AFRICASTALKING_USERNAME,
    api_key=settings.AFRICASTALKING_API_KEY,
)

sms = africastalking.SMS


class SMSDeliveryError(Exception):
    """
    Raised when SMS provider accepts the request but does not successfully
    deliver/send to the intended recipient.
    """
    pass


def normalize_kenyan_phone(phone: str) -> str:
    """
    Normalize Kenyan phone numbers to international format.

    Examples:
    0712345678     -> +254712345678
    0112345678     -> +254112345678
    254712345678   -> +254712345678
    +254712345678  -> +254712345678
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


def extract_sms_error(response: dict) -> str:
    """
    Build a meaningful provider error from Africa's Talking response.
    """
    if not isinstance(response, dict):
        return "SMS provider returned an invalid response."

    data = response.get("SMSMessageData", {}) or {}
    provider_message = str(data.get("Message", "")).strip()
    recipients = data.get("Recipients", []) or []

    if recipients:
        recipient_errors = []
        for recipient in recipients:
            number = recipient.get("number", "")
            status = str(recipient.get("status", "")).strip()
            message_id = recipient.get("messageId", "")
            cost = recipient.get("cost", "")

            parts = []
            if number:
                parts.append(str(number))
            if status:
                parts.append(status)
            if message_id:
                parts.append(f"messageId={message_id}")
            if cost:
                parts.append(f"cost={cost}")

            if parts:
                recipient_errors.append(" | ".join(parts))

        if recipient_errors:
            return f"{provider_message or 'SMS sending failed.'} :: {' ; '.join(recipient_errors)}"

    return provider_message or "SMS sending failed."


def send_sms(phone: str, message: str) -> dict:
    """
    Send SMS using Africa's Talking with validation and structured failure handling.

    Returns:
        dict: Africa's Talking response on success

    Raises:
        ValueError: for invalid phone/message input
        SMSDeliveryError: when provider rejects or fails delivery
        Exception: for unexpected provider/runtime issues
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
        recipients = data.get("Recipients", []) or []

        sent_ok = any(
            str(recipient.get("status", "")).strip().lower() == "success"
            for recipient in recipients
        )

        if not sent_ok:
            error_msg = extract_sms_error(response)
            print("❌ SMS FAILED")
            print("❌ ERROR:", error_msg)
            print("📌 SMS DEBUG END\n")
            raise SMSDeliveryError(error_msg)

        print("✅ SMS SENT SUCCESSFULLY")
        print("📌 SMS DEBUG END\n")
        return response

    except (ValueError, SMSDeliveryError):
        raise

    except Exception as e:
        print("❌ SMS FAILED")
        print("❌ ERROR:", str(e))
        print("📌 SMS DEBUG END\n")
        raise Exception(f"Unexpected SMS error: {str(e)}")