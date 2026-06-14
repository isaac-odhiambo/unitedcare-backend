from decimal import Decimal

from django.contrib import admin, messages
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone

from .models import (
    Loan,
    LoanGuarantor,
    LoanInstallment,
    LoanPayment,
    LoanProduct,
    LoanReminderLog,
    LoanSecurityAllocation,
    MemberCreditProfile,
)
from .services import (
    apply_weekly_late_fees,
    approve_loan_and_create_schedule,
    release_reserved_security_for_loan,
)


# =========================================================
# HELPERS
# =========================================================
def _today():
    return timezone.now().date()


def _safe_update_fields(obj, field_names):
    """
    Keeps admin actions safe when old migrations or optional fields differ.
    """
    model_fields = {field.name for field in obj._meta.get_fields()}
    return [field for field in field_names if field in model_fields]


def _installment_full_due(inst: LoanInstallment) -> Decimal:
    return max(
        Decimal("0.00"),
        Decimal(inst.total_due or Decimal("0.00"))
        + Decimal(getattr(inst, "default_interest", Decimal("0.00")) or Decimal("0.00"))
        + Decimal(getattr(inst, "late_fee", Decimal("0.00")) or Decimal("0.00")),
    )


def _refresh_installment_status(inst: LoanInstallment) -> None:
    if inst.is_paid:
        inst.status = "PAID"
        if not inst.paid_at:
            inst.paid_at = timezone.now()
        return

    if Decimal(inst.paid_amount or Decimal("0.00")) > Decimal("0.00"):
        inst.status = "PARTIAL"
        return

    today = _today()
    if inst.default_interest_start_date and today >= inst.default_interest_start_date:
        inst.status = "DEFAULTED"
        if not inst.defaulted_at:
            inst.defaulted_at = timezone.now()
        return

    if inst.due_date < today:
        inst.status = "OVERDUE"
        return

    if inst.due_date == today:
        inst.status = "DUE_TODAY"
        return

    days_remaining = (inst.due_date - today).days
    inst.status = "DUE_SOON" if days_remaining <= 3 else "PENDING"


# =========================================================
# LOAN ACTIONS
# =========================================================
@admin.action(description="Mark selected loans as under review")
def mark_loans_under_review(modeladmin, request, queryset):
    updated = queryset.filter(status="PENDING").update(status="UNDER_REVIEW")
    modeladmin.message_user(
        request,
        f"{updated} loan(s) marked as UNDER REVIEW.",
        level=messages.SUCCESS,
    )


@admin.action(description="Approve selected loans using service logic")
def approve_loans(modeladmin, request, queryset):
    count = 0
    failed = 0

    for loan in queryset.select_related("product", "borrower"):
        try:
            approve_loan_and_create_schedule(loan)
            count += 1
        except Exception as exc:
            failed += 1
            modeladmin.message_user(
                request,
                f"Loan #{loan.id} approval failed: {exc}",
                level=messages.ERROR,
            )

    if count:
        modeladmin.message_user(
            request,
            f"{count} loan(s) approved successfully.",
            level=messages.SUCCESS,
        )
    if failed:
        modeladmin.message_user(
            request,
            f"{failed} loan(s) could not be approved.",
            level=messages.WARNING,
        )


@admin.action(description="Disburse selected approved loans")
def disburse_loans(modeladmin, request, queryset):
    count = 0
    skipped = 0
    now = timezone.now()

    for loan in queryset.select_for_update():
        if loan.status != "APPROVED":
            skipped += 1
            continue

        loan.status = "DISBURSED"
        if hasattr(loan, "disbursed_at") and not loan.disbursed_at:
            loan.disbursed_at = now

        update_fields = _safe_update_fields(loan, ["status", "disbursed_at", "updated_at"])
        loan.save(update_fields=update_fields)
        count += 1

    if count:
        modeladmin.message_user(
            request,
            f"{count} loan(s) disbursed.",
            level=messages.SUCCESS,
        )
    if skipped:
        modeladmin.message_user(
            request,
            f"{skipped} loan(s) skipped because they were not APPROVED.",
            level=messages.WARNING,
        )


@admin.action(description="Reject selected loans")
def reject_loans(modeladmin, request, queryset):
    count = 0
    failed = 0
    now = timezone.now()

    for loan in queryset:
        try:
            if loan.status not in ("PENDING", "UNDER_REVIEW", "APPROVED"):
                continue

            if Decimal(loan.security_reserved_total or Decimal("0.00")) > Decimal("0.00"):
                release_reserved_security_for_loan(loan)

            loan.status = "REJECTED"
            if hasattr(loan, "rejected_at"):
                loan.rejected_at = now

            update_fields = _safe_update_fields(loan, ["status", "rejected_at", "updated_at"])
            loan.save(update_fields=update_fields)
            count += 1
        except Exception as exc:
            failed += 1
            modeladmin.message_user(
                request,
                f"Loan #{loan.id} rejection failed: {exc}",
                level=messages.ERROR,
            )

    if count:
        modeladmin.message_user(
            request,
            f"{count} loan(s) rejected.",
            level=messages.WARNING,
        )
    if failed:
        modeladmin.message_user(
            request,
            f"{failed} loan(s) could not be rejected.",
            level=messages.ERROR,
        )


@admin.action(description="Mark selected loans as defaulted")
def default_loans(modeladmin, request, queryset):
    count = 0
    now = timezone.now()

    for loan in queryset.exclude(status__in=("COMPLETED", "REJECTED", "CANCELLED")):
        loan.status = "DEFAULTED"
        loan.is_defaulter = True
        if hasattr(loan, "defaulted_at") and not loan.defaulted_at:
            loan.defaulted_at = now

        update_fields = _safe_update_fields(
            loan,
            ["status", "is_defaulter", "defaulted_at", "updated_at"],
        )
        loan.save(update_fields=update_fields)
        count += 1

    modeladmin.message_user(
        request,
        f"{count} loan(s) marked as DEFAULTED.",
        level=messages.WARNING,
    )


@admin.action(description="Mark selected loans as completed")
def complete_loans(modeladmin, request, queryset):
    count = 0
    failed = 0
    now = timezone.now()

    for loan in queryset:
        try:
            if loan.status not in ("APPROVED", "DISBURSED", "UNDER_REPAYMENT", "DEFAULTED"):
                continue

            loan.recompute_balances()
            loan.status = "COMPLETED"
            loan.is_defaulter = False
            loan.outstanding_balance = Decimal("0.00")
            loan.completed_at = now

            update_fields = _safe_update_fields(
                loan,
                [
                    "status",
                    "is_defaulter",
                    "total_paid",
                    "outstanding_balance",
                    "default_interest_total",
                    "late_fee_total",
                    "completed_at",
                    "updated_at",
                ],
            )
            loan.save(update_fields=update_fields)

            release_reserved_security_for_loan(loan)
            count += 1
        except Exception as exc:
            failed += 1
            modeladmin.message_user(
                request,
                f"Loan #{loan.id} completion failed: {exc}",
                level=messages.ERROR,
            )

    if count:
        modeladmin.message_user(
            request,
            f"{count} loan(s) marked as COMPLETED.",
            level=messages.SUCCESS,
        )
    if failed:
        modeladmin.message_user(
            request,
            f"{failed} loan(s) could not be completed.",
            level=messages.WARNING,
        )


@admin.action(description="Recompute balances for selected loans")
def recompute_selected_loan_balances(modeladmin, request, queryset):
    count = 0

    for loan in queryset:
        loan.recompute_balances()
        update_fields = _safe_update_fields(
            loan,
            [
                "total_paid",
                "outstanding_balance",
                "default_interest_total",
                "late_fee_total",
                "status",
                "is_defaulter",
                "completed_at",
                "updated_at",
            ],
        )
        loan.save(update_fields=update_fields)
        count += 1

    modeladmin.message_user(
        request,
        f"Balances recomputed for {count} loan(s).",
        level=messages.SUCCESS,
    )


@admin.action(description="Apply default interest scheduler once")
def apply_default_interest_now(modeladmin, request, queryset):
    """
    The scheduler works globally because overdue installments may cross many loans.
    This action runs it once from admin.
    """
    try:
        count = apply_weekly_late_fees()
        modeladmin.message_user(
            request,
            f"Default interest check completed. {count} fee application(s) processed.",
            level=messages.SUCCESS,
        )
    except Exception as exc:
        modeladmin.message_user(
            request,
            f"Default interest check failed: {exc}",
            level=messages.ERROR,
        )


@admin.action(description="Release reserved security for selected loans")
def release_selected_loan_security(modeladmin, request, queryset):
    count = 0
    failed = 0

    for loan in queryset:
        try:
            release_reserved_security_for_loan(loan)
            count += 1
        except Exception as exc:
            failed += 1
            modeladmin.message_user(
                request,
                f"Loan #{loan.id} security release failed: {exc}",
                level=messages.ERROR,
            )

    if count:
        modeladmin.message_user(
            request,
            f"Released security for {count} loan(s).",
            level=messages.SUCCESS,
        )
    if failed:
        modeladmin.message_user(
            request,
            f"{failed} loan(s) could not release security.",
            level=messages.WARNING,
        )


# =========================================================
# GUARANTOR ACTIONS
# =========================================================
@admin.action(description="Accept selected guarantors")
def accept_guarantors(modeladmin, request, queryset):
    count = 0
    now = timezone.now()

    for row in queryset:
        if row.accepted:
            continue

        row.accepted = True
        row.accepted_at = now
        if hasattr(row, "rejected_at"):
            row.rejected_at = None

        update_fields = _safe_update_fields(
            row,
            ["accepted", "accepted_at", "rejected_at", "updated_at"],
        )
        row.save(update_fields=update_fields)
        count += 1

    modeladmin.message_user(
        request,
        f"{count} guarantor(s) accepted.",
        level=messages.SUCCESS,
    )


@admin.action(description="Reject selected guarantors")
def reject_guarantors(modeladmin, request, queryset):
    count = 0
    now = timezone.now()

    for row in queryset:
        row.accepted = False
        if hasattr(row, "accepted_at"):
            row.accepted_at = None
        if hasattr(row, "rejected_at"):
            row.rejected_at = now

        update_fields = _safe_update_fields(
            row,
            ["accepted", "accepted_at", "rejected_at", "updated_at"],
        )
        row.save(update_fields=update_fields)
        count += 1

    modeladmin.message_user(
        request,
        f"{count} guarantor(s) rejected.",
        level=messages.WARNING,
    )


@admin.action(description="Reset selected guarantors to not accepted")
def unaccept_guarantors(modeladmin, request, queryset):
    count = 0

    for row in queryset:
        row.accepted = False
        row.accepted_at = None
        if hasattr(row, "rejected_at"):
            row.rejected_at = None

        update_fields = _safe_update_fields(
            row,
            ["accepted", "accepted_at", "rejected_at", "updated_at"],
        )
        row.save(update_fields=update_fields)
        count += 1

    modeladmin.message_user(
        request,
        f"{count} guarantor record(s) reset to not accepted.",
        level=messages.WARNING,
    )


# =========================================================
# INSTALLMENT ACTIONS
# =========================================================
@admin.action(description="Mark selected installments as paid")
def mark_installments_paid(modeladmin, request, queryset):
    count = 0
    now = timezone.now()

    for inst in queryset:
        inst.paid_amount = _installment_full_due(inst)
        inst.is_paid = True
        inst.status = "PAID"
        inst.paid_at = now

        update_fields = _safe_update_fields(
            inst,
            ["paid_amount", "is_paid", "status", "paid_at", "updated_at"],
        )
        inst.save(update_fields=update_fields)
        count += 1

    modeladmin.message_user(
        request,
        f"{count} installment(s) marked as paid.",
        level=messages.SUCCESS,
    )


@admin.action(description="Mark selected installments as unpaid")
def mark_installments_unpaid(modeladmin, request, queryset):
    count = 0

    for inst in queryset:
        inst.paid_amount = Decimal("0.00")
        inst.is_paid = False
        inst.paid_at = None
        _refresh_installment_status(inst)

        update_fields = _safe_update_fields(
            inst,
            ["paid_amount", "is_paid", "status", "paid_at", "defaulted_at", "updated_at"],
        )
        inst.save(update_fields=update_fields)
        count += 1

    modeladmin.message_user(
        request,
        f"{count} installment(s) marked as unpaid.",
        level=messages.WARNING,
    )


@admin.action(description="Refresh selected installment statuses")
def refresh_installment_statuses(modeladmin, request, queryset):
    count = 0

    for inst in queryset:
        _refresh_installment_status(inst)
        update_fields = _safe_update_fields(
            inst,
            ["status", "defaulted_at", "paid_at", "updated_at"],
        )
        inst.save(update_fields=update_fields)
        count += 1

    modeladmin.message_user(
        request,
        f"{count} installment status value(s) refreshed.",
        level=messages.SUCCESS,
    )


# =========================================================
# SECURITY ALLOCATION ACTIONS
# =========================================================
@admin.action(description="Release security via parent loan cleanup")
def release_security_allocations(modeladmin, request, queryset):
    """
    Safer than releasing allocations one by one, because the parent loan
    service also restores savings and group reserved amounts correctly.
    """
    count = 0
    failed = 0
    loan_ids = list(queryset.values_list("loan_id", flat=True).distinct())

    for loan in Loan.objects.filter(id__in=loan_ids):
        try:
            release_reserved_security_for_loan(loan)
            count += 1
        except Exception as exc:
            failed += 1
            modeladmin.message_user(
                request,
                f"Loan #{loan.id} security release failed: {exc}",
                level=messages.ERROR,
            )

    if count:
        modeladmin.message_user(
            request,
            f"Released security for {count} loan(s).",
            level=messages.SUCCESS,
        )
    if failed:
        modeladmin.message_user(
            request,
            f"{failed} loan(s) could not release security.",
            level=messages.WARNING,
        )


# =========================================================
# INLINES
# =========================================================
class LoanGuarantorInline(admin.TabularInline):
    model = LoanGuarantor
    extra = 0
    fields = (
        "guarantor",
        "accepted",
        "accepted_at",
        "rejected_at",
        "reserved_amount",
        "request_note",
        "admin_note",
        "created_at",
    )
    readonly_fields = ("created_at",)


class LoanSecurityAllocationInline(admin.TabularInline):
    model = LoanSecurityAllocation
    extra = 0
    fields = (
        "source_type",
        "owner_user",
        "guarantor_link",
        "savings_account",
        "merry",
        "group",
        "amount",
        "is_active",
        "created_at",
        "released_at",
    )
    readonly_fields = ("created_at", "released_at")


class LoanInstallmentInline(admin.TabularInline):
    model = LoanInstallment
    extra = 0
    fields = (
        "installment_no",
        "status",
        "due_date",
        "grace_ends_on",
        "default_interest_start_date",
        "principal_due",
        "interest_due",
        "total_due",
        "default_interest",
        "default_interest_weeks_applied",
        "late_fee",
        "late_fee_weeks_applied",
        "paid_amount",
        "is_paid",
        "paid_at",
        "defaulted_at",
    )
    readonly_fields = (
        "installment_no",
        "grace_ends_on",
        "default_interest_start_date",
        "paid_at",
        "defaulted_at",
    )


class LoanPaymentInline(admin.TabularInline):
    model = LoanPayment
    extra = 0
    fields = (
        "amount",
        "paid_at",
        "method",
        "reference",
        "applied_to_principal",
        "applied_to_interest",
        "applied_to_default_interest",
        "applied_to_late_fee",
        "excess_to_savings",
    )
    readonly_fields = ("paid_at",)


class LoanReminderLogInline(admin.TabularInline):
    model = LoanReminderLog
    extra = 0
    fields = (
        "reminder_type",
        "channel",
        "installment",
        "days_remaining",
        "days_overdue",
        "message",
        "sent_by",
        "sent_at",
        "was_successful",
    )
    readonly_fields = (
        "reminder_type",
        "channel",
        "installment",
        "days_remaining",
        "days_overdue",
        "message",
        "sent_by",
        "sent_at",
        "was_successful",
    )
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


# =========================================================
# MEMBER CREDIT PROFILE ADMIN
# =========================================================
@admin.register(MemberCreditProfile)
class MemberCreditProfileAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "score",
        "total_loans",
        "loans_completed",
        "loans_defaulted",
        "late_payments",
        "updated_at",
    )
    list_filter = ("updated_at",)
    search_fields = (
        "user__username",
        "user__phone",
        "user__first_name",
        "user__last_name",
    )
    ordering = ("-id",)
    readonly_fields = ("updated_at",)


# =========================================================
# LOAN PRODUCT ADMIN
# =========================================================
@admin.register(LoanProduct)
class LoanProductAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "interest_type",
        "annual_interest_rate",
        "repayment_frequency",
        "repayment_weekday",
        "max_weeks",
        "grace_period_days",
        "default_interest_rate_weekly",
        "late_fee_rate_weekly",
        "is_active",
        "is_default",
    )
    list_filter = (
        "interest_type",
        "repayment_frequency",
        "is_active",
        "is_default",
    )
    search_fields = ("name",)
    ordering = ("name", "id")
    fieldsets = (
        ("Product", {
            "fields": (
                "name",
                "is_active",
                "is_default",
            )
        }),
        ("Normal interest", {
            "fields": (
                "interest_type",
                "annual_interest_rate",
            )
        }),
        ("Repayment", {
            "fields": (
                "repayment_frequency",
                "repayment_weekday",
                "max_weeks",
            )
        }),
        ("Default settings", {
            "fields": (
                "grace_period_days",
                "default_interest_rate_weekly",
                "late_fee_rate_weekly",
            )
        }),
        ("Audit", {
            "fields": (
                "created_at",
                "updated_at",
            )
        }),
    )
    readonly_fields = ("created_at", "updated_at")


# =========================================================
# LOAN ADMIN
# =========================================================
@admin.register(Loan)
class LoanAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "borrower",
        "product",
        "principal",
        "term_weeks",
        "status",
        "is_defaulter",
        "total_payable",
        "total_paid",
        "outstanding_balance",
        "default_interest_total",
        "security_reserved_total",
        "approved_at",
        "disbursed_at",
        "defaulted_at",
        "created_at",
    )

    list_filter = (
        "status",
        "is_defaulter",
        "product",
        "approved_at",
        "disbursed_at",
        "repayment_started_at",
        "defaulted_at",
        "rejected_at",
        "completed_at",
        "created_at",
    )

    search_fields = (
        "borrower__username",
        "borrower__phone",
        "borrower__first_name",
        "borrower__last_name",
        "product__name",
        "id",
    )

    ordering = ("-id",)

    actions = [
        mark_loans_under_review,
        approve_loans,
        disburse_loans,
        reject_loans,
        complete_loans,
        default_loans,
        recompute_selected_loan_balances,
        apply_default_interest_now,
        release_selected_loan_security,
    ]

    inlines = [
        LoanGuarantorInline,
        LoanSecurityAllocationInline,
        LoanInstallmentInline,
        LoanPaymentInline,
        LoanReminderLogInline,
    ]

    fieldsets = (
        ("Borrower and product", {
            "fields": (
                "borrower",
                "product",
            )
        }),
        ("Loan terms", {
            "fields": (
                "principal",
                "term_weeks",
                "status",
                "is_defaulter",
                "is_active_display",
            )
        }),
        ("Lifecycle dates", {
            "fields": (
                "requested_at",
                "approved_at",
                "rejected_at",
                "disbursed_at",
                "repayment_started_at",
                "defaulted_at",
                "completed_at",
                "cancelled_at",
                "created_at",
                "updated_at",
            )
        }),
        ("Balances", {
            "fields": (
                "total_payable",
                "normal_interest_total",
                "default_interest_total",
                "late_fee_total",
                "total_paid",
                "outstanding_balance",
            )
        }),
        ("Security", {
            "fields": (
                "security_target",
                "security_reserved_total",
            )
        }),
        ("Notes", {
            "fields": (
                "member_note",
                "admin_note",
            )
        }),
    )

    readonly_fields = (
        "status",
        "is_defaulter",
        "requested_at",
        "approved_at",
        "rejected_at",
        "disbursed_at",
        "repayment_started_at",
        "defaulted_at",
        "completed_at",
        "cancelled_at",
        "created_at",
        "updated_at",
        "total_payable",
        "normal_interest_total",
        "default_interest_total",
        "late_fee_total",
        "total_paid",
        "outstanding_balance",
        "security_target",
        "security_reserved_total",
        "is_active_display",
    )

    def is_active_display(self, obj):
        return obj.is_active

    is_active_display.boolean = True
    is_active_display.short_description = "Active Loan"

    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))
        if obj:
            ro.extend(["borrower", "product", "principal", "term_weeks"])
        return tuple(dict.fromkeys(ro))

    def save_model(self, request, obj, form, change):
        """
        Lifecycle transitions should use admin actions or service methods.
        This prevents manual form edits from skipping accounting logic.
        """
        if change:
            old_obj = Loan.objects.get(pk=obj.pk)
            protected_statuses = {
                "APPROVED",
                "DISBURSED",
                "UNDER_REPAYMENT",
                "REJECTED",
                "COMPLETED",
                "DEFAULTED",
                "CANCELLED",
            }
            if obj.status != old_obj.status and obj.status in protected_statuses:
                raise DjangoValidationError(
                    "Do not change loan status from the form. Use admin actions instead."
                )

        super().save_model(request, obj, form, change)


# =========================================================
# LOAN SECURITY ALLOCATION ADMIN
# =========================================================
@admin.register(LoanSecurityAllocation)
class LoanSecurityAllocationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "loan",
        "source_type",
        "owner_user",
        "amount",
        "is_active",
        "created_at",
        "released_at",
    )
    list_filter = (
        "source_type",
        "is_active",
        "created_at",
        "released_at",
        "merry",
        "group",
    )
    search_fields = (
        "owner_user__username",
        "owner_user__phone",
        "owner_user__first_name",
        "owner_user__last_name",
        "loan__id",
    )
    ordering = ("-id",)
    readonly_fields = ("created_at", "released_at")
    actions = [release_security_allocations]


# =========================================================
# LOAN GUARANTOR ADMIN
# =========================================================
@admin.register(LoanGuarantor)
class LoanGuarantorAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "loan",
        "guarantor",
        "accepted",
        "accepted_at",
        "rejected_at",
        "reserved_amount",
        "loan_status_display",
        "created_at",
    )
    list_filter = (
        "accepted",
        "accepted_at",
        "rejected_at",
        "created_at",
        "loan__status",
    )
    search_fields = (
        "guarantor__username",
        "guarantor__phone",
        "guarantor__first_name",
        "guarantor__last_name",
        "loan__borrower__username",
        "loan__borrower__phone",
        "loan__id",
    )
    ordering = ("-id",)
    actions = [accept_guarantors, reject_guarantors, unaccept_guarantors]
    readonly_fields = ("created_at", "updated_at")

    def loan_status_display(self, obj):
        return obj.loan.status

    loan_status_display.short_description = "Loan Status"


# =========================================================
# LOAN INSTALLMENT ADMIN
# =========================================================
@admin.register(LoanInstallment)
class LoanInstallmentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "loan",
        "installment_no",
        "status",
        "due_date",
        "grace_ends_on",
        "principal_due",
        "interest_due",
        "total_due",
        "default_interest",
        "late_fee",
        "paid_amount",
        "is_paid",
        "full_due_display",
        "days_remaining_display",
        "days_overdue_display",
    )
    list_filter = (
        "status",
        "is_paid",
        "due_date",
        "grace_ends_on",
        "loan__status",
    )
    search_fields = (
        "loan__borrower__username",
        "loan__borrower__phone",
        "loan__borrower__first_name",
        "loan__borrower__last_name",
        "loan__id",
    )
    ordering = ("loan", "installment_no")
    actions = [
        mark_installments_paid,
        mark_installments_unpaid,
        refresh_installment_statuses,
    ]
    readonly_fields = (
        "created_at",
        "updated_at",
        "paid_at",
        "defaulted_at",
        "last_default_interest_applied_at",
    )

    def full_due_display(self, obj):
        return obj.full_amount_due

    full_due_display.short_description = "Full Due"

    def days_remaining_display(self, obj):
        days = (obj.due_date - _today()).days
        return days if days >= 0 and not obj.is_paid else 0

    days_remaining_display.short_description = "Days Remaining"

    def days_overdue_display(self, obj):
        days = (_today() - obj.due_date).days
        return days if days > 0 and not obj.is_paid else 0

    days_overdue_display.short_description = "Days Overdue"


# =========================================================
# LOAN PAYMENT ADMIN
# =========================================================
@admin.register(LoanPayment)
class LoanPaymentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "loan",
        "borrower_display",
        "amount",
        "applied_to_principal",
        "applied_to_interest",
        "applied_to_default_interest",
        "applied_to_late_fee",
        "excess_to_savings",
        "method",
        "reference",
        "paid_at",
    )
    list_filter = (
        "method",
        "paid_at",
    )
    search_fields = (
        "loan__borrower__username",
        "loan__borrower__phone",
        "loan__borrower__first_name",
        "loan__borrower__last_name",
        "reference",
        "loan__id",
    )
    ordering = ("-paid_at", "-id")
    readonly_fields = (
        "paid_at",
        "created_at",
    )

    def borrower_display(self, obj):
        return obj.loan.borrower

    borrower_display.short_description = "Borrower"


# =========================================================
# LOAN REMINDER LOG ADMIN
# =========================================================
@admin.register(LoanReminderLog)
class LoanReminderLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "loan",
        "borrower",
        "installment",
        "reminder_type",
        "channel",
        "days_remaining",
        "days_overdue",
        "sent_by",
        "sent_at",
        "was_successful",
    )
    list_filter = (
        "reminder_type",
        "channel",
        "was_successful",
        "sent_at",
    )
    search_fields = (
        "loan__id",
        "borrower__username",
        "borrower__phone",
        "borrower__first_name",
        "borrower__last_name",
        "message",
    )
    ordering = ("-sent_at", "-id")
    readonly_fields = (
        "loan",
        "installment",
        "borrower",
        "reminder_type",
        "channel",
        "days_remaining",
        "days_overdue",
        "message",
        "sent_by",
        "sent_at",
        "was_successful",
        "failure_reason",
    )

    def has_add_permission(self, request):
        return False


# from django.contrib import admin, messages
# from django.core.exceptions import ValidationError as DjangoValidationError
# from django.utils import timezone

# from .models import (
#     MemberCreditProfile,
#     LoanProduct,
#     Loan,
#     LoanGuarantor,
#     LoanSecurityAllocation,
#     LoanInstallment,
#     LoanPayment,
# )
# from .services import approve_loan_and_create_schedule, release_reserved_security_for_loan


# # =========================================================
# # LOAN ACTIONS
# # =========================================================
# @admin.action(description="Mark selected loans as under review")
# def mark_loans_under_review(modeladmin, request, queryset):
#     updated = queryset.filter(status="PENDING").update(status="UNDER_REVIEW")
#     modeladmin.message_user(
#         request,
#         f"{updated} loan(s) marked as UNDER REVIEW.",
#         level=messages.SUCCESS,
#     )


# @admin.action(description="Approve selected loans using service logic")
# def approve_loans(modeladmin, request, queryset):
#     count = 0
#     failed = 0

#     for loan in queryset:
#         try:
#             approve_loan_and_create_schedule(loan)
#             count += 1
#         except Exception as e:
#             failed += 1
#             modeladmin.message_user(
#                 request,
#                 f"Loan #{loan.id} approval failed: {e}",
#                 level=messages.ERROR,
#             )

#     if count:
#         modeladmin.message_user(
#             request,
#             f"{count} loan(s) approved successfully.",
#             level=messages.SUCCESS,
#         )
#     if failed:
#         modeladmin.message_user(
#             request,
#             f"{failed} loan(s) could not be approved.",
#             level=messages.WARNING,
#         )


# @admin.action(description="Reject selected loans")
# def reject_loans(modeladmin, request, queryset):
#     count = 0
#     failed = 0

#     for loan in queryset:
#         try:
#             if loan.status not in ("PENDING", "UNDER_REVIEW", "APPROVED", "DEFAULTED"):
#                 continue

#             if loan.security_reserved_total and loan.security_reserved_total > 0:
#                 release_reserved_security_for_loan(loan)

#             loan.status = "REJECTED"
#             if hasattr(loan, "rejected_at"):
#                 loan.rejected_at = timezone.now()
#                 loan.save(update_fields=["status", "rejected_at"])
#             else:
#                 loan.save(update_fields=["status"])

#             count += 1
#         except Exception as e:
#             failed += 1
#             modeladmin.message_user(
#                 request,
#                 f"Loan #{loan.id} rejection failed: {e}",
#                 level=messages.ERROR,
#             )

#     if count:
#         modeladmin.message_user(
#             request,
#             f"{count} loan(s) rejected.",
#             level=messages.WARNING,
#         )
#     if failed:
#         modeladmin.message_user(
#             request,
#             f"{failed} loan(s) could not be rejected.",
#             level=messages.ERROR,
#         )


# @admin.action(description="Mark selected loans as completed")
# def complete_loans(modeladmin, request, queryset):
#     count = 0
#     failed = 0

#     for loan in queryset:
#         try:
#             if loan.status not in ("APPROVED", "UNDER_REPAYMENT", "DEFAULTED"):
#                 continue

#             loan.recompute_balances()
#             loan.status = "COMPLETED"
#             loan.outstanding_balance = 0
#             loan.completed_at = timezone.now()
#             loan.save(update_fields=["status", "outstanding_balance", "completed_at"])

#             release_reserved_security_for_loan(loan)
#             count += 1
#         except Exception as e:
#             failed += 1
#             modeladmin.message_user(
#                 request,
#                 f"Loan #{loan.id} completion failed: {e}",
#                 level=messages.ERROR,
#             )

#     if count:
#         modeladmin.message_user(
#             request,
#             f"{count} loan(s) marked as COMPLETED.",
#             level=messages.SUCCESS,
#         )
#     if failed:
#         modeladmin.message_user(
#             request,
#             f"{failed} loan(s) could not be completed.",
#             level=messages.WARNING,
#         )


# @admin.action(description="Mark selected loans as defaulted")
# def default_loans(modeladmin, request, queryset):
#     updated = queryset.exclude(
#         status__in=("COMPLETED", "REJECTED", "CANCELLED")
#     ).update(
#         status="DEFAULTED",
#         is_defaulter=True,
#     )
#     modeladmin.message_user(
#         request,
#         f"{updated} loan(s) marked as DEFAULTED.",
#         level=messages.WARNING,
#     )


# @admin.action(description="Recompute balances for selected loans")
# def recompute_selected_loan_balances(modeladmin, request, queryset):
#     count = 0
#     for loan in queryset:
#         loan.recompute_balances()
#         loan.save(update_fields=["outstanding_balance", "status", "completed_at"])
#         count += 1

#     modeladmin.message_user(
#         request,
#         f"Balances recomputed for {count} loan(s).",
#         level=messages.SUCCESS,
#     )


# @admin.action(description="Release reserved security for selected loans")
# def release_selected_loan_security(modeladmin, request, queryset):
#     count = 0
#     failed = 0

#     for loan in queryset:
#         try:
#             release_reserved_security_for_loan(loan)
#             count += 1
#         except Exception as e:
#             failed += 1
#             modeladmin.message_user(
#                 request,
#                 f"Loan #{loan.id} security release failed: {e}",
#                 level=messages.ERROR,
#             )

#     if count:
#         modeladmin.message_user(
#             request,
#             f"Released security for {count} loan(s).",
#             level=messages.SUCCESS,
#         )
#     if failed:
#         modeladmin.message_user(
#             request,
#             f"{failed} loan(s) could not release security.",
#             level=messages.WARNING,
#         )


# # =========================================================
# # GUARANTOR ACTIONS
# # =========================================================
# @admin.action(description="Accept selected guarantors")
# def accept_guarantors(modeladmin, request, queryset):
#     count = 0
#     for row in queryset:
#         if row.accepted:
#             continue
#         row.accepted = True
#         row.accepted_at = timezone.now()
#         row.save(update_fields=["accepted", "accepted_at"])
#         count += 1

#     modeladmin.message_user(
#         request,
#         f"{count} guarantor(s) accepted.",
#         level=messages.SUCCESS,
#     )


# @admin.action(description="Unaccept selected guarantors")
# def unaccept_guarantors(modeladmin, request, queryset):
#     updated = queryset.update(accepted=False, accepted_at=None)
#     modeladmin.message_user(
#         request,
#         f"{updated} guarantor record(s) reset to not accepted.",
#         level=messages.WARNING,
#     )


# # =========================================================
# # INSTALLMENT ACTIONS
# # =========================================================
# @admin.action(description="Mark selected installments as paid")
# def mark_installments_paid(modeladmin, request, queryset):
#     updated = queryset.update(is_paid=True)
#     modeladmin.message_user(
#         request,
#         f"{updated} installment(s) marked as paid.",
#         level=messages.SUCCESS,
#     )


# @admin.action(description="Mark selected installments as unpaid")
# def mark_installments_unpaid(modeladmin, request, queryset):
#     updated = queryset.update(is_paid=False)
#     modeladmin.message_user(
#         request,
#         f"{updated} installment(s) marked as unpaid.",
#         level=messages.WARNING,
#     )


# # =========================================================
# # SECURITY ALLOCATION ACTIONS
# # =========================================================
# @admin.action(description="Release security via parent loan cleanup")
# def release_security_allocations(modeladmin, request, queryset):
#     """
#     Safer than releasing allocations one by one, because the parent loan
#     service also restores savings/group reserved amounts correctly.
#     """
#     count = 0
#     failed = 0
#     loan_ids = list(queryset.values_list("loan_id", flat=True).distinct())

#     for loan in Loan.objects.filter(id__in=loan_ids):
#         try:
#             release_reserved_security_for_loan(loan)
#             count += 1
#         except Exception as e:
#             failed += 1
#             modeladmin.message_user(
#                 request,
#                 f"Loan #{loan.id} security release failed: {e}",
#                 level=messages.ERROR,
#             )

#     if count:
#         modeladmin.message_user(
#             request,
#             f"Released security for {count} loan(s).",
#             level=messages.SUCCESS,
#         )
#     if failed:
#         modeladmin.message_user(
#             request,
#             f"{failed} loan(s) could not release security.",
#             level=messages.WARNING,
#         )


# # =========================================================
# # INLINES
# # =========================================================
# class LoanGuarantorInline(admin.TabularInline):
#     model = LoanGuarantor
#     extra = 0
#     fields = (
#         "guarantor",
#         "accepted",
#         "accepted_at",
#         "reserved_amount",
#         "request_note",
#         "admin_note",
#         "created_at",
#     )
#     readonly_fields = ("created_at",)


# class LoanSecurityAllocationInline(admin.TabularInline):
#     model = LoanSecurityAllocation
#     extra = 0
#     fields = (
#         "source_type",
#         "owner_user",
#         "guarantor_link",
#         "savings_account",
#         "merry",
#         "group",
#         "amount",
#         "is_active",
#         "created_at",
#         "released_at",
#     )
#     readonly_fields = (
#         "created_at",
#         "released_at",
#     )


# class LoanInstallmentInline(admin.TabularInline):
#     model = LoanInstallment
#     extra = 0
#     fields = (
#         "installment_no",
#         "due_date",
#         "principal_due",
#         "interest_due",
#         "total_due",
#         "late_fee",
#         "paid_amount",
#         "is_paid",
#     )
#     readonly_fields = ("installment_no",)


# class LoanPaymentInline(admin.TabularInline):
#     model = LoanPayment
#     extra = 0
#     fields = (
#         "amount",
#         "paid_at",
#         "method",
#         "reference",
#     )
#     readonly_fields = ("paid_at",)


# # =========================================================
# # MEMBER CREDIT PROFILE ADMIN
# # =========================================================
# @admin.register(MemberCreditProfile)
# class MemberCreditProfileAdmin(admin.ModelAdmin):
#     list_display = (
#         "id",
#         "user",
#         "score",
#         "total_loans",
#         "loans_completed",
#         "loans_defaulted",
#         "late_payments",
#         "updated_at",
#     )
#     list_filter = ("updated_at",)
#     search_fields = (
#         "user__username",
#         "user__phone",
#         "user__first_name",
#         "user__last_name",
#     )
#     ordering = ("-id",)
#     readonly_fields = ("updated_at",)


# # =========================================================
# # LOAN PRODUCT ADMIN
# # =========================================================
# @admin.register(LoanProduct)
# class LoanProductAdmin(admin.ModelAdmin):
#     list_display = (
#         "id",
#         "name",
#         "interest_type",
#         "annual_interest_rate",
#         "repayment_frequency",
#         "repayment_weekday",
#         "max_weeks",
#         "late_fee_rate_weekly",
#         "is_active",
#         "is_default",
#     )
#     list_filter = (
#         "interest_type",
#         "repayment_frequency",
#         "is_active",
#         "is_default",
#     )
#     search_fields = ("name",)
#     ordering = ("name",)


# # =========================================================
# # LOAN ADMIN
# # =========================================================
# @admin.register(Loan)
# class LoanAdmin(admin.ModelAdmin):
#     list_display = (
#         "id",
#         "borrower",
#         "product",
#         "principal",
#         "term_weeks",
#         "status",
#         "is_defaulter",
#         "total_payable",
#         "total_paid",
#         "outstanding_balance",
#         "security_target",
#         "security_reserved_total",
#         "approved_at",
#         "created_at",
#     )

#     list_filter = (
#         "status",
#         "is_defaulter",
#         "product",
#         "approved_at",
#         "rejected_at",
#         "completed_at",
#         "created_at",
#     )

#     search_fields = (
#         "borrower__username",
#         "borrower__phone",
#         "borrower__first_name",
#         "borrower__last_name",
#         "product__name",
#     )

#     ordering = ("-id",)

#     actions = [
#         mark_loans_under_review,
#         approve_loans,
#         reject_loans,
#         complete_loans,
#         default_loans,
#         recompute_selected_loan_balances,
#         release_selected_loan_security,
#     ]

#     inlines = [
#         LoanGuarantorInline,
#         LoanSecurityAllocationInline,
#         LoanInstallmentInline,
#         LoanPaymentInline,
#     ]

#     fieldsets = (
#         ("Borrower / Product", {
#             "fields": (
#                 "borrower",
#                 "product",
#             )
#         }),
#         ("Loan Terms", {
#             "fields": (
#                 "principal",
#                 "term_weeks",
#                 "status",
#                 "is_defaulter",
#                 "approved_at",
#                 "rejected_at",
#                 "completed_at",
#                 "created_at",
#                 "is_active_display",
#             )
#         }),
#         ("Balances", {
#             "fields": (
#                 "total_payable",
#                 "total_paid",
#                 "outstanding_balance",
#             )
#         }),
#         ("Security", {
#             "fields": (
#                 "security_target",
#                 "security_reserved_total",
#             )
#         }),
#         ("Notes", {
#             "fields": (
#                 "member_note",
#                 "admin_note",
#             )
#         }),
#     )

#     readonly_fields = (
#         "status",
#         "approved_at",
#         "rejected_at",
#         "completed_at",
#         "created_at",
#         "total_payable",
#         "total_paid",
#         "outstanding_balance",
#         "security_target",
#         "security_reserved_total",
#         "is_active_display",
#     )

#     def is_active_display(self, obj):
#         return obj.is_active

#     is_active_display.boolean = True
#     is_active_display.short_description = "Active Loan"

#     def get_readonly_fields(self, request, obj=None):
#         ro = list(super().get_readonly_fields(request, obj))
#         if obj:
#             # Once created, these should not be manually changed from admin form.
#             ro.extend(["borrower", "product", "principal", "term_weeks"])
#         return tuple(dict.fromkeys(ro))

#     def save_model(self, request, obj, form, change):
#         """
#         Prevent manual status-based approval/rejection/completion from the form.
#         Loan lifecycle transitions must go through admin actions or service methods.
#         """
#         if change:
#             old_obj = Loan.objects.get(pk=obj.pk)

#             protected_statuses = {"APPROVED", "REJECTED", "COMPLETED", "DEFAULTED"}
#             if obj.status != old_obj.status and obj.status in protected_statuses:
#                 raise DjangoValidationError(
#                     "Do not change loan status from the form. "
#                     "Use the admin actions: Approve selected loans using service logic, "
#                     "Reject selected loans, Mark selected loans as completed, or "
#                     "Mark selected loans as defaulted."
#                 )

#         super().save_model(request, obj, form, change)


# # =========================================================
# # LOAN SECURITY ALLOCATION ADMIN
# # =========================================================
# @admin.register(LoanSecurityAllocation)
# class LoanSecurityAllocationAdmin(admin.ModelAdmin):
#     list_display = (
#         "id",
#         "loan",
#         "source_type",
#         "owner_user",
#         "amount",
#         "is_active",
#         "created_at",
#         "released_at",
#     )
#     list_filter = (
#         "source_type",
#         "is_active",
#         "created_at",
#         "released_at",
#         "merry",
#         "group",
#     )
#     search_fields = (
#         "owner_user__username",
#         "owner_user__phone",
#         "owner_user__first_name",
#         "owner_user__last_name",
#         "loan__id",
#     )
#     ordering = ("-id",)
#     readonly_fields = (
#         "created_at",
#         "released_at",
#     )
#     actions = [release_security_allocations]


# # =========================================================
# # LOAN GUARANTOR ADMIN
# # =========================================================
# @admin.register(LoanGuarantor)
# class LoanGuarantorAdmin(admin.ModelAdmin):
#     list_display = (
#         "id",
#         "loan",
#         "guarantor",
#         "accepted",
#         "accepted_at",
#         "reserved_amount",
#         "loan_status_display",
#         "created_at",
#     )
#     list_filter = (
#         "accepted",
#         "accepted_at",
#         "created_at",
#         "loan__status",
#     )
#     search_fields = (
#         "guarantor__username",
#         "guarantor__phone",
#         "guarantor__first_name",
#         "guarantor__last_name",
#         "loan__borrower__username",
#         "loan__borrower__phone",
#     )
#     ordering = ("-id",)
#     actions = [accept_guarantors, unaccept_guarantors]

#     def loan_status_display(self, obj):
#         return obj.loan.status

#     loan_status_display.short_description = "Loan Status"


# # =========================================================
# # LOAN INSTALLMENT ADMIN
# # =========================================================
# @admin.register(LoanInstallment)
# class LoanInstallmentAdmin(admin.ModelAdmin):
#     list_display = (
#         "id",
#         "loan",
#         "installment_no",
#         "due_date",
#         "principal_due",
#         "interest_due",
#         "total_due",
#         "late_fee",
#         "paid_amount",
#         "is_paid",
#         "is_overdue_display",
#     )
#     list_filter = (
#         "is_paid",
#         "due_date",
#         "loan__status",
#     )
#     search_fields = (
#         "loan__borrower__username",
#         "loan__borrower__phone",
#         "loan__id",
#     )
#     ordering = ("loan", "installment_no")
#     actions = [mark_installments_paid, mark_installments_unpaid]

#     def is_overdue_display(self, obj):
#         return (not obj.is_paid) and (obj.due_date < timezone.now().date())

#     is_overdue_display.boolean = True
#     is_overdue_display.short_description = "Overdue"


# # =========================================================
# # LOAN PAYMENT ADMIN
# # =========================================================
# @admin.register(LoanPayment)
# class LoanPaymentAdmin(admin.ModelAdmin):
#     list_display = (
#         "id",
#         "loan",
#         "borrower_display",
#         "amount",
#         "method",
#         "reference",
#         "paid_at",
#     )
#     list_filter = (
#         "method",
#         "paid_at",
#     )
#     search_fields = (
#         "loan__borrower__username",
#         "loan__borrower__phone",
#         "loan__borrower__first_name",
#         "loan__borrower__last_name",
#         "reference",
#         "loan__id",
#     )
#     ordering = ("-paid_at",)
#     readonly_fields = ("paid_at",)

#     def borrower_display(self, obj):
#         return obj.loan.borrower

#     borrower_display.short_description = "Borrower"