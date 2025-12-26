import africastalking
from django.conf import settings

# Initialize Africa's Talking
africastalking.initialize(
    settings.AFRICASTALKING_USERNAME,
    settings.AFRICASTALKING_API_KEY
)

sms = africastalking.SMS


def send_sms(phone: str, message: str):
    """
    Send SMS using Africa's Talking (with debugging logs)
    """

    # 🔎 DEBUG: show raw inputs
    print("📌 SMS DEBUG START")
    print("Raw phone:", phone)
    print("Message:", message)

    # 🔄 Normalize phone to international format
    if phone.startswith("0"):
        phone = "+254" + phone[1:]

    print("Normalized phone:", phone)
    print("Sender ID:", settings.AFRICASTALKING_SENDER_ID)
    print("AT Username:", settings.AFRICASTALKING_USERNAME)

    try:
        print("📤 Sending SMS...")

        response = sms.send(
            message,
            [phone],
            sender_id=settings.AFRICASTALKING_SENDER_ID
        )

        print("✅ SMS SENT SUCCESSFULLY")
        print("📨 Africa's Talking Response:", response)
        print("📌 SMS DEBUG END\n")

        return response

    except Exception as e:
        print("❌ SMS FAILED")
        print("❌ ERROR:", str(e))
        print("📌 SMS DEBUG END\n")
        return None


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
