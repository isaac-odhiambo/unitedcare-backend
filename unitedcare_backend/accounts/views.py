from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.tokens import RefreshToken
from django.utils import timezone
from datetime import timedelta

from django.contrib.auth import get_user_model

from .serializers import (
    RegisterSerializer,
    LoginSerializer,
    ForgotPasswordSerializer,
    ResetPasswordSerializer,
)
from .models import OTP
from .throttles import LoginThrottle, OTPThrottle
from .utils.sms import send_sms
from .utils.phone import normalize_kenyan_phone

User = get_user_model()

OTP_COOLDOWN_SECONDS = 60
OTP_MAX_PER_HOUR = 5


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
            {
                "message": "Registration successful. OTP sent to phone.",
            },
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

        try:
            otp = OTP.objects.filter(
                phone=phone,
                code=code,
                is_used=False
            ).latest("created_at")
        except OTP.DoesNotExist:
            return Response(
                {"detail": "Invalid or expired OTP"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if otp.is_expired():
            return Response(
                {"detail": "OTP has expired"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = User.objects.get(phone=phone)
        user.is_active = True
        user.save(update_fields=["is_active"])

        otp.is_used = True
        otp.save(update_fields=["is_used"])

        return Response(
            {"message": "Account verified successfully"},
            status=status.HTTP_200_OK,
        )


# =========================
# 🔑 LOGIN (JWT)
# =========================
class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = serializer.validated_data["user"]
        refresh = RefreshToken.for_user(user)

        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "role": user.role,
                "status": user.status,
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

        phone = serializer.validated_data["phone"]
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
        otp_count = OTP.objects.filter(
            phone=phone,
            created_at__gte=hour_ago
        ).count()

        if otp_count >= OTP_MAX_PER_HOUR:
            return Response(
                {"detail": "OTP request limit reached. Try again later."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # 🔐 Generate OTP
        otp_code = OTP.generate()
        OTP.objects.create(phone=phone, code=otp_code)

        phone_intl = normalize_kenyan_phone(phone)
        message = f"Your password reset OTP is {otp_code}. Valid for 5 minutes."
        send_sms(phone_intl, message)

        return Response(
            {"message": "OTP sent successfully"},
            status=status.HTTP_200_OK,
        )


# =========================
# 🔄 RESET PASSWORD (AUTO LOGIN)
# =========================
class ResetPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        phone = serializer.validated_data["phone"]
        new_password = serializer.validated_data["new_password"]
        otp = serializer.validated_data["otp_obj"]

        user = User.objects.get(phone=phone)

        # 🔐 Reset password
        user.set_password(new_password)
        user.is_active = True
        user.save()

        # ✅ Mark OTP as used
        otp.is_used = True
        otp.save(update_fields=["is_used"])

        # 🔑 AUTO LOGIN
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
