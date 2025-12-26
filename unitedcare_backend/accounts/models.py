from django.contrib.auth.models import AbstractUser
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone
from datetime import timedelta
import random


# =========================
# 🔐 VALIDATORS
# =========================

phone_validator = RegexValidator(
    regex=r'^(07|01)\d{8}$',
    message="Phone number must be a valid Kenyan number (07XXXXXXXX or 01XXXXXXXX)"
)

username_validator = RegexValidator(
    regex=r'^[A-Za-z]+$',
    message="Username must contain letters only"
)

id_number_validator = RegexValidator(
    regex=r'^\d{1,9}$',
    message="ID number must be numeric and not exceed 9 digits"
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

    # 🔤 Username: letters only
    username = models.CharField(
        max_length=150,
        unique=True,
        validators=[username_validator]
    )

    # 📱 Phone number (LOGIN FIELD)
    phone = models.CharField(
        max_length=10,
        unique=True,
        validators=[phone_validator]
    )

    # 🪪 National ID (OPTIONAL – used during KYC later)
    id_number = models.CharField(
        max_length=9,
        unique=True,
        null=True,
        blank=True,
        validators=[id_number_validator]
    )

    role = models.CharField(
        max_length=10,
        choices=ROLE_CHOICES,
        default="member"
    )

    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default="pending"
    )

    # 🔐 OTP-based activation (login requires is_active=True)
    is_active = models.BooleanField(default=False)

    # 🔒 Login security
    failed_login_attempts = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    # 🔑 Authentication config
    USERNAME_FIELD = "phone"
    REQUIRED_FIELDS = ["username"]

    # =========================
    # 🔒 SECURITY METHODS
    # =========================

    def is_locked(self):
        return self.locked_until and timezone.now() < self.locked_until

    def lock_account(self):
        self.locked_until = timezone.now() + timedelta(minutes=15)
        self.failed_login_attempts = 0
        self.save(update_fields=["locked_until", "failed_login_attempts"])

    def reset_failed_attempts(self):
        self.failed_login_attempts = 0
        self.locked_until = None
        self.save(update_fields=["failed_login_attempts", "locked_until"])

    def __str__(self):
        return self.phone


# =========================
# 🔢 OTP MODEL
# =========================

class OTP(models.Model):
    phone = models.CharField(max_length=10)
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["phone", "created_at"]),
        ]

    def is_expired(self):
        return timezone.now() > self.created_at + timedelta(minutes=5)

    @staticmethod
    def generate():
        return str(random.randint(100000, 999999))

    def __str__(self):
        return f"{self.phone} - {self.code}"


# =========================
# 🧾 KYC MODEL (APPLIED LATER)
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
        related_name="kycprofile"
    )

    passport_photo = models.ImageField(upload_to="kyc/passport/")
    id_front = models.ImageField(upload_to="kyc/id_front/")
    id_back = models.ImageField(upload_to="kyc/id_back/")

    # ⚠️ KYC DOES NOT BLOCK LOGIN
    status = models.CharField(
        max_length=20,
        choices=KYC_STATUS,
        default="not_submitted"
    )

    submitted_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"KYC - {self.user.phone} ({self.status})"
