from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone
from rest_framework import serializers
from datetime import timedelta
import re

from .models import OTP

User = get_user_model()

MAX_FAILED_ATTEMPTS = 5


# =========================
# 🔐 REGISTER SERIALIZER
# =========================
class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = [
            "username",
            "phone",
            "id_number",
            "password",
            "role",
        ]

    def validate_username(self, value):
        if not re.match(r"^[A-Za-z]+$", value):
            raise serializers.ValidationError(
                "Username must contain letters only"
            )
        return value

    def validate_phone(self, value):
        if not re.match(r"^(07|01)\d{8}$", value):
            raise serializers.ValidationError(
                "Enter a valid Kenyan phone number (07XXXXXXXX or 01XXXXXXXX)"
            )
        return value

    def validate_id_number(self, value):
        if value and not re.match(r"^\d{1,9}$", value):
            raise serializers.ValidationError(
                "ID number must be numeric and not exceed 9 digits"
            )
        return value

    def validate_password(self, value):
        # Minimum 4 characters + Django security checks
        validate_password(value)
        return value

    def create(self, validated_data):
        user = User.objects.create_user(
            username=validated_data["username"],
            phone=validated_data["phone"],
            id_number=validated_data.get("id_number"),
            role=validated_data.get("role", "member"),
            password=validated_data["password"],
            is_active=False,  # activated via OTP
        )
        return user


# =========================
# 🔐 LOGIN SERIALIZER
# =========================
class LoginSerializer(serializers.Serializer):
    phone = serializers.CharField()
    password = serializers.CharField(write_only=True)

    def validate(self, data):
        phone = data["phone"]
        password = data["password"]

        try:
            user = User.objects.get(phone=phone)
        except User.DoesNotExist:
            raise serializers.ValidationError("Invalid phone or password")

        # 🚫 Blocked account
        if user.status == "blocked":
            raise serializers.ValidationError(
                "Your account has been blocked. Contact support."
            )

        # 🚫 Account not activated
        if not user.is_active:
            raise serializers.ValidationError(
                "Account not activated. Please verify OTP."
            )

        # 🔒 Account locked
        if user.is_locked():
            remaining = int(
                (user.locked_until - timezone.now()).total_seconds() / 60
            ) + 1
            raise serializers.ValidationError(
                f"Account locked. Try again in {remaining} minutes."
            )

        # 🔑 Authenticate
        auth_user = authenticate(username=phone, password=password)

        if not auth_user:
            user.failed_login_attempts += 1
            user.save(update_fields=["failed_login_attempts"])

            if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
                user.lock_account()
                raise serializers.ValidationError(
                    "Too many failed attempts. Account locked for 15 minutes."
                )

            remaining = MAX_FAILED_ATTEMPTS - user.failed_login_attempts
            raise serializers.ValidationError(
                f"Invalid credentials. {remaining} attempts remaining."
            )

        # ✅ Successful login
        user.reset_failed_attempts()
        data["user"] = user
        return data


# =========================
# 📩 FORGOT PASSWORD (REQUEST OTP)
# =========================
class ForgotPasswordSerializer(serializers.Serializer):
    phone = serializers.CharField()

    def validate_phone(self, value):
        if not re.match(r"^(07|01)\d{8}$", value):
            raise serializers.ValidationError(
                "Enter a valid Kenyan phone number"
            )

        if not User.objects.filter(phone=value).exists():
            raise serializers.ValidationError(
                "User with this phone does not exist"
            )

        return value


# =========================
# 🔄 RESET PASSWORD (VERIFY OTP)
# =========================
class ResetPasswordSerializer(serializers.Serializer):
    phone = serializers.CharField()
    otp = serializers.CharField()
    new_password = serializers.CharField(write_only=True)

    def validate(self, data):
        phone = data["phone"]
        otp_code = data["otp"]
        password = data["new_password"]

        try:
            otp = OTP.objects.filter(
                phone=phone,
                code=otp_code,
                is_used=False
            ).latest("created_at")
        except OTP.DoesNotExist:
            raise serializers.ValidationError(
                "Invalid or expired OTP"
            )

        if otp.is_expired():
            raise serializers.ValidationError(
                "OTP has expired"
            )

        # Validate password strength
        validate_password(password)

        data["otp_obj"] = otp
        return data
