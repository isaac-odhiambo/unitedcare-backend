from datetime import timedelta

from django.contrib.auth import get_user_model
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
    ResetPasswordSerializer,
    ResendOTPSerializer,
    MeSerializer,
    UpdateMeSerializer,
    KYCSubmitSerializer,
)
from .throttles import LoginThrottle, OTPThrottle
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


def is_stale_unverified_user(user) -> bool:
    return (
        not user.is_active
        and user.status == "pending"
        and user.date_joined <= timezone.now() - timedelta(minutes=STALE_UNVERIFIED_ACCOUNT_MINUTES)
    )


def get_user_kyc_status(user) -> str:
    kyc = getattr(user, "kycprofile", None)
    return getattr(kyc, "status", "not_submitted")


def user_has_full_access(user) -> bool:
    """
    Full access means:
    - account can log in
    - not blocked
    - KYC approved
    - admin/business approval done
    """
    return (
        user.is_authenticated
        and user.is_active
        and user.status == "approved"
        and get_user_kyc_status(user) == "approved"
    )


# =========================
# REGISTER (CREATE USER + SEND OTP)
# =========================
class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data.copy()
        phone_local = normalize_local_kenyan_phone(data.get("phone", ""))
        data["phone"] = phone_local

        existing_user = UserModel.objects.filter(phone=phone_local).first()

        if existing_user:
            if is_stale_unverified_user(existing_user):
                OTP.objects.filter(phone=phone_local).delete()
                existing_user.delete()
            else:
                return Response(
                    {
                        "detail": (
                            "An account with this phone already exists. "
                            "Verify OTP or request a new one."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        serializer = RegisterSerializer(data=data)
        serializer.is_valid(raise_exception=True)

        user = serializer.save()

        otp_code = OTP.generate()
        OTP.objects.create(phone=user.phone, code=otp_code)

        phone_intl = normalize_kenyan_phone(user.phone)
        message = f"Your verification code is {otp_code}. Valid for 5 minutes."

        print(f"🔐 REGISTER OTP for {user.phone}: {otp_code}")
        send_sms(phone_intl, message)

        return Response(
            {"detail": "Registration successful. OTP sent to phone."},
            status=status.HTTP_201_CREATED,
        )


# =========================
# VERIFY OTP (ACTIVATE ACCOUNT)
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

        try:
            user = UserModel.objects.get(phone=phone_local)
        except UserModel.DoesNotExist:
            return Response(
                {"detail": "User not found."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if is_stale_unverified_user(user):
            OTP.objects.filter(phone=phone_local).delete()
            user.delete()
            return Response(
                {"detail": "Registration expired. Please register again."},
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
            user.failed_login_attempts += 1

            if user.failed_login_attempts >= MAX_OTP_ATTEMPTS:
                user.lock_account()
            else:
                user.save(update_fields=["failed_login_attempts"])

            return Response(
                {"detail": "Invalid or expired OTP."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.is_active = True
        user.reset_failed_attempts()
        user.save(update_fields=["is_active", "failed_login_attempts", "locked_until"])

        otp.mark_used()

        return Response(
            {
                "detail": "Account verified successfully.",
                "status": user.status,
                "kyc_status": get_user_kyc_status(user),
                "has_full_access": user_has_full_access(user),
            },
            status=status.HTTP_200_OK,
        )


# =========================
# LOGIN (JWT)
# =========================
class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]

    def post(self, request):
        data = request.data.copy()
        data["phone"] = normalize_local_kenyan_phone(data.get("phone", ""))

        serializer = LoginSerializer(data=data)
        serializer.is_valid(raise_exception=True)

        user = serializer.validated_data["user"]

        if user.is_locked():
            return Response(
                {"detail": "Account temporarily locked. Try again later."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not user.is_active:
            return Response(
                {"detail": "Account not activated. Verify OTP first."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if user.status == "blocked":
            return Response(
                {"detail": "Account blocked. Contact admin."},
                status=status.HTTP_403_FORBIDDEN,
            )

        user.reset_failed_attempts()

        refresh = RefreshToken.for_user(user)
        kyc_status = get_user_kyc_status(user)

        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "role": user.role,
                "status": user.status,
                "is_admin": user.is_admin,
                "kyc_status": kyc_status,
                "has_full_access": user_has_full_access(user),
            },
            status=status.HTTP_200_OK,
        )


# =========================
# FORGOT PASSWORD (SEND OTP)
# =========================
class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [OTPThrottle]

    def post(self, request):
        data = request.data.copy()
        data["phone"] = normalize_local_kenyan_phone(data.get("phone", ""))

        serializer = ForgotPasswordSerializer(data=data)
        serializer.is_valid(raise_exception=True)

        phone_local = serializer.validated_data["phone"]
        phone_intl = normalize_kenyan_phone(phone_local)
        now = timezone.now()

        try:
            user = UserModel.objects.get(phone=phone_local)
        except UserModel.DoesNotExist:
            return Response(
                {"detail": "User not found."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if is_stale_unverified_user(user):
            OTP.objects.filter(phone=phone_local).delete()
            user.delete()
            return Response(
                {"detail": "Registration expired. Please register again."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        last_otp = OTP.objects.filter(phone=phone_local).order_by("-created_at").first()
        if last_otp and (now - last_otp.created_at).total_seconds() < OTP_COOLDOWN_SECONDS:
            return Response(
                {"detail": "Please wait before requesting another OTP."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        hour_ago = now - timedelta(hours=1)
        otp_count = OTP.objects.filter(phone=phone_local, created_at__gte=hour_ago).count()
        if otp_count >= OTP_MAX_PER_HOUR:
            return Response(
                {"detail": "OTP request limit reached. Try again later."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        otp_code = OTP.generate()
        OTP.objects.create(phone=phone_local, code=otp_code)

        message = f"Your password reset OTP is {otp_code}. Valid for 5 minutes."

        print(f"🔐 RESET OTP for {phone_local}: {otp_code}")
        send_sms(phone_intl, message)

        return Response(
            {"detail": "OTP sent successfully."},
            status=status.HTTP_200_OK,
        )


# =========================
# RESET PASSWORD (AUTO LOGIN)
# =========================
class ResetPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data.copy()
        data["phone"] = normalize_local_kenyan_phone(data.get("phone", ""))

        serializer = ResetPasswordSerializer(data=data)
        serializer.is_valid(raise_exception=True)

        phone_local = serializer.validated_data["phone"]
        new_password = serializer.validated_data["new_password"]
        otp = serializer.validated_data["otp_obj"]

        try:
            user = UserModel.objects.get(phone=phone_local)
        except UserModel.DoesNotExist:
            return Response(
                {"detail": "User not found."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if is_stale_unverified_user(user):
            OTP.objects.filter(phone=phone_local).delete()
            user.delete()
            return Response(
                {"detail": "Registration expired. Please register again."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.set_password(new_password)
        user.is_active = True
        user.failed_login_attempts = 0
        user.locked_until = None
        user.save(
            update_fields=[
                "password",
                "is_active",
                "failed_login_attempts",
                "locked_until",
            ]
        )

        otp.mark_used()

        refresh = RefreshToken.for_user(user)

        return Response(
            {
                "detail": "Password reset successful.",
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "role": user.role,
                "status": user.status,
                "is_admin": user.is_admin,
                "kyc_status": get_user_kyc_status(user),
                "has_full_access": user_has_full_access(user),
            },
            status=status.HTTP_200_OK,
        )


# =========================
# RESEND OTP
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

        try:
            user = UserModel.objects.get(phone=phone_local)
        except UserModel.DoesNotExist:
            return Response(
                {"detail": "User not found."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if user.is_active:
            return Response(
                {"detail": "Account is already verified. Please log in."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if is_stale_unverified_user(user):
            OTP.objects.filter(phone=phone_local).delete()
            user.delete()
            return Response(
                {"detail": "Registration expired. Please register again."},
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
        if OTP.objects.filter(phone=phone_local, created_at__gte=hour_ago).count() >= OTP_MAX_PER_HOUR:
            return Response(
                {"detail": "OTP request limit reached. Try again later."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        code = OTP.generate()
        OTP.objects.create(phone=phone_local, code=code)

        message = f"Your verification code is {code}. Valid for 5 minutes."

        print(f"🔐 RESEND OTP for {phone_local}: {code}")
        send_sms(phone_intl, message)

        return Response(
            {"detail": "OTP resent successfully."},
            status=status.HTTP_200_OK,
        )


# =========================
# USER INFO
# =========================
class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        data = MeSerializer(user).data
        data["is_admin"] = user.is_admin
        data["kyc_status"] = get_user_kyc_status(user)
        data["has_full_access"] = user_has_full_access(user)

        return Response(data, status=status.HTTP_200_OK)

    def patch(self, request):
        user = request.user
        serializer = UpdateMeSerializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        data = MeSerializer(user).data
        data["is_admin"] = user.is_admin
        data["kyc_status"] = get_user_kyc_status(user)
        data["has_full_access"] = user_has_full_access(user)

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

        serializer = KYCSubmitSerializer(kyc_obj, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        if kyc_obj.status != "submitted":
            kyc_obj.status = "submitted"
            kyc_obj.save(update_fields=["status"])

        return Response(
            {
                "detail": "KYC submitted successfully.",
                "kyc_status": kyc_obj.status,
                "has_full_access": user_has_full_access(user),
            },
            status=status.HTTP_200_OK,
        )