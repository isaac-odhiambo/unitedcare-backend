from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Sum
from django.utils import timezone


# ==========================================================
# Global Credit Profile (per borrower, not per context)
# ==========================================================
class MemberCreditProfile(models.Model):
    """
    Global credit profile for a borrower across the platform.

    Context is no longer the identity of the loan.
    Merry / Group are treated as optional sources of support/security,
    not as the loan's parent.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="credit_profile",
    )

    score = models.IntegerField(default=100)
    total_loans = models.IntegerField(default=0)
    loans_completed = models.IntegerField(default=0)
    loans_defaulted = models.IntegerField(default=0)
    late_payments = models.IntegerField(default=0)

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user} score={self.score}"


# ==========================================================
# Loan Product
# ==========================================================
class LoanProduct(models.Model):
    """
    Loan product configuration.
    Admin may assign/select this internally.
    Member does not need to see product details at request stage.
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
    interest_type = models.CharField(
        max_length=20,
        choices=INTEREST_TYPE,
        default="FLAT",
    )
    annual_interest_rate = models.DecimalField(max_digits=6, decimal_places=2)

    repayment_frequency = models.CharField(
        max_length=10,
        choices=REPAYMENT_FREQUENCY,
        default="WEEKLY",
    )
    repayment_weekday = models.IntegerField(default=0)  # Monday=0 .. Sunday=6

    max_weeks = models.PositiveIntegerField(default=12)

    late_fee_rate_weekly = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("2.00"),
        help_text="Percentage charged weekly on overdue installment total_due (e.g. 2.00 = 2%)",
    )

    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(
        default=False,
        help_text="Used when member requests a loan without choosing a product.",
    )

    def clean(self):
        if self.is_default:
            qs = LoanProduct.objects.filter(is_default=True).exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError("Only one loan product can be marked as default.")

    def __str__(self):
        return f"{self.name} ({self.interest_type}, {self.repayment_frequency})"


# ==========================================================
# Loan
# ==========================================================
class Loan(models.Model):
    """
    Global member loan.

    Important:
    - Loan is NOT owned by a Merry or Group.
    - Security may come from multiple sources.
    - Contexts are only used as security sources / relationship sources.
    """

    STATUS = (
        ("PENDING", "Pending"),
        ("UNDER_REVIEW", "Under Review"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
        ("COMPLETED", "Completed"),
        ("DEFAULTED", "Defaulted"),
        ("CANCELLED", "Cancelled"),
    )

    borrower = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="loans",
    )
    product = models.ForeignKey(
        LoanProduct,
        on_delete=models.PROTECT,
        related_name="loans",
    )

    principal = models.DecimalField(max_digits=12, decimal_places=2)
    term_weeks = models.PositiveIntegerField(default=12)

    status = models.CharField(max_length=20, choices=STATUS, default="PENDING")
    is_defaulter = models.BooleanField(default=False)

    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    total_payable = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total_paid = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    outstanding_balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    # Security summary values
    security_target = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    security_reserved_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    # Audit / review notes
    member_note = models.TextField(blank=True, default="")
    admin_note = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-id"]

    def clean(self):
        if self.principal is None or Decimal(self.principal) <= 0:
            raise ValidationError("Principal must be greater than 0.")

        if self.term_weeks <= 0:
            raise ValidationError("term_weeks must be greater than 0.")

        if self.product and self.product.repayment_frequency != "WEEKLY":
            raise ValidationError("This system currently supports WEEKLY repayment only.")

        if self.product and self.term_weeks > self.product.max_weeks:
            raise ValidationError("Loan term exceeds product max weeks.")

    def __str__(self):
        return f"Loan#{self.id} borrower={self.borrower_id} status={self.status}"

    @property
    def is_active(self):
        return self.status in ("PENDING", "UNDER_REVIEW", "APPROVED")

    def recompute_balances(self):
        self.outstanding_balance = Decimal(self.total_payable or Decimal("0.00")) - Decimal(
            self.total_paid or Decimal("0.00")
        )

        if self.outstanding_balance <= Decimal("0.00"):
            self.outstanding_balance = Decimal("0.00")
            if self.status == "APPROVED":
                self.status = "COMPLETED"
                self.completed_at = timezone.now()

    def recompute_reserved_security_total(self):
        total = (
            self.security_allocations.filter(is_active=True)
            .aggregate(total=Sum("amount"))
            .get("total")
            or Decimal("0.00")
        )
        self.security_reserved_total = total
        return total


# ==========================================================
# Loan Guarantor
# ==========================================================
class LoanGuarantor(models.Model):
    """
    Platform-level guarantor.

    Global approach:
    - Guarantor is no longer restricted to the same Merry or Group.
    - Validation of whether this guarantor is acceptable is done in services.
    - Model still blocks obviously invalid cases (like self-guarantee).
    """

    loan = models.ForeignKey(
        Loan,
        on_delete=models.CASCADE,
        related_name="guarantors",
    )
    guarantor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="guarantees",
    )

    accepted = models.BooleanField(default=False)
    accepted_at = models.DateTimeField(null=True, blank=True)

    # How much of this guarantor's support/security is currently reserved
    reserved_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    # Optional audit fields
    request_note = models.CharField(max_length=255, blank=True, default="")
    admin_note = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("loan", "guarantor")
        ordering = ["id"]

    def clean(self):
        if self.loan_id and self.loan.borrower_id == self.guarantor_id:
            raise ValidationError("Borrower cannot guarantee their own loan.")

    def __str__(self):
        return (
            f"Loan#{self.loan_id} guarantor={self.guarantor_id} "
            f"accepted={self.accepted} reserved={self.reserved_amount}"
        )


# ==========================================================
# Loan Security Allocation
# ==========================================================
class LoanSecurityAllocation(models.Model):
    """
    Flexible security/collateral registry for a global loan.

    This replaces hardcoded loan-level fields like:
    - borrower_reserved_savings
    - borrower_reserved_merry_credit
    and also replaces context-bound holding logic.

    Examples:
    - BORROWER_SAVINGS
    - BORROWER_GROUP_SHARE
    - BORROWER_MERRY_CREDIT
    - GUARANTOR_SAVINGS
    - GUARANTOR_GROUP_SHARE
    - GUARANTOR_MERRY_CREDIT
    """

    SOURCE_TYPE = (
        ("BORROWER_SAVINGS", "Borrower Savings"),
        ("BORROWER_GROUP_SHARE", "Borrower Group Share"),
        ("BORROWER_MERRY_CREDIT", "Borrower Merry Credit"),
        ("GUARANTOR_SAVINGS", "Guarantor Savings"),
        ("GUARANTOR_GROUP_SHARE", "Guarantor Group Share"),
        ("GUARANTOR_MERRY_CREDIT", "Guarantor Merry Credit"),
    )

    loan = models.ForeignKey(
        Loan,
        on_delete=models.CASCADE,
        related_name="security_allocations",
    )

    source_type = models.CharField(max_length=40, choices=SOURCE_TYPE)

    # Whose resource is this?
    owner_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="loan_security_allocations",
    )

    # Optional references for traceability
    guarantor_link = models.ForeignKey(
        LoanGuarantor,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="security_allocations",
        help_text="Set when this allocation is tied to a guarantor on this loan.",
    )

    savings_account = models.ForeignKey(
        "savings.SavingsAccount",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="loan_security_allocations",
    )

    merry = models.ForeignKey(
        "merry.MerryGoRound",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="loan_security_allocations",
    )

    group = models.ForeignKey(
        "groups.Group",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="loan_security_allocations",
    )

    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]

    def clean(self):
        if Decimal(self.amount or Decimal("0.00")) <= 0:
            raise ValidationError("Security allocation amount must be greater than 0.")

        if self.guarantor_link_id:
            if self.guarantor_link.loan_id != self.loan_id:
                raise ValidationError("guarantor_link must belong to the same loan.")

            if self.owner_user_id != self.guarantor_link.guarantor_id:
                raise ValidationError("owner_user must match the guarantor linked to this allocation.")

        if self.source_type.startswith("BORROWER_"):
            if self.owner_user_id != self.loan.borrower_id:
                raise ValidationError("Borrower allocation must belong to the borrower.")

        if self.source_type.startswith("GUARANTOR_"):
            if not self.guarantor_link_id:
                raise ValidationError("Guarantor allocation must be linked to a LoanGuarantor record.")

        if self.source_type.endswith("SAVINGS") and not self.savings_account_id:
            raise ValidationError("Savings-based security allocation requires a savings account reference.")

        if self.source_type.endswith("MERRY_CREDIT") and not self.merry_id:
            raise ValidationError("Merry-credit security allocation requires a merry reference.")

        if self.source_type.endswith("GROUP_SHARE") and not self.group_id:
            raise ValidationError("Group-share security allocation requires a group reference.")

    def release(self):
        self.is_active = False
        self.released_at = timezone.now()
        self.save(update_fields=["is_active", "released_at"])

    def __str__(self):
        return (
            f"Loan#{self.loan_id} {self.source_type} "
            f"owner={self.owner_user_id} amt={self.amount} active={self.is_active}"
        )


# ==========================================================
# Loan Installment
# ==========================================================
class LoanInstallment(models.Model):
    """
    Weekly repayment schedule.
    Due dates are generated in services.py.
    """

    loan = models.ForeignKey(
        Loan,
        on_delete=models.CASCADE,
        related_name="installments",
    )

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

    loan = models.ForeignKey(
        Loan,
        on_delete=models.CASCADE,
        related_name="payments",
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_at = models.DateTimeField(auto_now_add=True)

    method = models.CharField(max_length=50, default="MANUAL")
    reference = models.CharField(max_length=120, null=True, blank=True)

    def clean(self):
        if Decimal(self.amount or Decimal("0.00")) <= 0:
            raise ValidationError("Payment amount must be greater than 0.")

    def __str__(self):
        return f"Loan#{self.loan_id} paid {self.amount} ({self.method})"