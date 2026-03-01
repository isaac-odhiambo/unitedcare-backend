# payments/models.py
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType


# =========================
# Validators
# =========================
phone_validator = RegexValidator(
    regex=r"^(07|01)\d{8}$",
    message="Phone number must be a valid Kenyan number (07XXXXXXXX or 01XXXXXXXX).",
)


# =========================
# Mpesa Transaction
# =========================
class MpesaTransaction(models.Model):
    """
    Stores ALL Mpesa transactions in one place:
    - STK Push (customer pays you) -> direction=IN, channel=STK
    - B2C (you pay customer)       -> direction=OUT, channel=B2C

    This is the 'source of truth' for Mpesa references, callbacks and audit.
    """

    # Money direction
    DIRECTION_CHOICES = (
        ("IN", "Money In"),
        ("OUT", "Money Out"),
    )

    # Mpesa channel/type
    CHANNEL_CHOICES = (
        ("STK", "STK Push"),
        ("B2C", "B2C Payout"),
        ("C2B", "C2B Confirmation"),  # optional if later you support paybill confirmations
    )

    # Processing status (includes TIMEOUT because your view fallback sets TIMEOUT)
    STATUS_CHOICES = (
        ("INITIATED", "Initiated"),   # created locally (before real API call)
        ("PENDING", "Pending"),       # waiting callback
        ("SUCCESS", "Success"),
        ("FAILED", "Failed"),
        ("CANCELLED", "Cancelled"),
        ("TIMEOUT", "Timeout"),
    )

    # Purpose (business meaning)
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
        help_text="Owner/user related to this transaction (if known).",
    )

    # Views expect to accept phone like 07.. / 01..; services can normalize to 254..
    phone = models.CharField(max_length=20, validators=[phone_validator])
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )

    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES, default="IN")
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, default="STK")
    purpose = models.CharField(max_length=30, choices=PURPOSE_CHOICES, default="OTHER")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="INITIATED")

    # Your views fallback sends `reference=...`
    reference = models.CharField(max_length=120, blank=True, default="")

    # --- STK identifiers ---
    merchant_request_id = models.CharField(max_length=120, null=True, blank=True)
    checkout_request_id = models.CharField(max_length=120, null=True, blank=True, db_index=True)

    # --- B2C identifiers ---
    conversation_id = models.CharField(max_length=120, null=True, blank=True, db_index=True)
    originator_conversation_id = models.CharField(max_length=120, null=True, blank=True)

    # --- Result from callback ---
    result_code = models.CharField(max_length=20, null=True, blank=True)
    result_desc = models.CharField(max_length=255, null=True, blank=True)

    # For SUCCESS transactions (STK success usually returns receipt)
    mpesa_receipt_number = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    transaction_date = models.DateTimeField(null=True, blank=True)

    # Raw payloads (audit/debug)
    request_payload = models.JSONField(null=True, blank=True)
    callback_payload = models.JSONField(null=True, blank=True)

    # Link to "what this payment was for" (any model: SavingsTransaction, LoanPayment, Contribution, etc.)
    target_content_type = models.ForeignKey(ContentType, on_delete=models.SET_NULL, null=True, blank=True)
    target_object_id = models.PositiveIntegerField(null=True, blank=True)
    target_object = GenericForeignKey("target_content_type", "target_object_id")

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["status", "channel", "direction"]),
            models.Index(fields=["phone", "created_at"]),
        ]

    def __str__(self):
        return f"MpesaTx#{self.id} {self.channel} {self.direction} {self.amount} {self.status}"


# =========================
# Ledger / History
# =========================
class PaymentLedger(models.Model):
    """
    UI-friendly payment history table.
    Everything that affects a user's money should land here.

    Examples:
    - Savings deposit -> CREDIT
    - Loan repayment  -> CREDIT
    - Withdrawal      -> DEBIT
    """

    ENTRY_CHOICES = (
        ("CREDIT", "Credit (Money In)"),
        ("DEBIT", "Debit (Money Out)"),
    )

    CATEGORY_CHOICES = (
        ("SAVINGS", "Savings"),
        ("LOANS", "Loans"),
        ("MERRY", "Merry-go-round"),
        ("GROUP", "Group"),
        ("WITHDRAWAL", "Withdrawal"),
        ("OTHER", "Other"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ledger_entries",
    )

    entry_type = models.CharField(max_length=10, choices=ENTRY_CHOICES)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="OTHER")

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )

    narration = models.CharField(max_length=255, blank=True, default="")
    reference = models.CharField(
        max_length=120, blank=True, default="", db_index=True,
        help_text="Internal or Mpesa ref, e.g. MPESA receipt / LOAN#12"
    )

    # link to mpesa transaction if mpesa-based
    mpesa_tx = models.ForeignKey(
        MpesaTransaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ledger_entries",
    )

    # link to anything (Loan, SavingsAccount, Merry, Contribution, etc.)
    target_content_type = models.ForeignKey(ContentType, on_delete=models.SET_NULL, null=True, blank=True)
    target_object_id = models.PositiveIntegerField(null=True, blank=True)
    target_object = GenericForeignKey("target_content_type", "target_object_id")

    created_at = models.DateTimeField(default=timezone.now)

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
# Withdrawals (Admin Approval Required)
# =========================
class WithdrawalRequest(models.Model):
    """
    Withdrawal workflow:
    1) member creates request -> PENDING
    2) admin approves -> APPROVED
    3) system triggers B2C payout -> PROCESSING
    4) callback -> PAID or FAILED
    """

    STATUS_CHOICES = (
        ("PENDING", "Pending"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
        ("PROCESSING", "Processing"),
        ("PAID", "Paid"),
        ("FAILED", "Failed"),
        ("CANCELLED", "Cancelled"),
    )

    SOURCE_CHOICES = (
        ("SAVINGS", "Savings"),
        ("MERRY", "Merry-go-round"),
        ("GROUP", "Group"),
        ("OTHER", "Other"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="withdrawal_requests",
    )

    phone = models.CharField(max_length=20, validators=[phone_validator])
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )

    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="SAVINGS")

    # link withdrawal to specific object: SavingsAccount / Merry / etc.
    target_content_type = models.ForeignKey(ContentType, on_delete=models.SET_NULL, null=True, blank=True)
    target_object_id = models.PositiveIntegerField(null=True, blank=True)
    target_object = GenericForeignKey("target_content_type", "target_object_id")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING")

    # Admin decision fields
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

    # linked mpesa payout transaction (B2C)
    mpesa_tx = models.OneToOneField(
        MpesaTransaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="withdrawal_request",
    )

    created_at = models.DateTimeField(default=timezone.now)
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