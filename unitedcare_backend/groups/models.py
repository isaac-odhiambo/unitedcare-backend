# groups/models.py (COMPLETE UPDATED)
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Group(models.Model):
    name = models.CharField(max_length=120, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class GroupMembership(models.Model):
    ROLE = (
        ("MEMBER", "Member"),
        ("ADMIN", "Admin"),
    )

    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=ROLE, default="MEMBER")
    is_active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("group", "user")

    def __str__(self):
        return f"{self.group} - {self.user}"


# ==========================================================
# ✅ GROUP SAVINGS (NEW)
# ==========================================================

class GroupFund(models.Model):
    """
    The GROUP pooled wallet (group-owned money).
    """
    group = models.OneToOneField(Group, on_delete=models.CASCADE, related_name="fund")
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    reserved_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))  # optional future use
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(check=Q(balance__gte=0), name="groupfund_balance_non_negative"),
            models.CheckConstraint(check=Q(reserved_amount__gte=0), name="groupfund_reserved_non_negative"),
        ]

    @property
    def available_balance(self) -> Decimal:
        avail = (self.balance or Decimal("0.00")) - (self.reserved_amount or Decimal("0.00"))
        return avail if avail > Decimal("0.00") else Decimal("0.00")

    def clean(self):
        if self.reserved_amount and self.balance and self.reserved_amount > self.balance:
            raise ValidationError("Reserved amount cannot exceed balance.")

    def __str__(self):
        return f"GroupFund group={self.group_id} bal={self.balance}"


class GroupMemberShare(models.Model):
    """
    Per-member share inside a group (member contributions tracked).
    This is what you can lock as collateral for GROUP loans.
    """
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="member_shares")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="group_shares")

    total_contributed = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    reserved_share = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))  # locked for loans
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("group", "user")
        constraints = [
            models.CheckConstraint(check=Q(total_contributed__gte=0), name="groupshare_total_non_negative"),
            models.CheckConstraint(check=Q(reserved_share__gte=0), name="groupshare_reserved_non_negative"),
        ]

    @property
    def available_share(self) -> Decimal:
        avail = (self.total_contributed or Decimal("0.00")) - (self.reserved_share or Decimal("0.00"))
        return avail if avail > Decimal("0.00") else Decimal("0.00")

    def clean(self):
        if self.reserved_share and self.total_contributed and self.reserved_share > self.total_contributed:
            raise ValidationError("Reserved share cannot exceed total contributed.")

    def __str__(self):
        return f"Share g={self.group_id} u={self.user_id} total={self.total_contributed} reserved={self.reserved_share}"


class GroupContribution(models.Model):
    """
    Every contribution into group fund (audit trail).
    """
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="contributions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="group_contributions")

    amount = models.DecimalField(max_digits=14, decimal_places=2)
    reference = models.CharField(max_length=120, null=True, blank=True)  # mpesa receipt / internal ref
    note = models.CharField(max_length=255, null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["group", "user", "created_at"]),
            models.Index(fields=["reference"]),
        ]

    def clean(self):
        if self.amount is None or self.amount <= 0:
            raise ValidationError("Contribution amount must be greater than 0.")

    def __str__(self):
        return f"GroupContribution g={self.group_id} u={self.user_id} {self.amount}"


class GroupShareHold(models.Model):
    """
    Locks a member's group share as collateral for a loan in THIS group.
    We store loan_id as int to avoid cross-app FK complexities.
    """
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="share_holds")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="group_share_holds")

    loan_id = models.IntegerField(db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["group", "user", "loan_id", "is_active"]),
        ]

    def clean(self):
        if self.amount is None or self.amount <= 0:
            raise ValidationError("Hold amount must be greater than 0.")

    def release(self):
        if not self.is_active:
            return
        self.is_active = False
        self.released_at = timezone.now()
        self.save(update_fields=["is_active", "released_at"])

    def __str__(self):
        return f"GroupShareHold loan={self.loan_id} g={self.group_id} u={self.user_id} amt={self.amount} active={self.is_active}"