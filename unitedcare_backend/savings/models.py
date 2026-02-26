from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class SavingsAccount(models.Model):
    """
    PERSONAL (individual) savings accounts.

    A user can have multiple accounts:
    - FLEXIBLE (main wallet)
    - FIXED (locked until a date)
    - TARGET (goal-based)

    Savings is NOT tied to Merry or Group.
    Loans will still be tied to Merry/Group, but eligibility will use personal savings.

    We add:
    - reserved_amount: locked amount due to loans/guarantees
    - available_balance: balance - reserved_amount (never below 0)
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

    name = models.CharField(max_length=120)  # e.g. "Main Wallet", "Fixed 6 months", "December Trip Fund"
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPE)

    balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))

    # Locked/reserved funds due to active loan or being a guarantor
    reserved_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))

    # Fixed/Target controls
    locked_until = models.DateField(null=True, blank=True)  # for FIXED
    target_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)  # for TARGET
    target_deadline = models.DateField(null=True, blank=True)  # for TARGET (policy optional)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["user_id", "name"]
        constraints = [
            models.CheckConstraint(
                check=models.Q(balance__gte=0),
                name="savings_balance_non_negative",
            ),
            models.CheckConstraint(
                check=models.Q(reserved_amount__gte=0),
                name="savings_reserved_non_negative",
            ),
        ]

    @property
    def available_balance(self) -> Decimal:
        avail = (self.balance or Decimal("0.00")) - (self.reserved_amount or Decimal("0.00"))
        return avail if avail > Decimal("0.00") else Decimal("0.00")

    def can_withdraw_now(self) -> bool:
        """
        Basic account-type rules:
        - FIXED: cannot withdraw before locked_until
        - TARGET: allow by default (you can tighten policy in services)
        - FLEXIBLE: allow
        """
        if not self.is_active:
            return False

        if self.account_type == "FIXED" and self.locked_until:
            return timezone.now().date() >= self.locked_until

        return True

    def clean(self):
        # Basic consistency checks
        if self.account_type == "FIXED" and not self.locked_until:
            # Optional: enforce lock date for FIXED
            pass

        if self.balance is not None and self.balance < 0:
            raise ValidationError("Balance cannot be negative.")

        if self.reserved_amount is not None and self.reserved_amount < 0:
            raise ValidationError("Reserved amount cannot be negative.")

        if self.reserved_amount is not None and self.balance is not None and self.reserved_amount > self.balance:
            raise ValidationError("Reserved amount cannot exceed balance.")

    def __str__(self):
        return f"{self.user} - {self.name} ({self.account_type})"


class SavingsTransaction(models.Model):
    """
    Contribution tracking + history.
    Every deposit/withdrawal is logged here.
    """
    TXN_TYPE = (
        ("DEPOSIT", "Deposit"),
        ("WITHDRAWAL", "Withdrawal"),
        ("ADJUSTMENT", "Adjustment"),   # admin correction
        ("AUTO_DEDUCT", "Auto Deduction"),  # used by loan module
    )

    account = models.ForeignKey(SavingsAccount, on_delete=models.CASCADE, related_name="transactions")
    txn_type = models.CharField(max_length=20, choices=TXN_TYPE)
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    reference = models.CharField(max_length=120, null=True, blank=True)  # MPESA receipt, internal ref, etc.
    note = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["account", "txn_type", "created_at"]),
        ]

    def clean(self):
        if self.amount is None or self.amount <= 0:
            raise ValidationError("Transaction amount must be greater than 0.")

    def __str__(self):
        return f"{self.account_id} {self.txn_type} {self.amount}"


class WithdrawRequest(models.Model):
    """
    Withdraw workflow with admin approval.

    Process:
    - Member creates request (PENDING)
    - Admin approves/rejects
    - When PAID: system deducts from account + creates WITHDRAWAL transaction
    """
    STATUS = (
        ("PENDING", "Pending"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
        ("PAID", "Paid"),
    )

    account = models.ForeignKey(SavingsAccount, on_delete=models.CASCADE, related_name="withdraw_requests")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="withdraw_requests",
    )

    amount = models.DecimalField(max_digits=14, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS, default="PENDING")

    reason = models.CharField(max_length=255, null=True, blank=True)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_withdrawals",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
        ]

    def clean(self):
        if self.amount is None or self.amount <= 0:
            raise ValidationError("Withdrawal amount must be greater than 0.")

        if self.requested_by_id and self.account_id and self.requested_by_id != self.account.user_id:
            raise ValidationError("You can only withdraw from your own savings account.")

        if not self.account.is_active:
            raise ValidationError("Savings account is not active.")

        if not self.account.can_withdraw_now():
            raise ValidationError("This savings account is currently locked.")

        if self.amount > self.account.available_balance:
            raise ValidationError("Insufficient available balance (some funds may be reserved).")

    def __str__(self):
        return f"Withdraw#{self.id} {self.status} {self.amount}"