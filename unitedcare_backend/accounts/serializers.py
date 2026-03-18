from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone
from rest_framework import serializers
from .models import OTP, KYCProfile
import re

User = get_user_model()

MAX_FAILED_ATTEMPTS = 5
MAX_OTP_ATTEMPTS = 5


# =========================================================
# 🔐 REGISTER SERIALIZER
# =========================================================
class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = [
            "username",
            "phone",
            "id_number",
            "password",
        ]

    def validate_username(self, value):
        value = " ".join(str(value).strip().split())
        if not re.match(r"^[A-Za-z\s'-]+$", value):
            raise serializers.ValidationError(
                "Name can contain letters, spaces, hyphens, and apostrophes only"
            )
        return value

    def validate_phone(self, value):
        value = str(value).strip()
        if not re.match(r"^(07|01)\d{8}$", value):
            raise serializers.ValidationError(
                "Enter a valid Kenyan phone number (07XXXXXXXX or 01XXXXXXXX)"
            )
        return value

    def validate_id_number(self, value):
        if value in [None, ""]:
            return None

        value = str(value).strip()
        if not re.match(r"^\d{1,9}$", value):
            raise serializers.ValidationError(
                "ID number must be numeric and not exceed 9 digits"
            )
        return value

    def validate_password(self, value):
        validate_password(value)
        return value

    def create(self, validated_data):
        user = User.objects.create_user(
            username=validated_data["username"],
            phone=validated_data["phone"],
            id_number=validated_data.get("id_number"),
            password=validated_data["password"],
            role="member",
            status="pending",
            is_active=False,
        )
        return user


# =========================================================
# 🔐 LOGIN SERIALIZER (WITH ACCOUNT LOCKING)
# =========================================================
class LoginSerializer(serializers.Serializer):
    phone = serializers.CharField()
    password = serializers.CharField(write_only=True)

    def validate_phone(self, value):
        value = str(value).strip()
        if not re.match(r"^(07|01)\d{8}$", value):
            raise serializers.ValidationError(
                "Enter a valid Kenyan phone number (07XXXXXXXX or 01XXXXXXXX)"
            )
        return value

    def validate(self, data):
        phone = data["phone"]
        password = data["password"]

        try:
            user = User.objects.get(phone=phone)
        except User.DoesNotExist:
            raise serializers.ValidationError("Invalid phone or password")

        if user.status == "blocked":
            raise serializers.ValidationError(
                "Your account has been blocked. Contact support."
            )

        if not user.is_active:
            raise serializers.ValidationError(
                "Account not activated. Please verify OTP."
            )

        if user.is_locked():
            remaining = int(
                (user.locked_until - timezone.now()).total_seconds() / 60
            ) + 1
            raise serializers.ValidationError(
                f"Account locked. Try again in {remaining} minutes."
            )

        auth_user = authenticate(
            request=self.context.get("request"),
            username=phone,
            password=password,
        )

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

        user.reset_failed_attempts()
        data["user"] = user
        return data


# =========================================================
# 📩 FORGOT PASSWORD (REQUEST OTP)
# =========================================================
class ForgotPasswordSerializer(serializers.Serializer):
    phone = serializers.CharField()

    def validate_phone(self, value):
        value = str(value).strip()

        if not re.match(r"^(07|01)\d{8}$", value):
            raise serializers.ValidationError(
                "Enter a valid Kenyan phone number"
            )

        if not User.objects.filter(phone=value).exists():
            raise serializers.ValidationError(
                "User with this phone does not exist"
            )

        return value


# =========================================================
# 🔄 RESET PASSWORD (WITH OTP ATTEMPT LIMITING)
# =========================================================
class ResetPasswordSerializer(serializers.Serializer):
    phone = serializers.CharField()
    otp = serializers.CharField()
    new_password = serializers.CharField(write_only=True)

    def validate_phone(self, value):
        value = str(value).strip()

        if not re.match(r"^(07|01)\d{8}$", value):
            raise serializers.ValidationError(
                "Enter a valid Kenyan phone number"
            )
        return value

    def validate_otp(self, value):
        value = str(value).strip()
        if not re.match(r"^\d{4,6}$", value):
            raise serializers.ValidationError("Enter a valid OTP code")
        return value

    def validate(self, data):
        phone = data["phone"]
        otp_code = data["otp"]
        password = data["new_password"]

        try:
            otp = OTP.objects.filter(
                phone=phone,
                is_used=False
            ).latest("created_at")
        except OTP.DoesNotExist:
            raise serializers.ValidationError("Invalid or expired OTP")

        if otp.is_expired():
            otp.is_used = True
            otp.save(update_fields=["is_used"])
            raise serializers.ValidationError("OTP has expired")

        if otp.attempts >= MAX_OTP_ATTEMPTS:
            otp.is_used = True
            otp.save(update_fields=["is_used"])
            raise serializers.ValidationError(
                "Too many incorrect attempts. Request a new OTP."
            )

        if otp.code != otp_code:
            otp.attempts += 1
            otp.save(update_fields=["attempts"])

            remaining = MAX_OTP_ATTEMPTS - otp.attempts

            if remaining <= 0:
                otp.is_used = True
                otp.save(update_fields=["is_used"])
                raise serializers.ValidationError(
                    "Too many incorrect attempts. OTP locked."
                )

            raise serializers.ValidationError(
                f"Incorrect OTP. {remaining} attempts remaining."
            )

        validate_password(password)

        data["otp_obj"] = otp
        return data

    def save(self):
        phone = self.validated_data["phone"]
        password = self.validated_data["new_password"]
        otp = self.validated_data["otp_obj"]

        user = User.objects.get(phone=phone)
        user.set_password(password)
        user.save(update_fields=["password"])

        otp.is_used = True
        otp.save(update_fields=["is_used"])

        return user


# =========================================================
# 🔁 RESEND OTP
# =========================================================
class ResendOTPSerializer(serializers.Serializer):
    phone = serializers.CharField()

    def validate_phone(self, value):
        value = str(value).strip()

        if not re.match(r"^(07|01)\d{8}$", value):
            raise serializers.ValidationError(
                "Enter a valid Kenyan phone number"
            )

        try:
            user = User.objects.get(phone=value)
        except User.DoesNotExist:
            raise serializers.ValidationError(
                "User with this phone does not exist"
            )

        if user.is_active:
            raise serializers.ValidationError(
                "Account is already verified."
            )

        if user.status == "blocked":
            raise serializers.ValidationError(
                "Your account has been blocked."
            )

        self.context["user"] = user
        return value


# =========================================================
# 👤 CURRENT USER
# =========================================================
class MeSerializer(serializers.ModelSerializer):
    is_admin = serializers.SerializerMethodField()
    kyc_status = serializers.SerializerMethodField()
    is_kyc_approved = serializers.SerializerMethodField()
    has_limited_access = serializers.SerializerMethodField()
    has_full_access = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "phone",
            "email",
            "id_number",
            "role",
            "status",
            "is_active",
            "is_admin",
            "kyc_status",
            "is_kyc_approved",
            "has_limited_access",
            "has_full_access",
        ]

    def get_is_admin(self, obj):
        return obj.is_admin

    def get_kyc_status(self, obj):
        return obj.kyc_status

    def get_is_kyc_approved(self, obj):
        return obj.is_kyc_approved

    def get_has_limited_access(self, obj):
        return obj.has_limited_access

    def get_has_full_access(self, obj):
        return obj.has_full_access


class UpdateMeSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["username", "email"]

    def validate_username(self, value):
        value = " ".join(str(value).strip().split())
        if not re.match(r"^[A-Za-z\s'-]+$", value):
            raise serializers.ValidationError(
                "Name can contain letters, spaces, hyphens, and apostrophes only"
            )
        return value

    def validate_email(self, value):
        if value in [None, ""]:
            return None
        return str(value).strip().lower()


# =========================================================
# 🧾 KYC SUBMISSION
# =========================================================
class KYCSubmitSerializer(serializers.ModelSerializer):
    class Meta:
        model = KYCProfile
        fields = ["passport_photo", "id_front", "id_back"]

    def validate(self, attrs):
        for field in ["passport_photo", "id_front", "id_back"]:
            if not attrs.get(field):
                raise serializers.ValidationError(
                    {field: "This file is required."}
                )
        return attrs