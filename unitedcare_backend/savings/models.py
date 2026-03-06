from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


class SavingsAccount(models.Model):
    """
    PERSONAL savings accounts (not tied to group/merry).
    """

    ACCOUNT_TYPE = (
        ("FLEXIBLE", "Flexible Savings"),
        ("FIXED", "Fixed Savings"),
        ("TARGET", "Target-Based Savings"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="savings_accounts",
    )

    name = models.CharField(max_length=120)
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPE)

    balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))

    # Locked/reserved funds due to loans/guarantees
    reserved_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))

    locked_until = models.DateField(null=True, blank=True)  # FIXED
    target_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)  # TARGET
    target_deadline = models.DateField(null=True, blank=True)  # TARGET

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["user_id", "name"]
        constraints = [
            models.CheckConstraint(check=Q(balance__gte=0), name="savings_balance_non_negative"),
            models.CheckConstraint(check=Q(reserved_amount__gte=0), name="savings_reserved_non_negative"),
        ]

    @property
    def available_balance(self) -> Decimal:
        avail = (self.balance or Decimal("0.00")) - (self.reserved_amount or Decimal("0.00"))
        return avail if avail > Decimal("0.00") else Decimal("0.00")

    def can_withdraw_now(self) -> bool:
        if not self.is_active:
            return False
        if self.account_type == "FIXED" and self.locked_until:
            return timezone.now().date() >= self.locked_until
        return True

    def clean(self):
        if self.balance is not None and self.balance < 0:
            raise ValidationError("Balance cannot be negative.")
        if self.reserved_amount is not None and self.reserved_amount < 0:
            raise ValidationError("Reserved amount cannot be negative.")
        if (
            self.reserved_amount is not None
            and self.balance is not None
            and self.reserved_amount > self.balance
        ):
            raise ValidationError("Reserved amount cannot exceed balance.")

    def __str__(self):
        return f"{self.user} - {self.name} ({self.account_type})"


class SavingsTransaction(models.Model):
    """
    Personal savings history.
    Deposits/Withdrawals are recorded here.
    """

    TXN_TYPE = (
        ("DEPOSIT", "Deposit"),
        ("WITHDRAWAL", "Withdrawal"),
        ("ADJUSTMENT", "Adjustment"),
        ("AUTO_DEDUCT", "Auto Deduction"),
    )

    account = models.ForeignKey(SavingsAccount, on_delete=models.CASCADE, related_name="transactions")
    txn_type = models.CharField(max_length=20, choices=TXN_TYPE)
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    # idempotency / audit ref: MPESA_TX#<id> or WD#<id>
    reference = models.CharField(max_length=120, null=True, blank=True)
    note = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["account", "txn_type", "created_at"]),
            models.Index(fields=["reference"]),
        ]

    def clean(self):
        if self.amount is None or self.amount <= 0:
            raise ValidationError("Transaction amount must be greater than 0.")

    def __str__(self):
        return f"{self.account_id} {self.txn_type} {self.amount}"