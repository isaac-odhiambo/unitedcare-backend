from datetime import timedelta
import random

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import render
from django.utils import timezone

from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .models import OTP, KYCProfile
from .serializers import (
    RegisterSerializer,
    LoginSerializer,
    ForgotPasswordSerializer,
    ResendOTPSerializer,
    MeSerializer,
    UpdateMeSerializer,
    KYCSubmitSerializer,
)
from .throttles import LoginThrottle, OTPThrottle
from .utils.email import send_password_reset_email
from .utils.phone import normalize_kenyan_phone
from .utils.sms import send_sms

UserModel = get_user_model()

# =========================
# CONSTANTS
# =========================
OTP_COOLDOWN_SECONDS = 60
OTP_MAX_PER_HOUR = 5
MAX_OTP_ATTEMPTS = 5
MAX_LOGIN_ATTEMPTS = 5
STALE_UNVERIFIED_ACCOUNT_MINUTES = 10

PASSWORD_RESET_CODE_EXPIRY_SECONDS = 10 * 60  # 10 minutes
PASSWORD_RESET_MAX_PER_HOUR = 5
PASSWORD_RESET_CACHE_PREFIX = "pwd_reset"


# =========================
# PHONE HELPERS
# =========================
def normalize_local_kenyan_phone(phone: str) -> str:
    """
    Normalize to local DB format only:
    07XXXXXXXX or 01XXXXXXXX

    Examples:
    +254701234567 -> 0701234567
    254701234567  -> 0701234567
    0701234567    -> 0701234567
    """
    phone = str(phone or "").strip().replace(" ", "")

    if phone.startswith("+254") and len(phone) == 13:
        return "0" + phone[4:]
    if phone.startswith("254") and len(phone) == 12:
        return "0" + phone[3:]

    return phone


def get_user_kyc_status(user) -> str:
    kyc = getattr(user, "kycprofile", None)
    return getattr(kyc, "status", "not_submitted")


def user_has_full_access(user) -> bool:
    """
    Full access means:
    - account enabled
    - not blocked
    - KYC approved
    - admin/business approval done

    OTP is not mandatory at login for now.
    """
    return (
        user.is_authenticated
        and user.is_active
        and user.status == "approved"
        and get_user_kyc_status(user) == "approved"
    )


def build_user_payload(user) -> dict:
    """
    Single source of truth for frontend-safe user snapshot.
    Must match MeSerializer / frontend session shape.
    """
    return MeSerializer(user).data


def get_sms_error_message(exc: Exception) -> str:
    raw = str(exc or "").strip()

    if "UserInBlacklist" in raw:
        return (
            "We could not send OTP to this phone number because the SMS provider "
            "has blocked or blacklisted it. Please use another number or contact support."
        )

    return "Unable to send OTP right now. Please try again later."


# =========================
# PASSWORD RESET HELPERS
# =========================
def _password_reset_code_key(email: str) -> str:
    return f"{PASSWORD_RESET_CACHE_PREFIX}:code:{email.lower()}"


def _password_reset_meta_key(email: str) -> str:
    return f"{PASSWORD_RESET_CACHE_PREFIX}:meta:{email.lower()}"


def can_request_password_reset(email: str) -> tuple[bool, str]:
    """
    Limit password reset requests per email.
    """
    meta_key = _password_reset_meta_key(email)
    now_ts = int(timezone.now().timestamp())
    meta = cache.get(meta_key) or {"count": 0, "window_start": now_ts}

    window_start = int(meta.get("window_start", now_ts))
    count = int(meta.get("count", 0))

    if now_ts - window_start >= 3600:
        meta = {"count": 0, "window_start": now_ts}
        count = 0
        window_start = now_ts

    if count >= PASSWORD_RESET_MAX_PER_HOUR:
        return False, "Password reset request limit reached. Try again later."

    meta["count"] = count + 1
    cache.set(meta_key, meta, timeout=3600)
    return True, ""


def set_password_reset_code(email: str, code: str) -> None:
    cache.set(
        _password_reset_code_key(email),
        code,
        timeout=PASSWORD_RESET_CODE_EXPIRY_SECONDS,
    )


def get_password_reset_code(email: str):
    return cache.get(_password_reset_code_key(email))


def clear_password_reset_code(email: str) -> None:
    cache.delete(_password_reset_code_key(email))


# =========================
# REGISTER (CREATE USER ONLY)
# =========================
class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data.copy()
        data["phone"] = normalize_local_kenyan_phone(data.get("phone", ""))

        serializer = RegisterSerializer(data=data)
        serializer.is_valid(raise_exception=True)

        user = serializer.save()

        return Response(
            {
                "detail": "Registration successful. You can now log in.",
                "user": build_user_payload(user),
            },
            status=status.HTTP_201_CREATED,
        )


# =========================
# VERIFY OTP (VERIFY PHONE)
# Keep for future sensitive actions / optional phone verification
# =========================
class VerifyOTPView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [OTPThrottle]

    def post(self, request):
        phone_local = normalize_local_kenyan_phone(request.data.get("phone", ""))
        code = str(request.data.get("otp", "")).strip()

        if not phone_local or not code:
            return Response(
                {"detail": "Phone and OTP are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = UserModel.objects.filter(phone=phone_local).first()
        if not user:
            return Response(
                {"detail": "User not found."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if user.is_locked():
            return Response(
                {"detail": "Too many failed attempts. Account temporarily locked."},
                status=status.HTTP_403_FORBIDDEN,
            )

        otp = (
            OTP.objects.filter(phone=phone_local, code=code, is_used=False)
            .order_by("-created_at")
            .first()
        )

        if not otp or otp.is_expired():
            user.failed_login_attempts = (user.failed_login_attempts or 0) + 1

            if user.failed_login_attempts >= MAX_OTP_ATTEMPTS:
                user.save(update_fields=["failed_login_attempts"])
                user.lock_account()
            else:
                user.save(update_fields=["failed_login_attempts"])

            return Response(
                {"detail": "Invalid or expired OTP."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.is_phone_verified = True
        user.reset_failed_attempts()
        user.save(
            update_fields=["is_phone_verified", "failed_login_attempts", "locked_until"]
        )

        otp.mark_used()

        return Response(
            {
                "detail": "Phone verified successfully.",
                "user": build_user_payload(user),
            },
            status=status.HTTP_200_OK,
        )


# =========================
# LOGIN (JWT)
# No OTP required at login
# =========================
class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]

    def post(self, request):
        data = request.data.copy()
        data["phone"] = normalize_local_kenyan_phone(data.get("phone", ""))

        serializer = LoginSerializer(data=data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        user = serializer.validated_data["user"]

        if user.is_locked():
            return Response(
                {"detail": "Account temporarily locked. Try again later."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not user.is_active:
            return Response(
                {"detail": "Account disabled. Contact admin."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if user.status == "blocked":
            return Response(
                {"detail": "Account blocked. Contact admin."},
                status=status.HTTP_403_FORBIDDEN,
            )

        refresh = RefreshToken.for_user(user)

        return Response(
            {
                "detail": "Login successful.",
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": build_user_payload(user),
            },
            status=status.HTTP_200_OK,
        )


# =========================
# FORGOT PASSWORD (SEND EMAIL RESET CODE)
# =========================
class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [OTPThrottle]

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"].strip().lower()

        user = UserModel.objects.filter(email__iexact=email).first()
        if not user:
            return Response(
                {"detail": "User with this email does not exist."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not user.is_active:
            return Response(
                {"detail": "Account disabled. Contact support."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if user.status == "blocked":
            return Response(
                {"detail": "Account blocked. Contact support."},
                status=status.HTTP_403_FORBIDDEN,
            )

        allowed, error_message = can_request_password_reset(email)
        if not allowed:
            return Response(
                {"detail": error_message},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        code = str(random.randint(100000, 999999))
        set_password_reset_code(email, code)

        try:
            send_password_reset_email(
                email=email,
                code=code,
                expiry_minutes=PASSWORD_RESET_CODE_EXPIRY_SECONDS // 60,
            )
        except Exception as e:
            clear_password_reset_code(email)
            return Response(
                {
                    "detail": "Unable to send password reset email right now.",
                    "error": str(e),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {"detail": "Password reset code sent to your email."},
            status=status.HTTP_200_OK,
        )


# =========================
# RESET PASSWORD (EMAIL + CODE)
# =========================
class ResetPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = str(request.data.get("email", "")).strip().lower()
        code = str(request.data.get("code", "")).strip()
        new_password = request.data.get("new_password", "")

        if not email or not code or not new_password:
            return Response(
                {"detail": "Email, code, and new password are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        stored_code = get_password_reset_code(email)
        if not stored_code or stored_code != code:
            return Response(
                {"detail": "Invalid or expired reset code."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            validate_password(new_password)
        except DjangoValidationError as e:
            return Response(
                {"new_password": list(e.messages)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = UserModel.objects.filter(email__iexact=email).first()
        if not user:
            return Response(
                {"detail": "User not found."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if user.status == "blocked":
            return Response(
                {"detail": "Account blocked. Contact support."},
                status=status.HTTP_403_FORBIDDEN,
            )

        user.set_password(new_password)
        user.failed_login_attempts = 0
        user.locked_until = None
        user.save(update_fields=["password", "failed_login_attempts", "locked_until"])

        clear_password_reset_code(email)

        refresh = RefreshToken.for_user(user)

        return Response(
            {
                "detail": "Password reset successful.",
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": build_user_payload(user),
            },
            status=status.HTTP_200_OK,
        )


# =========================
# RESEND OTP
# Used only for phone verification / sensitive actions
# =========================
class ResendOTPView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [OTPThrottle]

    def post(self, request):
        data = request.data.copy()
        data["phone"] = normalize_local_kenyan_phone(data.get("phone", ""))

        serializer = ResendOTPSerializer(data=data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        phone_local = serializer.validated_data["phone"]
        phone_intl = normalize_kenyan_phone(phone_local)
        now = timezone.now()

        user = UserModel.objects.filter(phone=phone_local).first()
        if not user:
            return Response(
                {"detail": "User not found."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if user.is_phone_verified:
            return Response(
                {"detail": "Phone is already verified."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        last = OTP.objects.filter(phone=phone_local).order_by("-created_at").first()
        if last and (now - last.created_at).total_seconds() < OTP_COOLDOWN_SECONDS:
            wait = OTP_COOLDOWN_SECONDS - int((now - last.created_at).total_seconds())
            return Response(
                {"detail": f"Please wait {wait} seconds before requesting a new OTP."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        hour_ago = now - timedelta(hours=1)
        otp_count = OTP.objects.filter(
            phone=phone_local,
            created_at__gte=hour_ago,
        ).count()

        if otp_count >= OTP_MAX_PER_HOUR:
            return Response(
                {"detail": "OTP request limit reached. Try again later."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        code = OTP.generate()
        OTP.objects.create(phone=phone_local, code=code)

        message = f"Your verification code is {code}. Valid for 5 minutes."

        print(f"🔐 RESEND OTP for {phone_local}: {code}")

        try:
            send_sms(phone_intl, message)
        except Exception as e:
            OTP.objects.filter(phone=phone_local, code=code, is_used=False).delete()
            return Response(
                {
                    "detail": get_sms_error_message(e),
                    "error": str(e),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {"detail": "OTP sent successfully."},
            status=status.HTTP_200_OK,
        )


# =========================
# USER INFO
# =========================
class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        data = MeSerializer(request.user).data
        return Response(data, status=status.HTTP_200_OK)

    def patch(self, request):
        user = request.user
        serializer = UpdateMeSerializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        data = MeSerializer(user).data
        return Response(data, status=status.HTTP_200_OK)


# =========================
# KYC SUBMISSION
# =========================
class KYCSubmitView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user

        if user.status == "blocked":
            return Response(
                {"detail": "Account blocked. Contact admin."},
                status=status.HTTP_403_FORBIDDEN,
            )

        kyc_obj, _ = KYCProfile.objects.get_or_create(user=user)

        serializer = KYCSubmitSerializer(
            kyc_obj,
            data=request.data,
            partial=False,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        if kyc_obj.status != "submitted":
            kyc_obj.status = "submitted"
            kyc_obj.save(update_fields=["status"])

        user.refresh_from_db()

        return Response(
            {
                "detail": "KYC submitted successfully.",
                "kyc_status": kyc_obj.status,
                "has_full_access": user_has_full_access(user),
                "user": build_user_payload(user),
            },
            status=status.HTTP_200_OK,
        )


# =========================
# ACCOUNT DELETION PUBLIC PAGE
# =========================
def account_deletion_page(request):
    return render(request, "accounts/account_deletion.html")


# =========================
# ACCOUNT DELETION (REQUEST)
# =========================
class DeleteAccountView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user

        if not user.is_active:
            return Response(
                {"detail": "Account already inactive or deletion in progress."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.is_active = False
        user.save(update_fields=["is_active"])

        return Response(
            {
                "detail": "Your account deletion request has been received. Your account has been deactivated."
            },
            status=status.HTTP_200_OK,
        )
    def child_safety_page(request):
        return render(request, "accounts/child_safety.html")

# from datetime import timedelta

# from django.contrib.auth import get_user_model
# from django.utils import timezone

# from rest_framework import status
# from rest_framework.permissions import AllowAny, IsAuthenticated
# from rest_framework.response import Response
# from rest_framework.views import APIView
# from rest_framework_simplejwt.tokens import RefreshToken

# from .models import OTP, KYCProfile
# from .serializers import (
#     RegisterSerializer,
#     LoginSerializer,
#     ForgotPasswordSerializer,
#     ResetPasswordSerializer,
#     ResendOTPSerializer,
#     MeSerializer,
#     UpdateMeSerializer,
#     KYCSubmitSerializer,
# )
# from .throttles import LoginThrottle, OTPThrottle
# from .utils.phone import normalize_kenyan_phone
# from .utils.sms import send_sms

# UserModel = get_user_model()

# # =========================
# # CONSTANTS
# # =========================
# OTP_COOLDOWN_SECONDS = 60
# OTP_MAX_PER_HOUR = 5
# MAX_OTP_ATTEMPTS = 5
# MAX_LOGIN_ATTEMPTS = 5
# STALE_UNVERIFIED_ACCOUNT_MINUTES = 10


# # =========================
# # PHONE HELPERS
# # =========================
# def normalize_local_kenyan_phone(phone: str) -> str:
#     """
#     Normalize to local DB format only:
#     07XXXXXXXX or 01XXXXXXXX

#     Examples:
#     +254701234567 -> 0701234567
#     254701234567  -> 0701234567
#     0701234567    -> 0701234567
#     """
#     phone = str(phone or "").strip().replace(" ", "")

#     if phone.startswith("+254") and len(phone) == 13:
#         return "0" + phone[4:]
#     if phone.startswith("254") and len(phone) == 12:
#         return "0" + phone[3:]

#     return phone


# def is_stale_unverified_user(user) -> bool:
#     return (
#         not user.is_phone_verified
#         and user.status == "pending"
#         and user.date_joined
#         <= timezone.now() - timedelta(minutes=STALE_UNVERIFIED_ACCOUNT_MINUTES)
#     )


# def get_user_kyc_status(user) -> str:
#     kyc = getattr(user, "kycprofile", None)
#     return getattr(kyc, "status", "not_submitted")


# def user_has_full_access(user) -> bool:
#     """
#     Full access means:
#     - phone verified
#     - account enabled
#     - not blocked
#     - KYC approved
#     - admin/business approval done
#     """
#     return (
#         user.is_authenticated
#         and user.is_active
#         and user.is_phone_verified
#         and user.status == "approved"
#         and get_user_kyc_status(user) == "approved"
#     )


# def build_user_payload(user) -> dict:
#     """
#     Single source of truth for frontend-safe user snapshot.
#     Must match MeSerializer / frontend session shape.
#     """
#     return MeSerializer(user).data


# def get_sms_error_message(exc: Exception) -> str:
#     raw = str(exc or "").strip()

#     if "UserInBlacklist" in raw:
#         return (
#             "We could not send OTP to this phone number because the SMS provider "
#             "has blocked or blacklisted it. Please use another number or contact support."
#         )

#     return "Unable to send OTP right now. Please try again later."


# # =========================
# # REGISTER (CREATE USER + SEND OTP)
# # =========================
# class RegisterView(APIView):
#     permission_classes = [AllowAny]

#     def post(self, request):
#         data = request.data.copy()
#         phone_local = normalize_local_kenyan_phone(data.get("phone", ""))
#         data["phone"] = phone_local

#         existing_user = UserModel.objects.filter(phone=phone_local).first()

#         if existing_user:
#             if is_stale_unverified_user(existing_user):
#                 OTP.objects.filter(phone=phone_local).delete()
#                 existing_user.delete()
#             else:
#                 return Response(
#                     {
#                         "detail": (
#                             "An account with this phone already exists. "
#                             "Verify OTP or request a new one."
#                         )
#                     },
#                     status=status.HTTP_400_BAD_REQUEST,
#                 )

#         serializer = RegisterSerializer(data=data)
#         serializer.is_valid(raise_exception=True)

#         user = serializer.save()

#         otp_code = OTP.generate()
#         OTP.objects.create(phone=user.phone, code=otp_code)

#         phone_intl = normalize_kenyan_phone(user.phone)
#         message = f"Your verification code is {otp_code}. Valid for 5 minutes."

#         print(f"🔐 REGISTER OTP for {user.phone}: {otp_code}")

#         try:
#             send_sms(phone_intl, message)
#         except Exception as e:
#             OTP.objects.filter(phone=user.phone).delete()
#             user.delete()

#             return Response(
#                 {
#                     "detail": get_sms_error_message(e),
#                     "error": str(e),
#                 },
#                 status=status.HTTP_400_BAD_REQUEST,
#             )

#         return Response(
#             {"detail": "Registration successful. OTP sent to phone."},
#             status=status.HTTP_201_CREATED,
#         )


# # =========================
# # VERIFY OTP (VERIFY PHONE)
# # =========================
# class VerifyOTPView(APIView):
#     permission_classes = [AllowAny]
#     throttle_classes = [OTPThrottle]

#     def post(self, request):
#         phone_local = normalize_local_kenyan_phone(request.data.get("phone", ""))
#         code = str(request.data.get("otp", "")).strip()

#         if not phone_local or not code:
#             return Response(
#                 {"detail": "Phone and OTP are required."},
#                 status=status.HTTP_400_BAD_REQUEST,
#             )

#         try:
#             user = UserModel.objects.get(phone=phone_local)
#         except UserModel.DoesNotExist:
#             return Response(
#                 {"detail": "User not found."},
#                 status=status.HTTP_400_BAD_REQUEST,
#             )

#         if is_stale_unverified_user(user):
#             OTP.objects.filter(phone=phone_local).delete()
#             user.delete()
#             return Response(
#                 {"detail": "Registration expired. Please register again."},
#                 status=status.HTTP_400_BAD_REQUEST,
#             )

#         if user.is_locked():
#             return Response(
#                 {"detail": "Too many failed attempts. Account temporarily locked."},
#                 status=status.HTTP_403_FORBIDDEN,
#             )

#         otp = (
#             OTP.objects.filter(phone=phone_local, code=code, is_used=False)
#             .order_by("-created_at")
#             .first()
#         )

#         if not otp or otp.is_expired():
#             user.failed_login_attempts += 1

#             if user.failed_login_attempts >= MAX_OTP_ATTEMPTS:
#                 user.lock_account()
#             else:
#                 user.save(update_fields=["failed_login_attempts"])

#             return Response(
#                 {"detail": "Invalid or expired OTP."},
#                 status=status.HTTP_400_BAD_REQUEST,
#             )

#         user.is_phone_verified = True
#         user.reset_failed_attempts()
#         user.save(
#             update_fields=["is_phone_verified", "failed_login_attempts", "locked_until"]
#         )

#         otp.mark_used()

#         return Response(
#             {
#                 "detail": "Account verified successfully.",
#                 "user": build_user_payload(user),
#             },
#             status=status.HTTP_200_OK,
#         )


# # =========================
# # LOGIN (JWT)
# # =========================
# class LoginView(APIView):
#     permission_classes = [AllowAny]
#     throttle_classes = [LoginThrottle]

#     def post(self, request):
#         data = request.data.copy()
#         data["phone"] = normalize_local_kenyan_phone(data.get("phone", ""))

#         serializer = LoginSerializer(data=data, context={"request": request})
#         serializer.is_valid(raise_exception=True)

#         user = serializer.validated_data["user"]

#         if user.is_locked():
#             return Response(
#                 {"detail": "Account temporarily locked. Try again later."},
#                 status=status.HTTP_403_FORBIDDEN,
#             )

#         if not user.is_active:
#             return Response(
#                 {"detail": "Account disabled. Contact admin."},
#                 status=status.HTTP_403_FORBIDDEN,
#             )

#         if not user.is_phone_verified:
#             return Response(
#                 {"detail": "Account not verified. Verify OTP first."},
#                 status=status.HTTP_403_FORBIDDEN,
#             )

#         if user.status == "blocked":
#             return Response(
#                 {"detail": "Account blocked. Contact admin."},
#                 status=status.HTTP_403_FORBIDDEN,
#             )

#         user.reset_failed_attempts()

#         refresh = RefreshToken.for_user(user)

#         return Response(
#             {
#                 "access": str(refresh.access_token),
#                 "refresh": str(refresh),
#                 "user": build_user_payload(user),
#             },
#             status=status.HTTP_200_OK,
#         )


# # =========================
# # FORGOT PASSWORD (SEND OTP)
# # =========================
# class ForgotPasswordView(APIView):
#     permission_classes = [AllowAny]
#     throttle_classes = [OTPThrottle]

#     def post(self, request):
#         data = request.data.copy()
#         data["phone"] = normalize_local_kenyan_phone(data.get("phone", ""))

#         serializer = ForgotPasswordSerializer(data=data)
#         serializer.is_valid(raise_exception=True)

#         phone_local = serializer.validated_data["phone"]
#         phone_intl = normalize_kenyan_phone(phone_local)
#         now = timezone.now()

#         try:
#             user = UserModel.objects.get(phone=phone_local)
#         except UserModel.DoesNotExist:
#             return Response(
#                 {"detail": "User not found."},
#                 status=status.HTTP_400_BAD_REQUEST,
#             )

#         if is_stale_unverified_user(user):
#             OTP.objects.filter(phone=phone_local).delete()
#             user.delete()
#             return Response(
#                 {"detail": "Registration expired. Please register again."},
#                 status=status.HTTP_400_BAD_REQUEST,
#             )

#         last_otp = OTP.objects.filter(phone=phone_local).order_by("-created_at").first()
#         if last_otp and (now - last_otp.created_at).total_seconds() < OTP_COOLDOWN_SECONDS:
#             return Response(
#                 {"detail": "Please wait before requesting another OTP."},
#                 status=status.HTTP_429_TOO_MANY_REQUESTS,
#             )

#         hour_ago = now - timedelta(hours=1)
#         otp_count = OTP.objects.filter(phone=phone_local, created_at__gte=hour_ago).count()
#         if otp_count >= OTP_MAX_PER_HOUR:
#             return Response(
#                 {"detail": "OTP request limit reached. Try again later."},
#                 status=status.HTTP_429_TOO_MANY_REQUESTS,
#             )

#         otp_code = OTP.generate()
#         OTP.objects.create(phone=phone_local, code=otp_code)

#         message = f"Your password reset OTP is {otp_code}. Valid for 5 minutes."

#         print(f"🔐 RESET OTP for {phone_local}: {otp_code}")

#         try:
#             send_sms(phone_intl, message)
#         except Exception as e:
#             OTP.objects.filter(phone=phone_local, code=otp_code, is_used=False).delete()
#             return Response(
#                 {
#                     "detail": get_sms_error_message(e),
#                     "error": str(e),
#                 },
#                 status=status.HTTP_400_BAD_REQUEST,
#             )

#         return Response(
#             {"detail": "OTP sent successfully."},
#             status=status.HTTP_200_OK,
#         )


# # =========================
# # RESET PASSWORD (AUTO LOGIN)
# # =========================
# class ResetPasswordView(APIView):
#     permission_classes = [AllowAny]

#     def post(self, request):
#         data = request.data.copy()
#         data["phone"] = normalize_local_kenyan_phone(data.get("phone", ""))

#         serializer = ResetPasswordSerializer(data=data)
#         serializer.is_valid(raise_exception=True)

#         phone_local = serializer.validated_data["phone"]
#         new_password = serializer.validated_data["new_password"]
#         otp = serializer.validated_data["otp_obj"]

#         try:
#             user = UserModel.objects.get(phone=phone_local)
#         except UserModel.DoesNotExist:
#             return Response(
#                 {"detail": "User not found."},
#                 status=status.HTTP_400_BAD_REQUEST,
#             )

#         if is_stale_unverified_user(user):
#             OTP.objects.filter(phone=phone_local).delete()
#             user.delete()
#             return Response(
#                 {"detail": "Registration expired. Please register again."},
#                 status=status.HTTP_400_BAD_REQUEST,
#             )

#         user.set_password(new_password)
#         user.is_phone_verified = True
#         user.failed_login_attempts = 0
#         user.locked_until = None
#         user.save(
#             update_fields=[
#                 "password",
#                 "is_phone_verified",
#                 "failed_login_attempts",
#                 "locked_until",
#             ]
#         )

#         otp.mark_used()

#         refresh = RefreshToken.for_user(user)

#         return Response(
#             {
#                 "detail": "Password reset successful.",
#                 "access": str(refresh.access_token),
#                 "refresh": str(refresh),
#                 "user": build_user_payload(user),
#             },
#             status=status.HTTP_200_OK,
#         )


# # =========================
# # RESEND OTP
# # =========================
# class ResendOTPView(APIView):
#     permission_classes = [AllowAny]
#     throttle_classes = [OTPThrottle]

#     def post(self, request):
#         data = request.data.copy()
#         data["phone"] = normalize_local_kenyan_phone(data.get("phone", ""))

#         serializer = ResendOTPSerializer(data=data, context={"request": request})
#         serializer.is_valid(raise_exception=True)

#         phone_local = serializer.validated_data["phone"]
#         phone_intl = normalize_kenyan_phone(phone_local)
#         now = timezone.now()

#         try:
#             user = UserModel.objects.get(phone=phone_local)
#         except UserModel.DoesNotExist:
#             return Response(
#                 {"detail": "User not found."},
#                 status=status.HTTP_400_BAD_REQUEST,
#             )

#         if user.is_phone_verified:
#             return Response(
#                 {"detail": "Account is already verified. Please log in."},
#                 status=status.HTTP_400_BAD_REQUEST,
#             )

#         if is_stale_unverified_user(user):
#             OTP.objects.filter(phone=phone_local).delete()
#             user.delete()
#             return Response(
#                 {"detail": "Registration expired. Please register again."},
#                 status=status.HTTP_400_BAD_REQUEST,
#             )

#         last = OTP.objects.filter(phone=phone_local).order_by("-created_at").first()
#         if last and (now - last.created_at).total_seconds() < OTP_COOLDOWN_SECONDS:
#             wait = OTP_COOLDOWN_SECONDS - int((now - last.created_at).total_seconds())
#             return Response(
#                 {"detail": f"Please wait {wait} seconds before requesting a new OTP."},
#                 status=status.HTTP_429_TOO_MANY_REQUESTS,
#             )

#         hour_ago = now - timedelta(hours=1)
#         if OTP.objects.filter(phone=phone_local, created_at__gte=hour_ago).count() >= OTP_MAX_PER_HOUR:
#             return Response(
#                 {"detail": "OTP request limit reached. Try again later."},
#                 status=status.HTTP_429_TOO_MANY_REQUESTS,
#             )

#         code = OTP.generate()
#         OTP.objects.create(phone=phone_local, code=code)

#         message = f"Your verification code is {code}. Valid for 5 minutes."

#         print(f"🔐 RESEND OTP for {phone_local}: {code}")

#         try:
#             send_sms(phone_intl, message)
#         except Exception as e:
#             OTP.objects.filter(phone=phone_local, code=code, is_used=False).delete()
#             return Response(
#                 {
#                     "detail": get_sms_error_message(e),
#                     "error": str(e),
#                 },
#                 status=status.HTTP_400_BAD_REQUEST,
#             )

#         return Response(
#             {"detail": "OTP resent successfully."},
#             status=status.HTTP_200_OK,
#         )


# # =========================
# # USER INFO
# # =========================
# class MeView(APIView):
#     permission_classes = [IsAuthenticated]

#     def get(self, request):
#         data = MeSerializer(request.user).data
#         return Response(data, status=status.HTTP_200_OK)

#     def patch(self, request):
#         user = request.user
#         serializer = UpdateMeSerializer(user, data=request.data, partial=True)
#         serializer.is_valid(raise_exception=True)
#         serializer.save()

#         data = MeSerializer(user).data
#         return Response(data, status=status.HTTP_200_OK)


# # =========================
# # KYC SUBMISSION
# # =========================
# class KYCSubmitView(APIView):
#     permission_classes = [IsAuthenticated]

#     def post(self, request):
#         user = request.user

#         if user.status == "blocked":
#             return Response(
#                 {"detail": "Account blocked. Contact admin."},
#                 status=status.HTTP_403_FORBIDDEN,
#             )

#         kyc_obj, _ = KYCProfile.objects.get_or_create(user=user)

#         serializer = KYCSubmitSerializer(kyc_obj, data=request.data)
#         serializer.is_valid(raise_exception=True)
#         serializer.save()

#         if kyc_obj.status != "submitted":
#             kyc_obj.status = "submitted"
#             kyc_obj.save(update_fields=["status"])

#         user.refresh_from_db()

#         return Response(
#             {
#                 "detail": "KYC submitted successfully.",
#                 "kyc_status": kyc_obj.status,
#                 "has_full_access": user_has_full_access(user),
#                 "user": build_user_payload(user),
#             },
#             status=status.HTTP_200_OK,
#         )