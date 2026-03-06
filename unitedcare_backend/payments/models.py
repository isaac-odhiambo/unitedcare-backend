# payments/models.py
from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


# =========================
# Validators
# =========================
phone_validator = RegexValidator(
    regex=r"^(07|01)\d{8}$",
    message="Phone number must be a valid Kenyan number (07XXXXXXXX or 01XXXXXXXX).",
)


# =========================
# Fee Configuration
# =========================
class TransactionFeeConfig(models.Model):
    """
    Central source of truth for transaction fees.

    Examples:
    - SAVINGS_DEPOSIT
    - MERRY_CONTRIBUTION
    - GROUP_CONTRIBUTION
    - LOAN_REPAYMENT
    - WITHDRAWAL

    Fee formula:
      total_fee = fixed_fee + (percentage_fee % of base_amount)
    """

    PURPOSE_CHOICES = (
        ("SAVINGS_DEPOSIT", "Savings Deposit"),
        ("MERRY_CONTRIBUTION", "Merry Contribution"),
        ("GROUP_CONTRIBUTION", "Group Contribution"),
        ("LOAN_REPAYMENT", "Loan Repayment"),
        ("WITHDRAWAL", "Withdrawal"),
        ("OTHER", "Other"),
    )

    purpose = models.CharField(
        max_length=40,
        choices=PURPOSE_CHOICES,
        unique=True,
        db_index=True,
    )
    fixed_fee = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    percentage_fee = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Percentage applied on base amount, e.g. 2.50 means 2.5%",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["purpose"]
        verbose_name = "Transaction Fee Config"
        verbose_name_plural = "Transaction Fee Configs"

    def __str__(self):
        return f"{self.purpose} | fixed={self.fixed_fee} | pct={self.percentage_fee}%"


# =========================
# MpesaTransaction (STK + B2C ONLY)
# =========================
class MpesaTransaction(models.Model):
    """
    Source-of-truth table for Mpesa:
    - STK Push (customer pays you): direction=IN, channel=STK
    - B2C payout (you pay customer): direction=OUT, channel=B2C

    Amount design:
    - amount = final transaction amount actually used in Mpesa call
      * STK: total charged to customer
      * B2C: actual payout sent to customer
    - base_amount = business/base amount before fee
    - transaction_fee = fee portion applied by backend
    """

    DIRECTION_CHOICES = (("IN", "Money In"), ("OUT", "Money Out"))
    CHANNEL_CHOICES = (("STK", "STK Push"), ("B2C", "B2C Payout"))
    STATUS_CHOICES = (
        ("INITIATED", "Initiated"),
        ("PENDING", "Pending"),
        ("SUCCESS", "Success"),
        ("FAILED", "Failed"),
        ("CANCELLED", "Cancelled"),
        ("TIMEOUT", "Timeout"),
    )
    PURPOSE_CHOICES = (
        ("SAVINGS_DEPOSIT", "Savings Deposit"),
        ("MERRY_CONTRIBUTION", "Merry Contribution"),
        ("GROUP_CONTRIBUTION", "Group Contribution"),
        ("LOAN_REPAYMENT", "Loan Repayment"),
        ("WITHDRAWAL", "Withdrawal"),
        ("LOAN_DISBURSEMENT", "Loan Disbursement"),
        ("OTHER", "Other"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="mpesa_transactions",
        db_index=True,
        help_text="Owner/user related to this transaction (if known).",
    )

    phone = models.CharField(max_length=20, validators=[phone_validator], db_index=True)

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Final amount used for the actual Mpesa transaction.",
    )

    base_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Original business/base amount before fee.",
    )

    transaction_fee = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Fee portion charged or deducted by backend.",
    )

    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES, default="IN")
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, default="STK")
    purpose = models.CharField(max_length=30, choices=PURPOSE_CHOICES, default="OTHER")
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="INITIATED",
        db_index=True,
    )

    # Business/internal reference (optional)
    reference = models.CharField(max_length=120, blank=True, default="", db_index=True)

    # --- STK identifiers ---
    merchant_request_id = models.CharField(max_length=120, null=True, blank=True)
    checkout_request_id = models.CharField(
        max_length=120,
        null=True,
        blank=True,
        db_index=True,
        help_text="STK unique ID returned by Safaricom (idempotency key).",
    )

    # --- B2C identifiers ---
    conversation_id = models.CharField(
        max_length=120,
        null=True,
        blank=True,
        db_index=True,
        help_text="B2C unique conversation id returned by Safaricom (idempotency key).",
    )
    originator_conversation_id = models.CharField(max_length=120, null=True, blank=True)

    # --- Result fields (callback) ---
    result_code = models.CharField(max_length=20, null=True, blank=True)
    result_desc = models.CharField(max_length=255, null=True, blank=True)

    mpesa_receipt_number = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
        help_text="Receipt number for successful transactions (strongest uniqueness).",
    )
    transaction_date = models.DateTimeField(null=True, blank=True)

    # Raw payloads (audit/debug)
    request_payload = models.JSONField(null=True, blank=True)
    callback_payload = models.JSONField(null=True, blank=True)

    # Link to "what this payment was for" (any model)
    target_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    target_object_id = models.PositiveIntegerField(null=True, blank=True)
    target_object = GenericForeignKey("target_content_type", "target_object_id")

    # Idempotency for posting ledger
    ledger_posted = models.BooleanField(default=False, db_index=True)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["status", "channel", "direction"]),
            models.Index(fields=["phone", "created_at"]),
            models.Index(fields=["purpose", "created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["checkout_request_id"],
                name="uniq_checkout_request_id",
                condition=Q(checkout_request_id__isnull=False) & ~Q(checkout_request_id=""),
            ),
            models.UniqueConstraint(
                fields=["mpesa_receipt_number"],
                name="uniq_mpesa_receipt_number",
                condition=Q(mpesa_receipt_number__isnull=False) & ~Q(mpesa_receipt_number=""),
            ),
            models.UniqueConstraint(
                fields=["conversation_id"],
                name="uniq_conversation_id",
                condition=Q(conversation_id__isnull=False) & ~Q(conversation_id=""),
            ),
        ]

    def __str__(self):
        return (
            f"MpesaTx#{self.id} {self.channel} {self.direction} "
            f"amount={self.amount} base={self.base_amount} fee={self.transaction_fee} {self.status}"
        )


# =========================
# Ledger / History
# =========================
class PaymentLedger(models.Model):
    """
    UI-friendly money history.
    Allows multiple ledger lines per MpesaTransaction
    (e.g. withdrawal + withdrawal fee).
    """

    ENTRY_CHOICES = (("CREDIT", "Credit (Money In)"), ("DEBIT", "Debit (Money Out)"))

    CATEGORY_CHOICES = (
        ("SAVINGS", "Savings"),
        ("LOANS", "Loans"),
        ("MERRY", "Merry-go-round"),
        ("GROUP", "Group"),
        ("WITHDRAWAL", "Withdrawal"),
        ("WITHDRAWAL_FEE", "Withdrawal Fee"),
        ("TRANSACTION_FEE", "Transaction Fee"),
        ("OTHER", "Other"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ledger_entries",
        db_index=True,
    )
    entry_type = models.CharField(max_length=10, choices=ENTRY_CHOICES, db_index=True)
    category = models.CharField(
        max_length=20,
        choices=CATEGORY_CHOICES,
        default="OTHER",
        db_index=True,
    )

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )

    narration = models.CharField(max_length=255, blank=True, default="")
    reference = models.CharField(max_length=120, blank=True, default="", db_index=True)

    mpesa_tx = models.ForeignKey(
        MpesaTransaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ledger_entries",
        db_index=True,
    )

    target_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    target_object_id = models.PositiveIntegerField(null=True, blank=True)
    target_object = GenericForeignKey("target_content_type", "target_object_id")

    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["category", "created_at"]),
            models.Index(fields=["reference"]),
        ]

    def __str__(self):
        return f"Ledger#{self.id} user={self.user_id} {self.entry_type} {self.amount}"


# =========================
# WithdrawalRequest
# =========================
class WithdrawalRequest(models.Model):
    """
    Workflow:
    1) member creates request -> PENDING
    2) admin approves -> APPROVED
    3) system triggers B2C payout -> PROCESSING
    4) callback -> PAID or FAILED
    """

    STATUS_CHOICES = ("PENDING", "APPROVED", "REJECTED", "PROCESSING", "PAID", "FAILED", "CANCELLED")
    SOURCE_CHOICES = ("SAVINGS", "MERRY", "GROUP", "OTHER")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="withdrawal_requests",
        db_index=True,
    )

    phone = models.CharField(max_length=20, validators=[phone_validator], db_index=True)
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Requested/base payout amount before fee deduction.",
    )

    source = models.CharField(
        max_length=20,
        choices=[(c, c) for c in SOURCE_CHOICES],
        default="SAVINGS",
        db_index=True,
    )

    target_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    target_object_id = models.PositiveIntegerField(null=True, blank=True)
    target_object = GenericForeignKey("target_content_type", "target_object_id")

    status = models.CharField(
        max_length=20,
        choices=[(s, s) for s in STATUS_CHOICES],
        default="PENDING",
        db_index=True,
    )

    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_withdrawals",
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rejected_withdrawals",
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=255, null=True, blank=True)

    mpesa_tx = models.OneToOneField(
        MpesaTransaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="withdrawal_request",
    )

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["user", "created_at"]),
        ]

    def __str__(self):
        return f"WD#{self.id} user={self.user_id} {self.amount} {self.status}"

    @property
    def is_final(self) -> bool:
        return self.status in ("PAID", "REJECTED", "CANCELLED")

    @property
    def can_withdraw_merry(self) -> bool:
        """
        If source is MERRY and target_object has next_payout_date, enforce it.
        Otherwise allow.
        """
        if self.source != "MERRY" or not self.target_object:
            return True

        next_payout_date = getattr(self.target_object, "next_payout_date", None)
        if not next_payout_date:
            return True

        return timezone.now().date() >= next_payout_date