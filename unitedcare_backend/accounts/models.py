from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone
import random


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

    phone = models.CharField(max_length=15, unique=True)

    # ✅ OPTIONAL FOR ADMINS, REQUIRED FOR MEMBERS (ENFORCED IN SERIALIZER)
    id_number = models.CharField(
        max_length=20,
        unique=True,
        null=True,
        blank=True
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

    USERNAME_FIELD = "phone"
    REQUIRED_FIELDS = ["username"]

    def __str__(self):
        return self.phone


class OTP(models.Model):
    phone = models.CharField(max_length=15)
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    is_used = models.BooleanField(default=False)

    def is_expired(self):
        return timezone.now() > self.created_at + timezone.timedelta(minutes=5)

    @staticmethod
    def generate():
        return str(random.randint(100000, 999999))


class KYCProfile(models.Model):
    KYC_STATUS = (
        ("not_submitted", "Not Submitted"),
        ("submitted", "Submitted"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    )

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    passport_photo = models.ImageField(upload_to="kyc/passport/")
    id_front = models.ImageField(upload_to="kyc/id_front/")
    id_back = models.ImageField(upload_to="kyc/id_back/")
    status = models.CharField(
        max_length=20,
        choices=KYC_STATUS,
        default="not_submitted"
    )
    submitted_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"KYC - {self.user.phone}"
