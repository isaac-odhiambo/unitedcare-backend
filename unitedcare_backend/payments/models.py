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
# Mpesa Config
# =========================
class MpesaConfig(models.Model):
    """
    Central M-Pesa payment channel configuration.

    Stores business/payment numbers shown to users:
    - paybill number
    - business number / shortcode
    - till number

    Business references like:
      - mus12
      - saving23
      - loan35
      - grp9
    are not stored here. They are generated elsewhere.
    """

    name = models.CharField(
        max_length=100,
        default="default",
        unique=True,
        help_text="Friendly config name, e.g. default or production.",
    )

    paybill_number = models.CharField(
        max_length=30,
        blank=True,
        default="",
        db_index=True,
        help_text="Manual Paybill number shown to users.",
    )

    business_number = models.CharField(
        max_length=30,
        blank=True,
        default="",
        db_index=True,
        help_text="Primary business/short code used for collections.",
    )

    till_number = models.CharField(
        max_length=30,
        blank=True,
        default="",
        db_index=True,
        help_text="Optional till number if Buy Goods is enabled.",
    )

    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Whether this config is active for the system.",
    )

    is_paybill_enabled = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Controls whether manual paybill option is shown/enabled.",
    )

    is_till_enabled = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Controls whether till option is shown/enabled.",
    )

    notes = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Optional admin notes.",
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Mpesa Config"
        verbose_name_plural = "Mpesa Configs"

    def __str__(self):
        return (
            f"{self.name} | "
            f"paybill={self.paybill_number or '-'} | "
            f"business={self.business_number or '-'} | "
            f"till={self.till_number or '-'}"
        )


# =========================
# MpesaTransaction (central source of truth)
# =========================
class MpesaTransaction(models.Model):
    """
    CENTRAL source-of-truth for all M-Pesa flows.

    Supported:
    - STK Push from app
    - Manual outside-app paybill (C2B)
    - Buy Goods / Till if enabled
    - B2C payouts

    This model captures the raw payment event first.
    Domain allocation happens later in merry/savings/loans/groups services.

    Example outside-app merry payment:
      paybill number = 123456
      account/reference entered by payer = mus11

    In that case:
    - channel = C2B
    - origin = EXTERNAL
    - payment_method = PAYBILL
    - reference = mus11
    - external_reference_raw = mus11
    - purpose may start as MERRY_CONTRIBUTION if detected, else OTHER until parsed
    - allocation_status records allocation progress
    """

    DIRECTION_CHOICES = (
        ("IN", "Money In"),
        ("OUT", "Money Out"),
    )

    CHANNEL_CHOICES = (
        ("STK", "STK Push"),
        ("C2B", "C2B Paybill"),
        ("B2C", "B2C Payout"),
    )

    PAYMENT_METHOD_CHOICES = (
        ("STK", "STK Push"),
        ("PAYBILL", "Paybill"),
        ("TILL", "Till / Buy Goods"),
        ("B2C", "B2C Payout"),
        ("OTHER", "Other"),
    )

    ORIGIN_CHOICES = (
        ("APP", "Started Inside App"),
        ("EXTERNAL", "Started Outside App"),
        ("ADMIN", "Started by Admin"),
        ("SYSTEM", "System Generated"),
    )

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

    MATCHED_REFERENCE_TYPE_CHOICES = (
        ("MERRY", "Merry"),
        ("SAVINGS", "Savings"),
        ("LOAN", "Loan"),
        ("GROUP", "Group"),
        ("WITHDRAWAL", "Withdrawal"),
        ("OTHER", "Other"),
        ("UNKNOWN", "Unknown"),
    )

    ALLOCATION_STATUS_CHOICES = (
        ("UNALLOCATED", "Unallocated"),
        ("AUTO_ALLOCATED", "Auto Allocated"),
        ("PARTIALLY_ALLOCATED", "Partially Allocated"),
        ("MANUAL_REVIEW", "Manual Review"),
        ("MANUALLY_ALLOCATED", "Manually Allocated"),
        ("INVALID_REFERENCE", "Invalid Reference"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="mpesa_transactions",
        db_index=True,
        help_text="Resolved owner/user if known.",
    )

    phone = models.CharField(
        max_length=20,
        validators=[phone_validator],
        db_index=True,
        help_text="Phone used to make or receive the transaction.",
    )

    matched_user_phone = models.CharField(
        max_length=20,
        blank=True,
        default="",
        db_index=True,
        help_text="Registered user phone matched by backend, if any.",
    )

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Final amount actually transacted through M-Pesa.",
    )

    base_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Business/base amount before fee.",
    )

    transaction_fee = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Fee amount applied by backend.",
    )

    direction = models.CharField(
        max_length=10,
        choices=DIRECTION_CHOICES,
        default="IN",
        db_index=True,
    )

    channel = models.CharField(
        max_length=10,
        choices=CHANNEL_CHOICES,
        default="STK",
        db_index=True,
    )

    payment_method = models.CharField(
        max_length=30,
        choices=PAYMENT_METHOD_CHOICES,
        default="STK",
        db_index=True,
        help_text="How the payment was made: STK, PAYBILL, TILL, or B2C.",
    )

    origin = models.CharField(
        max_length=30,
        choices=ORIGIN_CHOICES,
        default="APP",
        db_index=True,
        help_text="Where the payment started: app, external/manual, admin, or system.",
    )

    purpose = models.CharField(
        max_length=30,
        choices=PURPOSE_CHOICES,
        default="OTHER",
        db_index=True,
    )

    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        default="INITIATED",
        db_index=True,
    )

    reference = models.CharField(
        max_length=120,
        blank=True,
        default="",
        db_index=True,
        help_text="Normalized business reference, e.g. mus11, saving23, loan35, grp9.",
    )

    external_reference_raw = models.CharField(
        max_length=120,
        blank=True,
        default="",
        db_index=True,
        help_text="Exact account/reference entered by payer outside the app.",
    )

    matched_reference_type = models.CharField(
        max_length=30,
        choices=MATCHED_REFERENCE_TYPE_CHOICES,
        default="UNKNOWN",
        db_index=True,
        help_text="What type of business reference this transaction matched.",
    )

    merchant_request_id = models.CharField(max_length=150, null=True, blank=True)

    checkout_request_id = models.CharField(
        max_length=150,
        null=True,
        blank=True,
        db_index=True,
        help_text="Unique STK request id from Safaricom.",
    )

    conversation_id = models.CharField(
        max_length=150,
        null=True,
        blank=True,
        db_index=True,
        help_text="Unique B2C conversation id from Safaricom.",
    )

    originator_conversation_id = models.CharField(
        max_length=150,
        null=True,
        blank=True,
    )

    result_code = models.CharField(max_length=30, null=True, blank=True)
    result_desc = models.CharField(max_length=255, null=True, blank=True)

    mpesa_receipt_number = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
        help_text="Safaricom receipt number for successful transactions.",
    )

    transaction_date = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Transaction date returned by Safaricom.",
    )

    callback_received_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="When the system received the callback or validation result.",
    )

    request_payload = models.JSONField(null=True, blank=True)
    callback_payload = models.JSONField(null=True, blank=True)

    target_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    target_object_id = models.PositiveIntegerField(null=True, blank=True)
    target_object = GenericForeignKey("target_content_type", "target_object_id")

    ledger_posted = models.BooleanField(default=False, db_index=True)

    allocation_status = models.CharField(
        max_length=25,
        choices=ALLOCATION_STATUS_CHOICES,
        default="UNALLOCATED",
        db_index=True,
    )

    allocation_notes = models.CharField(max_length=255, blank=True, default="")

    allocated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="allocated_mpesa_transactions",
    )

    allocated_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["status", "channel", "direction"]),
            models.Index(fields=["payment_method", "origin", "created_at"]),
            models.Index(fields=["phone", "created_at"]),
            models.Index(fields=["purpose", "created_at"]),
            models.Index(fields=["allocation_status", "created_at"]),
            models.Index(fields=["reference", "channel", "status"]),
            models.Index(fields=["external_reference_raw", "created_at"]),
            models.Index(fields=["matched_reference_type", "created_at"]),
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
            f"MpesaTx#{self.id} {self.channel}/{self.payment_method} "
            f"{self.direction} amount={self.amount} "
            f"ref={self.reference or '-'} status={self.status}"
        )


# =========================
# Ledger / History
# =========================
class PaymentLedger(models.Model):
    """
    UI-friendly central history.

    One MpesaTransaction can create multiple ledger lines.
    Example:
    - main money in/out line
    - fee line
    - adjustment line
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

    entry_type = models.CharField(
        max_length=10,
        choices=ENTRY_CHOICES,
        db_index=True,
    )

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

    STATUS_CHOICES = (
        ("PENDING", "PENDING"),
        ("APPROVED", "APPROVED"),
        ("REJECTED", "REJECTED"),
        ("PROCESSING", "PROCESSING"),
        ("PAID", "PAID"),
        ("FAILED", "FAILED"),
        ("CANCELLED", "CANCELLED"),
    )

    SOURCE_CHOICES = (
        ("SAVINGS", "SAVINGS"),
        ("MERRY", "MERRY"),
        ("GROUP", "GROUP"),
        ("OTHER", "OTHER"),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="withdrawal_requests",
        db_index=True,
    )

    phone = models.CharField(
        max_length=20,
        validators=[phone_validator],
        db_index=True,
    )

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Requested/base payout amount before fee deduction.",
    )

    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
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
        choices=STATUS_CHOICES,
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