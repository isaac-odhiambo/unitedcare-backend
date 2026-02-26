# loans/models.py (COMPLETE UPDATED VERSION)
# ----------------------------------------
# This version supports:
# ✅ Loan context = exactly one (Merry OR Group)
# ✅ Multiple guarantors
# ✅ Coverage-based security reserve (borrower savings + merry credit + guarantors)
# ✅ Accurate release using stored reserved_amount fields
# ✅ Merry credit holds using your Merry Contribution/Payout tables

from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


# ==========================================================
# Credit Profile (per context)
# ==========================================================
class MemberCreditProfile(models.Model):
    """
    Credit score tracked per context:
    - Either within a Merry, OR within a Group.
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    merry = models.ForeignKey("merry.MerryGoRound", null=True, blank=True, on_delete=models.CASCADE)
    group = models.ForeignKey("groups.Group", null=True, blank=True, on_delete=models.CASCADE)

    score = models.IntegerField(default=100)
    total_loans = models.IntegerField(default=0)
    loans_completed = models.IntegerField(default=0)
    loans_defaulted = models.IntegerField(default=0)
    late_payments = models.IntegerField(default=0)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    (models.Q(merry__isnull=False) & models.Q(group__isnull=True))
                    | (models.Q(merry__isnull=True) & models.Q(group__isnull=False))
                ),
                name="credit_profile_exactly_one_context",
            ),
            models.UniqueConstraint(
                fields=["user", "merry"],
                condition=models.Q(merry__isnull=False),
                name="uniq_credit_profile_user_merry_when_set",
            ),
            models.UniqueConstraint(
                fields=["user", "group"],
                condition=models.Q(group__isnull=False),
                name="uniq_credit_profile_user_group_when_set",
            ),
        ]

    def __str__(self):
        ctx = f"Merry:{self.merry_id}" if self.merry_id else f"Group:{self.group_id}"
        return f"{self.user} {ctx} score={self.score}"


# ==========================================================
# Loan Product
# ==========================================================
class LoanProduct(models.Model):
    """
    Loan product configuration.
    We default to WEEKLY repayment (every Monday).
    """
    INTEREST_TYPE = (
        ("FLAT", "Flat"),
        ("REDUCING", "Reducing Balance"),
    )

    REPAYMENT_FREQUENCY = (
        ("WEEKLY", "Weekly"),
        ("MONTHLY", "Monthly"),
    )

    name = models.CharField(max_length=100)
    interest_type = models.CharField(max_length=20, choices=INTEREST_TYPE, default="FLAT")
    annual_interest_rate = models.DecimalField(max_digits=6, decimal_places=2)

    repayment_frequency = models.CharField(max_length=10, choices=REPAYMENT_FREQUENCY, default="WEEKLY")
    repayment_weekday = models.IntegerField(default=0)  # Monday=0 .. Sunday=6

    max_weeks = models.PositiveIntegerField(default=12)

    late_fee_rate_weekly = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("2.00"),
        help_text="Percentage charged weekly on overdue installment total_due (e.g., 2.00 = 2%)",
    )

    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} ({self.interest_type}, {self.repayment_frequency})"


# ==========================================================
# Loan
# ==========================================================
class Loan(models.Model):
    """
    Loan belongs to exactly one context: Merry OR Group.
    Borrower is a User; membership/eligibility is enforced in services/views.
    """

    STATUS = (
        ("PENDING", "Pending"),
        ("UNDER_REVIEW", "Under Review"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
        ("COMPLETED", "Completed"),
        ("DEFAULTED", "Defaulted"),
    )

    merry = models.ForeignKey(
        "merry.MerryGoRound", null=True, blank=True, on_delete=models.CASCADE, related_name="loans"
    )
    group = models.ForeignKey(
        "groups.Group", null=True, blank=True, on_delete=models.CASCADE, related_name="loans"
    )

    borrower = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="loans")
    product = models.ForeignKey(LoanProduct, on_delete=models.PROTECT)

    principal = models.DecimalField(max_digits=12, decimal_places=2)
    term_weeks = models.PositiveIntegerField(default=12)

    status = models.CharField(max_length=20, choices=STATUS, default="PENDING")
    is_defaulter = models.BooleanField(default=False)

    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    total_payable = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total_paid = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    outstanding_balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    # -----------------------------
    # ✅ NEW SECURITY FIELDS
    # -----------------------------
    borrower_reserved_savings = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    borrower_reserved_merry_credit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    security_target = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    (models.Q(merry__isnull=False) & models.Q(group__isnull=True))
                    | (models.Q(merry__isnull=True) & models.Q(group__isnull=False))
                ),
                name="loan_exactly_one_context",
            ),
        ]

    def clean(self):
        if bool(self.merry) == bool(self.group):
            raise ValidationError("Loan must belong to either a Merry OR a Group (not both).")

        if self.product and self.product.repayment_frequency != "WEEKLY":
            raise ValidationError("This system currently supports WEEKLY repayment only.")

        if self.term_weeks <= 0:
            raise ValidationError("term_weeks must be greater than 0.")

        if self.product and self.term_weeks > self.product.max_weeks:
            raise ValidationError("Loan term exceeds product max weeks.")

    def __str__(self):
        ctx = f"Merry:{self.merry_id}" if self.merry_id else f"Group:{self.group_id}"
        return f"Loan#{self.id} {ctx} {self.borrower} {self.status}"

    @property
    def is_active(self):
        return self.status in ("PENDING", "UNDER_REVIEW", "APPROVED")

    def recompute_balances(self):
        self.outstanding_balance = (self.total_payable - self.total_paid)
        if self.outstanding_balance <= Decimal("0.00") and self.status == "APPROVED":
            self.status = "COMPLETED"
            self.outstanding_balance = Decimal("0.00")


# ==========================================================
# ✅ Merry Credit Hold (NEW)
# ==========================================================
class MerryCreditHold(models.Model):
    """
    Holds 'unpaid-out' merry credit as loan collateral.

    When a borrower uses merry credit as security:
      - we create a hold record linked to the loan
      - payout confirmation in merry should check active holds and block payout
    """
    loan = models.OneToOneField(Loan, on_delete=models.CASCADE, related_name="merry_hold")
    merry = models.ForeignKey("merry.MerryGoRound", on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)

    def release(self):
        self.is_active = False
        self.released_at = timezone.now()
        self.save(update_fields=["is_active", "released_at"])

    def __str__(self):
        return f"MerryHold loan={self.loan_id} user={self.user_id} merry={self.merry_id} amt={self.amount} active={self.is_active}"


# ==========================================================
# Loan Guarantor
# ==========================================================
class LoanGuarantor(models.Model):
    """
    Guarantor rules:
    - Must be a member of the same context (same Merry or same Group)
    - Must accept to become guarantor
    - One guarantor can guarantee ONLY ONE active loan at a time per context

    ✅ NEW:
    - reserved_amount: exactly how much was locked from this guarantor savings
    """
    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="guarantors")
    guarantor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="guarantees")

    accepted = models.BooleanField(default=False)
    accepted_at = models.DateTimeField(null=True, blank=True)

    # -----------------------------
    # ✅ NEW SECURITY FIELD
    # -----------------------------
    reserved_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        unique_together = ("loan", "guarantor")

    def clean(self):
        if self.loan_id is None:
            return

        if self.loan.borrower_id == self.guarantor_id:
            raise ValidationError("Borrower cannot guarantee their own loan.")

        from merry.models import Member as MerryMember
        from groups.models import GroupMembership

        if self.loan.merry_id:
            if not MerryMember.objects.filter(merry_id=self.loan.merry_id, user_id=self.guarantor_id).exists():
                raise ValidationError("Guarantor must be a member of this Merry.")
        else:
            if not GroupMembership.objects.filter(
                group_id=self.loan.group_id, user_id=self.guarantor_id, is_active=True
            ).exists():
                raise ValidationError("Guarantor must be an active member of this Group.")

        active = LoanGuarantor.objects.filter(
            guarantor_id=self.guarantor_id,
            accepted=True,
            loan__status="APPROVED",
            loan__outstanding_balance__gt=0,
        ).exclude(loan_id=self.loan_id)

        if self.loan.merry_id:
            active = active.filter(loan__merry_id=self.loan.merry_id)
        else:
            active = active.filter(loan__group_id=self.loan.group_id)

        if active.exists():
            raise ValidationError("Guarantor can only guarantee one active loan at a time in this context.")

    def __str__(self):
        return f"Loan#{self.loan_id} guarantor={self.guarantor} accepted={self.accepted} reserved={self.reserved_amount}"


# ==========================================================
# Loan Installment
# ==========================================================
class LoanInstallment(models.Model):
    """
    Weekly repayment schedule.
    Due dates generated in services.py.
    """
    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="installments")

    installment_no = models.PositiveIntegerField()
    due_date = models.DateField()

    principal_due = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    interest_due = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total_due = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    late_fee = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    is_paid = models.BooleanField(default=False)

    class Meta:
        unique_together = ("loan", "installment_no")
        ordering = ["installment_no"]

    def __str__(self):
        return f"Loan#{self.loan_id} week#{self.installment_no} due={self.due_date}"


# ==========================================================
# Loan Payment
# ==========================================================
class LoanPayment(models.Model):
    """
    Partial repayment supported.
    Allocation to installments is handled in services.py.
    """
    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_at = models.DateTimeField(auto_now_add=True)

    method = models.CharField(max_length=50, default="MANUAL")
    reference = models.CharField(max_length=120, null=True, blank=True)

    def __str__(self):
        return f"Loan#{self.loan_id} paid {self.amount} ({self.method})"