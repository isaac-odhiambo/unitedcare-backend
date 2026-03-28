from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.validators import validate_email
from django.utils import timezone
from rest_framework import serializers
from .models import OTP, KYCProfile
import re

User = get_user_model()

MAX_FAILED_ATTEMPTS = 5
MAX_OTP_ATTEMPTS = 5


# =========================================================
# HELPERS
# =========================================================
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


# =========================================================
# REGISTER SERIALIZER
# =========================================================
class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)
    email = serializers.EmailField(required=True)

    class Meta:
        model = User
        fields = [
            "username",
            "phone",
            "email",
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
        value = normalize_local_kenyan_phone(value)
        if not re.match(r"^(07|01)\d{8}$", value):
            raise serializers.ValidationError(
                "Enter a valid Kenyan phone number (07XXXXXXXX or 01XXXXXXXX)"
            )

        if User.objects.filter(phone=value).exists():
            raise serializers.ValidationError(
                "A user with this phone number already exists"
            )
        return value

    def validate_email(self, value):
        value = str(value).strip().lower()
        validate_email(value)

        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError(
                "A user with this email already exists"
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
            email=validated_data["email"],
            id_number=validated_data.get("id_number"),
            password=validated_data["password"],
            role="member",
            status="pending",
            is_active=True,
            is_phone_verified=False,
        )
        return user


# =========================================================
# LOGIN SERIALIZER
# - phone + password only
# - no KYC requirement
# - no phone verification requirement
# - prevents 500 for invalid credentials
# =========================================================
class LoginSerializer(serializers.Serializer):
    phone = serializers.CharField()
    password = serializers.CharField(write_only=True)

    def validate_phone(self, value):
        value = normalize_local_kenyan_phone(value)
        if not re.match(r"^(07|01)\d{8}$", value):
            raise serializers.ValidationError(
                "Enter a valid Kenyan phone number (07XXXXXXXX or 01XXXXXXXX)"
            )
        return value

    def validate(self, data):
        phone = normalize_local_kenyan_phone(data.get("phone", ""))
        password = data.get("password", "")

        if not phone or not password:
            raise serializers.ValidationError(
                {"detail": "Phone and password are required."}
            )

        user = User.objects.filter(phone=phone).first()
        if not user:
            raise serializers.ValidationError(
                {"detail": "Invalid phone or password."}
            )

        if user.status == "blocked":
            raise serializers.ValidationError(
                {"detail": "Your account has been blocked. Contact support."}
            )

        if not user.is_active:
            raise serializers.ValidationError(
                {"detail": "Account disabled. Contact support."}
            )

        if hasattr(user, "is_locked") and user.is_locked():
            locked_until = getattr(user, "locked_until", None)
            if locked_until:
                remaining = int(
                    max(0, (locked_until - timezone.now()).total_seconds()) / 60
                ) + 1
            else:
                remaining = 15

            raise serializers.ValidationError(
                {"detail": f"Account locked. Try again in {remaining} minutes."}
            )

        auth_user = authenticate(
            request=self.context.get("request"),
            username=phone,
            password=password,
        )

        if not auth_user:
            current_attempts = int(getattr(user, "failed_login_attempts", 0) or 0) + 1
            user.failed_login_attempts = current_attempts
            user.save(update_fields=["failed_login_attempts"])

            if current_attempts >= MAX_FAILED_ATTEMPTS:
                if hasattr(user, "lock_account"):
                    user.lock_account()
                raise serializers.ValidationError(
                    {"detail": "Too many failed attempts. Account locked for 15 minutes."}
                )

            remaining = MAX_FAILED_ATTEMPTS - current_attempts
            raise serializers.ValidationError(
                {"detail": f"Invalid phone or password. {remaining} attempts remaining."}
            )

        if hasattr(auth_user, "reset_failed_attempts"):
            auth_user.reset_failed_attempts()
        else:
            if hasattr(auth_user, "failed_login_attempts"):
                auth_user.failed_login_attempts = 0
            if hasattr(auth_user, "locked_until"):
                auth_user.locked_until = None

            update_fields = []
            if hasattr(auth_user, "failed_login_attempts"):
                update_fields.append("failed_login_attempts")
            if hasattr(auth_user, "locked_until"):
                update_fields.append("locked_until")
            if update_fields:
                auth_user.save(update_fields=update_fields)

        data["user"] = auth_user
        return data


# =========================================================
# FORGOT PASSWORD (REQUEST EMAIL RESET)
# =========================================================
class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value):
        value = str(value).strip().lower()
        validate_email(value)

        if not User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError(
                "User with this email does not exist"
            )

        return value


# =========================================================
# RESET PASSWORD (EMAIL-BASED)
# =========================================================
class ResetPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField()
    new_password = serializers.CharField(write_only=True)

    def validate_email(self, value):
        value = str(value).strip().lower()
        validate_email(value)

        if not User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError(
                "User with this email does not exist"
            )

        return value

    def validate_new_password(self, value):
        validate_password(value)
        return value

    def save(self):
        email = self.validated_data["email"]
        password = self.validated_data["new_password"]

        user = User.objects.get(email__iexact=email)
        user.set_password(password)
        user.save(update_fields=["password"])

        return user


# =========================================================
# RESEND OTP
# Used only for phone verification / sensitive actions
# =========================================================
class ResendOTPSerializer(serializers.Serializer):
    phone = serializers.CharField()

    def validate_phone(self, value):
        value = normalize_local_kenyan_phone(value)

        if not re.match(r"^(07|01)\d{8}$", value):
            raise serializers.ValidationError(
                "Enter a valid Kenyan phone number"
            )

        user = User.objects.filter(phone=value).first()
        if not user:
            raise serializers.ValidationError(
                "User with this phone does not exist"
            )

        if getattr(user, "is_phone_verified", False):
            raise serializers.ValidationError(
                "Phone is already verified."
            )

        if not getattr(user, "is_active", False):
            raise serializers.ValidationError(
                "Account is disabled."
            )

        if getattr(user, "status", "") == "blocked":
            raise serializers.ValidationError(
                "Your account has been blocked."
            )

        self.context["user"] = user
        return value


# =========================================================
# CURRENT USER
# =========================================================
class MeSerializer(serializers.ModelSerializer):
    is_admin = serializers.SerializerMethodField()
    kyc_status = serializers.SerializerMethodField()
    is_kyc_approved = serializers.SerializerMethodField()
    has_limited_access = serializers.SerializerMethodField()
    has_full_access = serializers.SerializerMethodField()
    requires_phone_verification = serializers.SerializerMethodField()

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
            "is_phone_verified",
            "is_admin",
            "kyc_status",
            "is_kyc_approved",
            "has_limited_access",
            "has_full_access",
            "requires_phone_verification",
        ]

    def get_is_admin(self, obj):
        return getattr(obj, "is_admin", False)

    def get_kyc_status(self, obj):
        return getattr(obj, "kyc_status", "not_submitted")

    def get_is_kyc_approved(self, obj):
        return getattr(obj, "is_kyc_approved", False)

    def get_has_limited_access(self, obj):
        return getattr(obj, "has_limited_access", True)

    def get_has_full_access(self, obj):
        return getattr(obj, "has_full_access", False)

    def get_requires_phone_verification(self, obj):
        return getattr(obj, "requires_phone_verification", False)


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
        value = str(value).strip().lower()
        validate_email(value)

        qs = User.objects.filter(email__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)

        if qs.exists():
            raise serializers.ValidationError(
                "A user with this email already exists"
            )

        return value


# =========================================================
# KYC SUBMISSION
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
# original
# from django.contrib.auth import authenticate, get_user_model
# from django.contrib.auth.password_validation import validate_password
# from django.utils import timezone
# from rest_framework import serializers
# from .models import OTP, KYCProfile
# import re

# User = get_user_model()

# MAX_FAILED_ATTEMPTS = 5
# MAX_OTP_ATTEMPTS = 5


# # =========================================================
# # 🔐 REGISTER SERIALIZER
# # =========================================================
# class RegisterSerializer(serializers.ModelSerializer):
#     password = serializers.CharField(write_only=True)

#     class Meta:
#         model = User
#         fields = [
#             "username",
#             "phone",
#             "id_number",
#             "password",
#         ]

#     def validate_username(self, value):
#         value = " ".join(str(value).strip().split())
#         if not re.match(r"^[A-Za-z\s'-]+$", value):
#             raise serializers.ValidationError(
#                 "Name can contain letters, spaces, hyphens, and apostrophes only"
#             )
#         return value

#     def validate_phone(self, value):
#         value = str(value).strip()
#         if not re.match(r"^(07|01)\d{8}$", value):
#             raise serializers.ValidationError(
#                 "Enter a valid Kenyan phone number (07XXXXXXXX or 01XXXXXXXX)"
#             )
#         return value

#     def validate_id_number(self, value):
#         if value in [None, ""]:
#             return None

#         value = str(value).strip()
#         if not re.match(r"^\d{1,9}$", value):
#             raise serializers.ValidationError(
#                 "ID number must be numeric and not exceed 9 digits"
#             )
#         return value

#     def validate_password(self, value):
#         validate_password(value)
#         return value

#     def create(self, validated_data):
#         user = User.objects.create_user(
#             username=validated_data["username"],
#             phone=validated_data["phone"],
#             id_number=validated_data.get("id_number"),
#             password=validated_data["password"],
#             role="member",
#             status="pending",
#             is_active=True,
#             is_phone_verified=False,
#         )
#         return user


# # =========================================================
# # 🔐 LOGIN SERIALIZER (WITH ACCOUNT LOCKING)
# # =========================================================
# class LoginSerializer(serializers.Serializer):
#     phone = serializers.CharField()
#     password = serializers.CharField(write_only=True)

#     def validate_phone(self, value):
#         value = str(value).strip()
#         if not re.match(r"^(07|01)\d{8}$", value):
#             raise serializers.ValidationError(
#                 "Enter a valid Kenyan phone number (07XXXXXXXX or 01XXXXXXXX)"
#             )
#         return value

#     def validate(self, data):
#         phone = data["phone"]
#         password = data["password"]

#         try:
#             user = User.objects.get(phone=phone)
#         except User.DoesNotExist:
#             raise serializers.ValidationError("Invalid phone or password")

#         if user.status == "blocked":
#             raise serializers.ValidationError(
#                 "Your account has been blocked. Contact support."
#             )

#         if not user.is_active:
#             raise serializers.ValidationError(
#                 "Account disabled. Contact support."
#             )

#         if not user.is_phone_verified:
#             raise serializers.ValidationError(
#                 "Account not verified. Please verify OTP."
#             )

#         if user.is_locked():
#             remaining = int(
#                 (user.locked_until - timezone.now()).total_seconds() / 60
#             ) + 1
#             raise serializers.ValidationError(
#                 f"Account locked. Try again in {remaining} minutes."
#             )

#         auth_user = authenticate(
#             request=self.context.get("request"),
#             username=phone,
#             password=password,
#         )

#         if not auth_user:
#             user.failed_login_attempts += 1
#             user.save(update_fields=["failed_login_attempts"])

#             if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
#                 user.lock_account()
#                 raise serializers.ValidationError(
#                     "Too many failed attempts. Account locked for 15 minutes."
#                 )

#             remaining = MAX_FAILED_ATTEMPTS - user.failed_login_attempts
#             raise serializers.ValidationError(
#                 f"Invalid credentials. {remaining} attempts remaining."
#             )

#         user.reset_failed_attempts()
#         data["user"] = user
#         return data


# # =========================================================
# # 📩 FORGOT PASSWORD (REQUEST OTP)
# # =========================================================
# class ForgotPasswordSerializer(serializers.Serializer):
#     phone = serializers.CharField()

#     def validate_phone(self, value):
#         value = str(value).strip()

#         if not re.match(r"^(07|01)\d{8}$", value):
#             raise serializers.ValidationError(
#                 "Enter a valid Kenyan phone number"
#             )

#         if not User.objects.filter(phone=value).exists():
#             raise serializers.ValidationError(
#                 "User with this phone does not exist"
#             )

#         return value


# # =========================================================
# # 🔄 RESET PASSWORD (WITH OTP ATTEMPT LIMITING)
# # =========================================================
# class ResetPasswordSerializer(serializers.Serializer):
#     phone = serializers.CharField()
#     otp = serializers.CharField()
#     new_password = serializers.CharField(write_only=True)

#     def validate_phone(self, value):
#         value = str(value).strip()

#         if not re.match(r"^(07|01)\d{8}$", value):
#             raise serializers.ValidationError(
#                 "Enter a valid Kenyan phone number"
#             )
#         return value

#     def validate_otp(self, value):
#         value = str(value).strip()
#         if not re.match(r"^\d{4,6}$", value):
#             raise serializers.ValidationError("Enter a valid OTP code")
#         return value

#     def validate(self, data):
#         phone = data["phone"]
#         otp_code = data["otp"]
#         password = data["new_password"]

#         try:
#             otp = OTP.objects.filter(
#                 phone=phone,
#                 is_used=False
#             ).latest("created_at")
#         except OTP.DoesNotExist:
#             raise serializers.ValidationError("Invalid or expired OTP")

#         if otp.is_expired():
#             otp.is_used = True
#             otp.save(update_fields=["is_used"])
#             raise serializers.ValidationError("OTP has expired")

#         if otp.attempts >= MAX_OTP_ATTEMPTS:
#             otp.is_used = True
#             otp.save(update_fields=["is_used"])
#             raise serializers.ValidationError(
#                 "Too many incorrect attempts. Request a new OTP."
#             )

#         if otp.code != otp_code:
#             otp.attempts += 1
#             otp.save(update_fields=["attempts"])

#             remaining = MAX_OTP_ATTEMPTS - otp.attempts

#             if remaining <= 0:
#                 otp.is_used = True
#                 otp.save(update_fields=["is_used"])
#                 raise serializers.ValidationError(
#                     "Too many incorrect attempts. OTP locked."
#                 )

#             raise serializers.ValidationError(
#                 f"Incorrect OTP. {remaining} attempts remaining."
#             )

#         validate_password(password)

#         data["otp_obj"] = otp
#         return data

#     def save(self):
#         phone = self.validated_data["phone"]
#         password = self.validated_data["new_password"]
#         otp = self.validated_data["otp_obj"]

#         user = User.objects.get(phone=phone)
#         user.set_password(password)
#         user.save(update_fields=["password"])

#         otp.is_used = True
#         otp.save(update_fields=["is_used"])

#         return user


# # =========================================================
# # 🔁 RESEND OTP
# # =========================================================
# class ResendOTPSerializer(serializers.Serializer):
#     phone = serializers.CharField()

#     def validate_phone(self, value):
#         value = str(value).strip()

#         if not re.match(r"^(07|01)\d{8}$", value):
#             raise serializers.ValidationError(
#                 "Enter a valid Kenyan phone number"
#             )

#         try:
#             user = User.objects.get(phone=value)
#         except User.DoesNotExist:
#             raise serializers.ValidationError(
#                 "User with this phone does not exist"
#             )

#         if user.is_phone_verified:
#             raise serializers.ValidationError(
#                 "Account is already verified."
#             )

#         if not user.is_active:
#             raise serializers.ValidationError(
#                 "Account is disabled."
#             )

#         if user.status == "blocked":
#             raise serializers.ValidationError(
#                 "Your account has been blocked."
#             )

#         self.context["user"] = user
#         return value


# # =========================================================
# # 👤 CURRENT USER
# # =========================================================
# class MeSerializer(serializers.ModelSerializer):
#     is_admin = serializers.SerializerMethodField()
#     kyc_status = serializers.SerializerMethodField()
#     is_kyc_approved = serializers.SerializerMethodField()
#     has_limited_access = serializers.SerializerMethodField()
#     has_full_access = serializers.SerializerMethodField()

#     class Meta:
#         model = User
#         fields = [
#             "id",
#             "username",
#             "phone",
#             "email",
#             "id_number",
#             "role",
#             "status",
#             "is_active",
#             "is_phone_verified",
#             "is_admin",
#             "kyc_status",
#             "is_kyc_approved",
#             "has_limited_access",
#             "has_full_access",
#         ]

#     def get_is_admin(self, obj):
#         return obj.is_admin

#     def get_kyc_status(self, obj):
#         return obj.kyc_status

#     def get_is_kyc_approved(self, obj):
#         return obj.is_kyc_approved

#     def get_has_limited_access(self, obj):
#         return obj.has_limited_access

#     def get_has_full_access(self, obj):
#         return obj.has_full_access


# class UpdateMeSerializer(serializers.ModelSerializer):
#     class Meta:
#         model = User
#         fields = ["username", "email"]

#     def validate_username(self, value):
#         value = " ".join(str(value).strip().split())
#         if not re.match(r"^[A-Za-z\s'-]+$", value):
#             raise serializers.ValidationError(
#                 "Name can contain letters, spaces, hyphens, and apostrophes only"
#             )
#         return value

#     def validate_email(self, value):
#         if value in [None, ""]:
#             return None
#         return str(value).strip().lower()


# # =========================================================
# # 🧾 KYC SUBMISSION
# # =========================================================
# class KYCSubmitSerializer(serializers.ModelSerializer):
#     class Meta:
#         model = KYCProfile
#         fields = ["passport_photo", "id_front", "id_back"]

#     def validate(self, attrs):
#         for field in ["passport_photo", "id_front", "id_back"]:
#             if not attrs.get(field):
#                 raise serializers.ValidationError(
#                     {field: "This file is required."}
#                 )
#         return attrs