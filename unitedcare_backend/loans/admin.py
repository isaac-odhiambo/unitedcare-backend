from django.contrib import admin, messages
from django.utils import timezone

from .models import (
    MemberCreditProfile,
    LoanProduct,
    Loan,
    MerryCreditHold,
    LoanGuarantor,
    LoanInstallment,
    LoanPayment,
)
from .services import approve_loan_and_create_schedule


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

    for loan in queryset:
        try:
            approve_loan_and_create_schedule(loan)
            count += 1
        except Exception as e:
            failed += 1
            modeladmin.message_user(
                request,
                f"Loan #{loan.id} approval failed: {e}",
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


@admin.action(description="Reject selected loans")
def reject_loans(modeladmin, request, queryset):
    updated = queryset.filter(status__in=("PENDING", "UNDER_REVIEW")).update(status="REJECTED")
    modeladmin.message_user(
        request,
        f"{updated} loan(s) rejected.",
        level=messages.WARNING,
    )


@admin.action(description="Mark selected loans as completed")
def complete_loans(modeladmin, request, queryset):
    count = 0
    for loan in queryset:
        if loan.status != "APPROVED":
            continue

        loan.recompute_balances()
        loan.status = "COMPLETED"
        loan.outstanding_balance = 0
        loan.save(update_fields=["status", "outstanding_balance"])
        count += 1

    modeladmin.message_user(
        request,
        f"{count} loan(s) marked as COMPLETED.",
        level=messages.SUCCESS,
    )


@admin.action(description="Mark selected loans as defaulted")
def default_loans(modeladmin, request, queryset):
    updated = queryset.exclude(status__in=("COMPLETED", "REJECTED")).update(
        status="DEFAULTED",
        is_defaulter=True,
    )
    modeladmin.message_user(
        request,
        f"{updated} loan(s) marked as DEFAULTED.",
        level=messages.WARNING,
    )


@admin.action(description="Recompute balances for selected loans")
def recompute_selected_loan_balances(modeladmin, request, queryset):
    count = 0
    for loan in queryset:
        loan.recompute_balances()
        loan.save(update_fields=["outstanding_balance", "status"])
        count += 1

    modeladmin.message_user(
        request,
        f"Balances recomputed for {count} loan(s).",
        level=messages.SUCCESS,
    )


# =========================================================
# GUARANTOR ACTIONS
# =========================================================
@admin.action(description="Accept selected guarantors")
def accept_guarantors(modeladmin, request, queryset):
    count = 0
    for row in queryset:
        if row.accepted:
            continue
        row.accepted = True
        row.accepted_at = timezone.now()
        row.save(update_fields=["accepted", "accepted_at"])
        count += 1

    modeladmin.message_user(
        request,
        f"{count} guarantor(s) accepted.",
        level=messages.SUCCESS,
    )


@admin.action(description="Unaccept selected guarantors")
def unaccept_guarantors(modeladmin, request, queryset):
    updated = queryset.update(accepted=False, accepted_at=None)
    modeladmin.message_user(
        request,
        f"{updated} guarantor record(s) reset to not accepted.",
        level=messages.WARNING,
    )


# =========================================================
# INSTALLMENT ACTIONS
# =========================================================
@admin.action(description="Mark selected installments as paid")
def mark_installments_paid(modeladmin, request, queryset):
    updated = queryset.update(is_paid=True)
    modeladmin.message_user(
        request,
        f"{updated} installment(s) marked as paid.",
        level=messages.SUCCESS,
    )


@admin.action(description="Mark selected installments as unpaid")
def mark_installments_unpaid(modeladmin, request, queryset):
    updated = queryset.update(is_paid=False)
    modeladmin.message_user(
        request,
        f"{updated} installment(s) marked as unpaid.",
        level=messages.WARNING,
    )


# =========================================================
# MERRY HOLD ACTIONS
# =========================================================
@admin.action(description="Release selected merry credit holds")
def release_merry_holds(modeladmin, request, queryset):
    count = 0
    for hold in queryset.filter(is_active=True):
        hold.release()
        count += 1

    modeladmin.message_user(
        request,
        f"{count} merry credit hold(s) released.",
        level=messages.SUCCESS,
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
        "reserved_amount",
    )


class LoanInstallmentInline(admin.TabularInline):
    model = LoanInstallment
    extra = 0
    fields = (
        "installment_no",
        "due_date",
        "principal_due",
        "interest_due",
        "total_due",
        "late_fee",
        "paid_amount",
        "is_paid",
    )
    readonly_fields = ("installment_no",)


class LoanPaymentInline(admin.TabularInline):
    model = LoanPayment
    extra = 0
    fields = (
        "amount",
        "paid_at",
        "method",
        "reference",
    )
    readonly_fields = ("paid_at",)


class MerryCreditHoldInline(admin.StackedInline):
    model = MerryCreditHold
    extra = 0
    can_delete = False
    fields = (
        "merry",
        "user",
        "amount",
        "is_active",
        "created_at",
        "released_at",
    )
    readonly_fields = (
        "created_at",
        "released_at",
    )


# =========================================================
# MEMBER CREDIT PROFILE ADMIN
# =========================================================
@admin.register(MemberCreditProfile)
class MemberCreditProfileAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "context_display",
        "score",
        "total_loans",
        "loans_completed",
        "loans_defaulted",
        "late_payments",
        "updated_at",
    )
    list_filter = (
        "updated_at",
        "merry",
        "group",
    )
    search_fields = (
        "user__username",
        "user__phone",
        "merry__name",
        "group__name",
    )
    ordering = ("-id",)
    readonly_fields = ("updated_at",)

    def context_display(self, obj):
        if obj.merry_id:
            return f"Merry: {obj.merry}"
        if obj.group_id:
            return f"Group: {obj.group}"
        return "-"

    context_display.short_description = "Context"


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
        "late_fee_rate_weekly",
        "is_active",
    )
    list_filter = (
        "interest_type",
        "repayment_frequency",
        "is_active",
    )
    search_fields = ("name",)
    ordering = ("name",)


# =========================================================
# LOAN ADMIN
# =========================================================
@admin.register(Loan)
class LoanAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "borrower",
        "context_display",
        "product",
        "principal",
        "term_weeks",
        "status",
        "is_defaulter",
        "total_payable",
        "total_paid",
        "outstanding_balance",
        "security_target",
        "approved_at",
        "created_at",
    )

    list_filter = (
        "status",
        "is_defaulter",
        "product",
        "approved_at",
        "created_at",
        "merry",
        "group",
    )

    search_fields = (
        "borrower__username",
        "borrower__phone",
        "product__name",
        "merry__name",
        "group__name",
    )

    ordering = ("-id",)

    readonly_fields = (
        "approved_at",
        "created_at",
        "is_active_display",
        "context_display",
    )

    actions = [
        mark_loans_under_review,
        approve_loans,
        reject_loans,
        complete_loans,
        default_loans,
        recompute_selected_loan_balances,
    ]

    inlines = [
        LoanGuarantorInline,
        LoanInstallmentInline,
        LoanPaymentInline,
        MerryCreditHoldInline,
    ]

    fieldsets = (
        ("Context", {
            "fields": (
                "borrower",
                "product",
                "merry",
                "group",
                "context_display",
            )
        }),
        ("Loan Terms", {
            "fields": (
                "principal",
                "term_weeks",
                "status",
                "is_defaulter",
                "approved_at",
                "created_at",
                "is_active_display",
            )
        }),
        ("Balances", {
            "fields": (
                "total_payable",
                "total_paid",
                "outstanding_balance",
            )
        }),
        ("Security / Reserves", {
            "fields": (
                "borrower_reserved_savings",
                "borrower_reserved_merry_credit",
                "security_target",
            )
        }),
    )

    def context_display(self, obj):
        if obj.merry_id:
            return f"Merry: {obj.merry}"
        if obj.group_id:
            return f"Group: {obj.group}"
        return "-"

    context_display.short_description = "Context"

    def is_active_display(self, obj):
        return obj.is_active

    is_active_display.boolean = True
    is_active_display.short_description = "Active Loan"

    def save_model(self, request, obj, form, change):
        if obj.status == "APPROVED" and not obj.approved_at:
            obj.approved_at = timezone.now()
        super().save_model(request, obj, form, change)


# =========================================================
# MERRY CREDIT HOLD ADMIN
# =========================================================
@admin.register(MerryCreditHold)
class MerryCreditHoldAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "loan",
        "merry",
        "user",
        "amount",
        "is_active",
        "created_at",
        "released_at",
    )
    list_filter = (
        "is_active",
        "created_at",
        "released_at",
        "merry",
    )
    search_fields = (
        "user__username",
        "user__phone",
        "merry__name",
        "loan__id",
    )
    ordering = ("-id",)
    readonly_fields = (
        "created_at",
        "released_at",
    )
    actions = [release_merry_holds]


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
        "reserved_amount",
        "loan_status_display",
        "context_display",
    )
    list_filter = (
        "accepted",
        "accepted_at",
        "loan__status",
        "loan__merry",
        "loan__group",
    )
    search_fields = (
        "guarantor__username",
        "guarantor__phone",
        "loan__borrower__username",
        "loan__borrower__phone",
    )
    ordering = ("-id",)
    actions = [accept_guarantors, unaccept_guarantors]

    def loan_status_display(self, obj):
        return obj.loan.status

    loan_status_display.short_description = "Loan Status"

    def context_display(self, obj):
        if obj.loan.merry_id:
            return f"Merry: {obj.loan.merry}"
        if obj.loan.group_id:
            return f"Group: {obj.loan.group}"
        return "-"

    context_display.short_description = "Context"


# =========================================================
# LOAN INSTALLMENT ADMIN
# =========================================================
@admin.register(LoanInstallment)
class LoanInstallmentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "loan",
        "installment_no",
        "due_date",
        "principal_due",
        "interest_due",
        "total_due",
        "late_fee",
        "paid_amount",
        "is_paid",
        "is_overdue_display",
    )
    list_filter = (
        "is_paid",
        "due_date",
        "loan__status",
    )
    search_fields = (
        "loan__borrower__username",
        "loan__borrower__phone",
        "loan__id",
    )
    ordering = ("loan", "installment_no")
    actions = [mark_installments_paid, mark_installments_unpaid]

    def is_overdue_display(self, obj):
        return (not obj.is_paid) and (obj.due_date < timezone.now().date())

    is_overdue_display.boolean = True
    is_overdue_display.short_description = "Overdue"


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
        "reference",
        "loan__id",
    )
    ordering = ("-paid_at",)
    readonly_fields = ("paid_at",)

    def borrower_display(self, obj):
        return obj.loan.borrower

    borrower_display.short_description = "Borrower"