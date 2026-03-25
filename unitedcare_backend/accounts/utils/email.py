from django.conf import settings
from django.core.mail import send_mail


# =========================================================
# 🔐 PASSWORD RESET EMAIL
# =========================================================
def send_password_reset_email(email: str, code: str, expiry_minutes: int = 10) -> None:
    """
    Send password reset email with a one-time code.
    """

    subject = "United Care Password Reset Code"

    message = (
        "Hello,\n\n"
        "You requested to reset your United Care account password.\n\n"
        f"Your password reset code is: {code}\n\n"
        f"This code will expire in {expiry_minutes} minutes.\n\n"
        "If you did not request this change, please ignore this email.\n\n"
        "— United Care"
    )

    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=False,
    )


# =========================================================
# ✅ ACCOUNT APPROVED EMAIL
# =========================================================
def send_account_approved_email(email: str, username: str = "") -> None:
    """
    Send email when user account is approved by admin.
    """

    subject = "United Care Account Approved"

    name = username.strip() if username else "Member"

    message = (
        f"Hello {name},\n\n"
        "Your United Care account has been approved successfully.\n\n"
        "You can now log in and continue using the platform.\n\n"
        "If you have completed KYC and other requirements, "
        "you now have full access.\n\n"
        "— United Care"
    )

    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=False,
    )


# =========================================================
# 🎉 OPTIONAL: WELCOME EMAIL (FUTURE USE)
# =========================================================
def send_welcome_email(email: str, username: str = "") -> None:
    """
    Optional: Send welcome email after registration.
    Not yet used, but ready for future.
    """

    subject = "Welcome to United Care"

    name = username.strip() if username else "Member"

    message = (
        f"Hello {name},\n\n"
        "Welcome to United Care Self-Group.\n\n"
        "You can now join groups, contribute, and grow together.\n\n"
        "— United Care Team"
    )

    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=True,  # optional emails shouldn't break flow
    )