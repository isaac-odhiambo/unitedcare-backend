from datetime import timedelta
from django.conf import settings
from django.contrib.auth import get_user_model, authenticate
from django.utils import timezone

from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

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
from .models import OTP, KYCProfile
from .throttles import LoginThrottle, OTPThrottle
from .utils.phone import normalize_kenyan_phone
from .utils.sms import send_sms

UserModel = get_user_model()

# Constants
OTP_COOLDOWN_SECONDS = 60
OTP_MAX_PER_HOUR = 5
MAX_OTP_ATTEMPTS = 5
MAX_LOGIN_ATTEMPTS = 5


# =========================
# 📝 REGISTER (OTP SENT)
# =========================
class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = serializer.save()

        # 🔐 Generate OTP
        otp_code = OTP.generate()
        OTP.objects.create(phone=user.phone, code=otp_code)

        # 📩 Send OTP
        phone_intl = normalize_kenyan_phone(user.phone)
        message = f"Your verification code is {otp_code}. Valid for 5 minutes."
        send_sms(phone_intl, message)

        return Response(
            {"message": "Registration successful. OTP sent to phone."},
            status=status.HTTP_201_CREATED,
        )


# =========================
# 🔐 VERIFY OTP (ACTIVATE)
# =========================
class VerifyOTPView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [OTPThrottle]

    def post(self, request):
        phone = request.data.get("phone")
        code = request.data.get("otp")

        if not phone or not code:
            return Response(
                {"detail": "Phone and OTP are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        phone = normalize_kenyan_phone(phone)

        try:
            user = UserModel.objects.get(phone=phone)
        except UserModel.DoesNotExist:
            return Response(
                {"detail": "User not found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Locked accounts
        if user.is_locked():
            return Response(
                {"detail": "Too many failed attempts. Account temporarily locked."},
                status=status.HTTP_403_FORBIDDEN,
            )

        otp = OTP.objects.filter(phone=phone, code=code, is_used=False).order_by("-created_at").first()

        if not otp or otp.is_expired():
            # Increase failed attempts
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= MAX_OTP_ATTEMPTS:
                user.lock_account()
            else:
                user.save(update_fields=["failed_login_attempts"])
            return Response(
                {"detail": "Invalid or expired OTP"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ✅ Success
        user.is_active = True
        user.reset_failed_attempts()
        user.save(update_fields=["is_active"])

        otp.is_used = True
        otp.save(update_fields=["is_used"])

        return Response({"message": "Account verified successfully"}, status=status.HTTP_200_OK)


# =========================
# 🔑 LOGIN (JWT) + SECURE
# =========================
class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = serializer.validated_data["user"]

        # Locked?
        if user.is_locked():
            return Response(
                {"detail": "Account temporarily locked. Try again later."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Must be OTP-activated
        if not user.is_active:
            return Response(
                {"detail": "Account not activated. Verify OTP first."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Blocked
        if user.status == "blocked":
            return Response(
                {"detail": "Account blocked. Contact admin."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # ✅ Successful login
        user.reset_failed_attempts()

        refresh = RefreshToken.for_user(user)

        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "role": user.role,
                "status": user.status,
                "is_admin": user.is_admin,
            },
            status=status.HTTP_200_OK,
        )


# =========================
# 📩 FORGOT PASSWORD (OTP)
# =========================
class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [OTPThrottle]

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        phone = normalize_kenyan_phone(serializer.validated_data["phone"])
        now = timezone.now()

        # ⏱️ Cooldown: 1 OTP per minute
        last_otp = OTP.objects.filter(phone=phone).order_by("-created_at").first()
        if last_otp and (now - last_otp.created_at).total_seconds() < OTP_COOLDOWN_SECONDS:
            return Response(
                {"detail": "Please wait before requesting another OTP"},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # 🔢 Limit: 5 OTPs per hour
        hour_ago = now - timedelta(hours=1)
        otp_count = OTP.objects.filter(phone=phone, created_at__gte=hour_ago).count()
        if otp_count >= OTP_MAX_PER_HOUR:
            return Response(
                {"detail": "OTP request limit reached. Try again later."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # Generate OTP
        otp_code = OTP.generate()
        OTP.objects.create(phone=phone, code=otp_code)

        message = f"Your password reset OTP is {otp_code}. Valid for 5 minutes."
        send_sms(phone, message)

        return Response({"message": "OTP sent successfully"}, status=status.HTTP_200_OK)


# =========================
# 🔄 RESET PASSWORD (AUTO LOGIN)
# =========================
class ResetPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        phone = normalize_kenyan_phone(serializer.validated_data["phone"])
        new_password = serializer.validated_data["new_password"]
        otp = serializer.validated_data["otp_obj"]

        try:
            user = UserModel.objects.get(phone=phone)
        except UserModel.DoesNotExist:
            return Response({"detail": "User not found."}, status=status.HTTP_400_BAD_REQUEST)

        # Reset password
        user.set_password(new_password)

        # Ensure account is active
        user.is_active = True
        user.failed_login_attempts = 0
        user.locked_until = None
        user.save(update_fields=["password", "is_active", "failed_login_attempts", "locked_until"])

        # Mark OTP used
        otp.is_used = True
        otp.save(update_fields=["is_used"])

        refresh = RefreshToken.for_user(user)

        return Response(
            {
                "message": "Password reset successful",
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "role": user.role,
                "status": user.status,
            },
            status=status.HTTP_200_OK,
        )


# =========================
# 🔁 RESEND OTP
# =========================
class ResendOTPView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [OTPThrottle]

    def post(self, request):
        serializer = ResendOTPSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        phone = normalize_kenyan_phone(serializer.validated_data["phone"])
        now = timezone.now()

        # Cooldown 60s
        last = OTP.objects.filter(phone=phone).order_by("-created_at").first()
        if last and (now - last.created_at).total_seconds() < 60:
            wait = 60 - int((now - last.created_at).total_seconds())
            return Response(
                {"detail": f"Please wait {wait} seconds before requesting a new OTP."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # Max 5 per hour
        hour_ago = now - timedelta(hours=1)
        if OTP.objects.filter(phone=phone, created_at__gte=hour_ago).count() >= 5:
            return Response(
                {"detail": "OTP request limit reached. Try again later."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        code = OTP.generate()
        OTP.objects.create(phone=phone, code=code)
        message = f"Your verification code is {code}. Valid for 5 minutes."
        send_sms(phone, message)

        return Response({"detail": "OTP resent successfully."}, status=status.HTTP_200_OK)


# =========================
# 👤 USER INFO
# =========================
class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        data = MeSerializer(user).data
        data["is_admin"] = user.is_admin
        # ✅ add this
        kyc = getattr(user, "kycprofile", None)
        data["kyc_status"] = getattr(kyc, "status", "not_submitted")
        return Response(data, status=status.HTTP_200_OK)

    def patch(self, request):
        user = request.user
        serializer = UpdateMeSerializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        data = MeSerializer(user).data
        data["is_admin"] = user.is_admin
        return Response(data, status=status.HTTP_200_OK)


# =========================
# 🪪 KYC SUBMISSION
# =========================
class KYCSubmitView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        kyc_obj, _ = KYCProfile.objects.get_or_create(user=user)

        serializer = KYCSubmitSerializer(kyc_obj, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        # Auto-update status
        if kyc_obj.status != "submitted":
            kyc_obj.status = "submitted"
            kyc_obj.save(update_fields=["status"])

        return Response(
            {"message": "KYC submitted successfully", "kyc_status": kyc_obj.status},
            status=status.HTTP_200_OK,
        )