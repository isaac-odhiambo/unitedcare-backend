from datetime import timedelta
import random

from django.contrib.auth.models import AbstractUser
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone


# =========================
# 🔐 VALIDATORS
# =========================
phone_validator = RegexValidator(
    regex=r"^(07|01)\d{8}$",
    message="Phone number must be a valid Kenyan number (07XXXXXXXX or 01XXXXXXXX)",
)

username_validator = RegexValidator(
    regex=r"^[A-Za-z\s'-]+$",
    message="Name can contain letters, spaces, hyphens, and apostrophes only",
)

id_number_validator = RegexValidator(
    regex=r"^\d{1,9}$",
    message="ID number must be numeric and not exceed 9 digits",
)


# =========================
# 👤 USER MODEL
# =========================
class User(AbstractUser):
    ROLE_CHOICES = (
        ("admin", "Admin"),
        ("member", "Member"),
    )

    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("blocked", "Blocked"),
    )

    # Required for password recovery
    # email = models.EmailField(
    #     unique=True,
    # )
    email = models.EmailField(null=True, blank=True, unique=True)

    username = models.CharField(
        max_length=150,
        unique=True,
        validators=[username_validator],
    )

    phone = models.CharField(
        max_length=10,
        unique=True,
        db_index=True,
        validators=[phone_validator],
    )

    id_number = models.CharField(
        max_length=9,
        unique=True,
        null=True,
        blank=True,
        validators=[id_number_validator],
    )

    role = models.CharField(
        max_length=10,
        choices=ROLE_CHOICES,
        default="member",
    )

    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default="pending",
    )

    # Django account-level activation
    is_active = models.BooleanField(default=True)

    # App-level phone/OTP verification
    is_phone_verified = models.BooleanField(default=False)

    failed_login_attempts = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = "phone"
    REQUIRED_FIELDS = ["username", "email"]

    class Meta:
        ordering = ["-id"]

    # =========================
    # 🔒 ACCOUNT LOCK SECURITY
    # =========================
    def is_locked(self) -> bool:
        return bool(self.locked_until and timezone.now() < self.locked_until)

    def lock_account(self) -> None:
        self.locked_until = timezone.now() + timedelta(minutes=15)
        self.failed_login_attempts = 0
        self.save(update_fields=["locked_until", "failed_login_attempts"])

    def reset_failed_attempts(self) -> None:
        self.failed_login_attempts = 0
        self.locked_until = None
        self.save(update_fields=["failed_login_attempts", "locked_until"])

    # =========================
    # ✅ ACCESS HELPERS
    # =========================
    @property
    def is_admin(self) -> bool:
        return self.is_superuser or self.is_staff or self.role == "admin"

    @property
    def is_approved(self) -> bool:
        return self.status == "approved"

    @property
    def is_blocked(self) -> bool:
        return self.status == "blocked"

    @property
    def kyc_status(self) -> str:
        return getattr(getattr(self, "kycprofile", None), "status", "not_submitted")

    @property
    def is_kyc_approved(self) -> bool:
        return self.kyc_status == "approved"

    @property
    def has_limited_access(self) -> bool:
        """
        User can log in and use limited parts of the app
        even if phone is not yet verified.
        """
        return not self.is_blocked

    @property
    def has_full_access(self) -> bool:
        """
        Full access after:
        - not blocked
        - admin/business approval done
        - KYC approved

        OTP is NOT mandatory for full access at launch.
        """
        return (
            not self.is_blocked
            and self.status == "approved"
            and self.is_kyc_approved
        )

    @property
    def requires_phone_verification(self) -> bool:
        """
        Use this for sensitive actions like:
        - withdrawals
        - changing phone number
        - high-risk payments
        """
        return not self.is_phone_verified

    def __str__(self):
        return f"{self.username} ({self.phone})"


# =========================
# 🔢 OTP MODEL
# =========================
class OTP(models.Model):
    phone = models.CharField(max_length=10, validators=[phone_validator])
    code = models.CharField(max_length=6)

    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)
    attempts = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["phone", "created_at"]),
        ]

    def is_expired(self):
        return timezone.now() > self.created_at + timedelta(minutes=5)

    def is_locked(self):
        return self.attempts >= 5

    def mark_used(self):
        self.is_used = True
        self.save(update_fields=["is_used"])

    def increment_attempts(self):
        self.attempts += 1
        self.save(update_fields=["attempts"])

    @staticmethod
    def generate():
        return str(random.randint(100000, 999999))

    @staticmethod
    def can_request_new(phone):
        last_otp = OTP.objects.filter(phone=phone).order_by("-created_at").first()
        if not last_otp:
            return True
        return timezone.now() > last_otp.created_at + timedelta(seconds=60)

    def __str__(self):
        return f"{self.phone} - {self.code}"


# =========================
# 🧾 KYC MODEL
# =========================
class KYCProfile(models.Model):
    KYC_STATUS = (
        ("not_submitted", "Not Submitted"),
        ("submitted", "Submitted"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    )

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="kycprofile",
    )

    passport_photo = models.ImageField(upload_to="kyc/passport/")
    id_front = models.ImageField(upload_to="kyc/id_front/")
    id_back = models.ImageField(upload_to="kyc/id_back/")

    status = models.CharField(
        max_length=20,
        choices=KYC_STATUS,
        default="not_submitted",
    )

    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-submitted_at"]

    def __str__(self):
        return f"KYC - {self.user.phone} ({self.status})"

# from datetime import timedelta
# import random

# from django.contrib.auth.models import AbstractUser
# from django.core.validators import RegexValidator
# from django.db import models
# from django.utils import timezone


# # =========================
# # 🔐 VALIDATORS
# # =========================
# phone_validator = RegexValidator(
#     regex=r"^(07|01)\d{8}$",
#     message="Phone number must be a valid Kenyan number (07XXXXXXXX or 01XXXXXXXX)",
# )

# username_validator = RegexValidator(
#     regex=r"^[A-Za-z\s'-]+$",
#     message="Name can contain letters, spaces, hyphens, and apostrophes only",
# )

# id_number_validator = RegexValidator(
#     regex=r"^\d{1,9}$",
#     message="ID number must be numeric and not exceed 9 digits",
# )


# # =========================
# # 👤 USER MODEL
# # =========================
# class User(AbstractUser):
#     ROLE_CHOICES = (
#         ("admin", "Admin"),
#         ("member", "Member"),
#     )

#     STATUS_CHOICES = (
#         ("pending", "Pending"),
#         ("approved", "Approved"),
#         ("blocked", "Blocked"),
#     )

#     email = models.EmailField(null=True, blank=True)

#     username = models.CharField(
#         max_length=150,
#         unique=True,
#         validators=[username_validator],
#     )

#     phone = models.CharField(
#         max_length=10,
#         unique=True,
#         db_index=True,
#         validators=[phone_validator],
#     )

#     id_number = models.CharField(
#         max_length=9,
#         unique=True,
#         null=True,
#         blank=True,
#         validators=[id_number_validator],
#     )

#     role = models.CharField(
#         max_length=10,
#         choices=ROLE_CHOICES,
#         default="member",
#     )

#     status = models.CharField(
#         max_length=10,
#         choices=STATUS_CHOICES,
#         default="pending",
#     )

#     # Django account-level activation
#     is_active = models.BooleanField(default=True)

#     # App-level phone/OTP verification
#     is_phone_verified = models.BooleanField(default=False)

#     failed_login_attempts = models.PositiveIntegerField(default=0)
#     locked_until = models.DateTimeField(null=True, blank=True)

#     USERNAME_FIELD = "phone"
#     REQUIRED_FIELDS = ["username"]

#     class Meta:
#         ordering = ["-id"]

#     # =========================
#     # 🔒 ACCOUNT LOCK SECURITY
#     # =========================
#     def is_locked(self) -> bool:
#         return bool(self.locked_until and timezone.now() < self.locked_until)

#     def lock_account(self) -> None:
#         self.locked_until = timezone.now() + timedelta(minutes=15)
#         self.failed_login_attempts = 0
#         self.save(update_fields=["locked_until", "failed_login_attempts"])

#     def reset_failed_attempts(self) -> None:
#         self.failed_login_attempts = 0
#         self.locked_until = None
#         self.save(update_fields=["failed_login_attempts", "locked_until"])

#     # =========================
#     # ✅ ACCESS HELPERS
#     # =========================
#     @property
#     def is_admin(self) -> bool:
#         return self.is_superuser or self.is_staff or self.role == "admin"

#     @property
#     def is_approved(self) -> bool:
#         return self.status == "approved"

#     @property
#     def is_blocked(self) -> bool:
#         return self.status == "blocked"

#     @property
#     def kyc_status(self) -> str:
#         return getattr(getattr(self, "kycprofile", None), "status", "not_submitted")

#     @property
#     def is_kyc_approved(self) -> bool:
#         return self.kyc_status == "approved"

#     @property
#     def has_limited_access(self) -> bool:
#         """
#         OTP verified user can log in and use limited parts of the app.
#         """
#         return self.is_phone_verified and not self.is_blocked

#     @property
#     def has_full_access(self) -> bool:
#         """
#         Full access only after:
#         - OTP verified
#         - not blocked
#         - KYC approved
#         - admin/business approval done
#         """
#         return (
#             self.is_phone_verified
#             and not self.is_blocked
#             and self.status == "approved"
#             and self.is_kyc_approved
#         )

#     def __str__(self):
#         return f"{self.username} ({self.phone})"


# # =========================
# # 🔢 OTP MODEL
# # =========================
# class OTP(models.Model):
#     phone = models.CharField(max_length=10, validators=[phone_validator])
#     code = models.CharField(max_length=6)

#     created_at = models.DateTimeField(auto_now_add=True)
#     is_used = models.BooleanField(default=False)
#     attempts = models.PositiveIntegerField(default=0)

#     class Meta:
#         ordering = ["-created_at"]
#         indexes = [
#             models.Index(fields=["phone", "created_at"]),
#         ]

#     def is_expired(self):
#         return timezone.now() > self.created_at + timedelta(minutes=5)

#     def is_locked(self):
#         return self.attempts >= 5

#     def mark_used(self):
#         self.is_used = True
#         self.save(update_fields=["is_used"])

#     def increment_attempts(self):
#         self.attempts += 1
#         self.save(update_fields=["attempts"])

#     @staticmethod
#     def generate():
#         return str(random.randint(100000, 999999))

#     @staticmethod
#     def can_request_new(phone):
#         last_otp = OTP.objects.filter(phone=phone).order_by("-created_at").first()
#         if not last_otp:
#             return True
#         return timezone.now() > last_otp.created_at + timedelta(seconds=60)

#     def __str__(self):
#         return f"{self.phone} - {self.code}"


# # =========================
# # 🧾 KYC MODEL
# # =========================
# class KYCProfile(models.Model):
#     KYC_STATUS = (
#         ("not_submitted", "Not Submitted"),
#         ("submitted", "Submitted"),
#         ("approved", "Approved"),
#         ("rejected", "Rejected"),
#     )

#     user = models.OneToOneField(
#         User,
#         on_delete=models.CASCADE,
#         related_name="kycprofile",
#     )

#     passport_photo = models.ImageField(upload_to="kyc/passport/")
#     id_front = models.ImageField(upload_to="kyc/id_front/")
#     id_back = models.ImageField(upload_to="kyc/id_back/")

#     status = models.CharField(
#         max_length=20,
#         choices=KYC_STATUS,
#         default="not_submitted",
#     )

#     submitted_at = models.DateTimeField(auto_now_add=True)

#     class Meta:
#         ordering = ["-submitted_at"]

#     def __str__(self):
#         return f"KYC - {self.user.phone} ({self.status})"