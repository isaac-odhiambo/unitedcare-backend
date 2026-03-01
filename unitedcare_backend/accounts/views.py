from datetime import timedelta
from django.conf import settings

from django.contrib.auth import get_user_model
from django.utils import timezone

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from .serializers import ResendOTPSerializer
from .serializers import MeSerializer, UpdateMeSerializer, KYCSubmitSerializer
from .models import KYCProfile
from rest_framework.permissions import IsAuthenticated


from .models import OTP
from .serializers import (
    RegisterSerializer,
    LoginSerializer,
    ForgotPasswordSerializer,
    ResetPasswordSerializer,
)
from .throttles import LoginThrottle, OTPThrottle
from .utils.phone import normalize_kenyan_phone
from .utils.sms import send_sms

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

        try:
            otp = OTP.objects.filter(phone=phone, code=code, is_used=False).latest("created_at")
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

        try:
            user = User.objects.get(phone=phone)
        except User.DoesNotExist:
            return Response(
                {"detail": "User not found for this phone."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ✅ Activate account
        user.is_active = True
        user.save(update_fields=["is_active"])

        # ✅ Mark OTP used
        otp.is_used = True
        otp.save(update_fields=["is_used"])

        return Response({"message": "Account verified successfully"}, status=status.HTTP_200_OK)


# =========================
# 🔑 LOGIN (JWT) + BEST-PRACTICE CHECKS
# =========================
class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LoginThrottle]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = serializer.validated_data["user"]

        # ✅ 1) Block locked accounts
        if user.is_locked():
            return Response(
                {"detail": "Account temporarily locked. Try again later."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # ✅ 2) Must be OTP-activated
        if not user.is_active:
            return Response(
                {"detail": "Account not activated. Verify OTP first."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # ✅ 3) Blocked users cannot login (recommended)
        if getattr(user, "status", "") == "blocked":
            return Response(
                {"detail": "Account blocked. Contact admin."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # ✅ reset failed attempts after successful login
        user.reset_failed_attempts()

        refresh = RefreshToken.for_user(user)

        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "role": user.role,
                "status": user.status,
                "is_admin": getattr(user, "is_admin", False),
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
        otp_count = OTP.objects.filter(phone=phone, created_at__gte=hour_ago).count()
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

        return Response({"message": "OTP sent successfully"}, status=status.HTTP_200_OK)


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

        try:
            user = User.objects.get(phone=phone)
        except User.DoesNotExist:
            return Response({"detail": "User not found."}, status=status.HTTP_400_BAD_REQUEST)

        # 🔐 Reset password
        user.set_password(new_password)

        # ✅ Ensure account is active after password reset
        user.is_active = True

        # ✅ Reset lock state too
        user.failed_login_attempts = 0
        user.locked_until = None
        user.save(update_fields=["password", "is_active", "failed_login_attempts", "locked_until"])

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
class ResendOTPView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        serializer = ResendOTPSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        phone = serializer.validated_data["phone"]

        # 🔁 optional: limit spam (1 OTP per 60 seconds)
        last = OTP.objects.filter(phone=phone).order_by("-created_at").first()
        if last and (timezone.now() - last.created_at).total_seconds() < 60:
            wait = 60 - int((timezone.now() - last.created_at).total_seconds())
            return Response(
                {"detail": f"Please wait {wait} seconds before requesting a new OTP."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # ✅ create new OTP
        code = OTP.generate()
        otp = OTP.objects.create(phone=phone, code=code)

        # ✅ TODO: send SMS here
        # For now DEV mode:
        if settings.DEBUG:
            print(f"DEV RESEND OTP for {phone}: {code}")

        return Response(
            {"detail": "OTP resent. Please check your phone."},
            status=status.HTTP_200_OK
        )
class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        data = MeSerializer(user).data

        # add is_admin computed flag
        data["is_admin"] = getattr(user, "is_admin", False)
        return Response(data, status=status.HTTP_200_OK)

    def patch(self, request):
        user = request.user
        serializer = UpdateMeSerializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        data = MeSerializer(user).data
        data["is_admin"] = getattr(user, "is_admin", False)
        return Response(data, status=status.HTTP_200_OK)


# =========================
# 🪪 KYC SUBMIT (multipart)
# =========================
class KYCSubmitView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user

        # create or update KYC profile
        kyc_obj, _ = KYCProfile.objects.get_or_create(user=user)

        serializer = KYCSubmitSerializer(kyc_obj, data=request.data, partial=False)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        # optional: set status to submitted automatically
        if kyc_obj.status != "submitted":
            kyc_obj.status = "submitted"
            kyc_obj.save(update_fields=["status"])

        return Response(
            {"message": "KYC submitted successfully", "kyc_status": kyc_obj.status},
            status=status.HTTP_200_OK,
        )
    