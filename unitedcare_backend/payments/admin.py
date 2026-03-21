from django.contrib import admin, messages
from django.utils import timezone

from .models import (
    MpesaConfig,
    MpesaTransaction,
    PaymentLedger,
    TransactionFeeConfig,
    WithdrawalRequest,
)


# =========================================================
# WITHDRAWAL ADMIN ACTIONS
# =========================================================
@admin.action(description="Approve selected withdrawal requests")
def approve_withdrawals(modeladmin, request, queryset):
    count = 0

    for wd in queryset:
        if wd.status != "PENDING":
            continue

        wd.status = "APPROVED"
        wd.approved_by = request.user
        wd.approved_at = timezone.now()
        wd.rejected_by = None
        wd.rejected_at = None
        wd.rejection_reason = ""
        wd.save(
            update_fields=[
                "status",
                "approved_by",
                "approved_at",
                "rejected_by",
                "rejected_at",
                "rejection_reason",
            ]
        )
        count += 1

    modeladmin.message_user(
        request,
        f"{count} withdrawal request(s) approved.",
        level=messages.SUCCESS,
    )


@admin.action(description="Reject selected withdrawal requests")
def reject_withdrawals(modeladmin, request, queryset):
    count = 0

    for wd in queryset:
        if wd.status not in ("PENDING", "APPROVED"):
            continue

        wd.status = "REJECTED"
        wd.rejected_by = request.user
        wd.rejected_at = timezone.now()
        if not wd.rejection_reason:
            wd.rejection_reason = "Rejected by admin"
        wd.save(
            update_fields=[
                "status",
                "rejected_by",
                "rejected_at",
                "rejection_reason",
            ]
        )
        count += 1

    modeladmin.message_user(
        request,
        f"{count} withdrawal request(s) rejected.",
        level=messages.WARNING,
    )


@admin.action(description="Mark selected withdrawal requests as processing")
def mark_withdrawals_processing(modeladmin, request, queryset):
    updated = queryset.filter(status="APPROVED").update(status="PROCESSING")
    modeladmin.message_user(
        request,
        f"{updated} withdrawal request(s) marked as PROCESSING.",
        level=messages.SUCCESS,
    )


@admin.action(description="Mark selected withdrawal requests as paid")
def mark_withdrawals_paid(modeladmin, request, queryset):
    updated = queryset.filter(status__in=("APPROVED", "PROCESSING")).update(status="PAID")
    modeladmin.message_user(
        request,
        f"{updated} withdrawal request(s) marked as PAID.",
        level=messages.SUCCESS,
    )


@admin.action(description="Mark selected withdrawal requests as failed")
def mark_withdrawals_failed(modeladmin, request, queryset):
    updated = queryset.filter(status__in=("APPROVED", "PROCESSING")).update(status="FAILED")
    modeladmin.message_user(
        request,
        f"{updated} withdrawal request(s) marked as FAILED.",
        level=messages.WARNING,
    )


@admin.action(description="Cancel selected withdrawal requests")
def cancel_withdrawals(modeladmin, request, queryset):
    updated = queryset.exclude(status__in=("PAID", "REJECTED")).update(status="CANCELLED")
    modeladmin.message_user(
        request,
        f"{updated} withdrawal request(s) cancelled.",
        level=messages.WARNING,
    )


# =========================================================
# MPESA ADMIN ACTIONS
# =========================================================
@admin.action(description="Mark selected Mpesa transactions as pending")
def mark_mpesa_pending(modeladmin, request, queryset):
    updated = queryset.update(status="PENDING")
    modeladmin.message_user(
        request,
        f"{updated} Mpesa transaction(s) marked as PENDING.",
        level=messages.SUCCESS,
    )


@admin.action(description="Mark selected Mpesa transactions as success")
def mark_mpesa_success(modeladmin, request, queryset):
    updated = queryset.update(status="SUCCESS")
    modeladmin.message_user(
        request,
        f"{updated} Mpesa transaction(s) marked as SUCCESS.",
        level=messages.SUCCESS,
    )


@admin.action(description="Mark selected Mpesa transactions as failed")
def mark_mpesa_failed(modeladmin, request, queryset):
    updated = queryset.update(status="FAILED")
    modeladmin.message_user(
        request,
        f"{updated} Mpesa transaction(s) marked as FAILED.",
        level=messages.WARNING,
    )


@admin.action(description="Mark selected Mpesa transactions as cancelled")
def mark_mpesa_cancelled(modeladmin, request, queryset):
    updated = queryset.update(status="CANCELLED")
    modeladmin.message_user(
        request,
        f"{updated} Mpesa transaction(s) marked as CANCELLED.",
        level=messages.WARNING,
    )


@admin.action(description="Mark selected Mpesa transactions for manual review")
def mark_mpesa_manual_review(modeladmin, request, queryset):
    updated = queryset.update(
        allocation_status="MANUAL_REVIEW",
        allocation_notes="Marked for manual review by admin",
    )
    modeladmin.message_user(
        request,
        f"{updated} Mpesa transaction(s) marked for MANUAL REVIEW.",
        level=messages.WARNING,
    )


@admin.action(description="Mark selected Mpesa transactions as manually allocated")
def mark_mpesa_manually_allocated(modeladmin, request, queryset):
    count = 0
    for tx in queryset:
        tx.allocation_status = "MANUALLY_ALLOCATED"
        tx.allocated_by = request.user
        tx.allocated_at = timezone.now()
        if not tx.allocation_notes:
            tx.allocation_notes = "Manually allocated by admin"
        tx.save(
            update_fields=[
                "allocation_status",
                "allocated_by",
                "allocated_at",
                "allocation_notes",
            ]
        )
        count += 1

    modeladmin.message_user(
        request,
        f"{count} Mpesa transaction(s) marked as MANUALLY ALLOCATED.",
        level=messages.SUCCESS,
    )


@admin.action(description="Mark selected Mpesa transactions as invalid reference")
def mark_mpesa_invalid_reference(modeladmin, request, queryset):
    updated = queryset.update(
        allocation_status="INVALID_REFERENCE",
        allocation_notes="Marked invalid by admin",
    )
    modeladmin.message_user(
        request,
        f"{updated} Mpesa transaction(s) marked as INVALID REFERENCE.",
        level=messages.WARNING,
    )


# =========================================================
# INLINE: LEDGER ON MPESA TX
# =========================================================
class PaymentLedgerInline(admin.TabularInline):
    model = PaymentLedger
    extra = 0
    fields = (
        "user",
        "entry_type",
        "category",
        "amount",
        "narration",
        "reference",
        "created_at",
    )
    readonly_fields = (
        "user",
        "entry_type",
        "category",
        "amount",
        "narration",
        "reference",
        "created_at",
    )
    can_delete = False


# =========================================================
# MPESA CONFIG ADMIN
# =========================================================
@admin.register(MpesaConfig)
class MpesaConfigAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "paybill_number",
        "business_number",
        "till_number",
        "is_active",
        "is_paybill_enabled",
        "is_till_enabled",
        "updated_at",
    )

    list_filter = (
        "is_active",
        "is_paybill_enabled",
        "is_till_enabled",
        "updated_at",
    )

    search_fields = (
        "name",
        "paybill_number",
        "business_number",
        "till_number",
        "notes",
    )

    ordering = ("name",)
    readonly_fields = ("updated_at",)

    fieldsets = (
        ("Identity", {
            "fields": (
                "name",
                "is_active",
            )
        }),
        ("Paybill / Business", {
            "fields": (
                "paybill_number",
                "business_number",
                "is_paybill_enabled",
            )
        }),
        ("Till", {
            "fields": (
                "till_number",
                "is_till_enabled",
            )
        }),
        ("Notes", {
            "fields": (
                "notes",
            )
        }),
        ("Timestamp", {
            "fields": (
                "updated_at",
            )
        }),
    )


# =========================================================
# TRANSACTION FEE CONFIG ADMIN
# =========================================================
@admin.register(TransactionFeeConfig)
class TransactionFeeConfigAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "purpose",
        "fixed_fee",
        "percentage_fee",
        "is_active",
        "updated_at",
    )
    list_filter = ("purpose", "is_active", "updated_at")
    search_fields = ("purpose",)
    ordering = ("purpose",)
    readonly_fields = ("updated_at",)

    fieldsets = (
        ("Configuration", {
            "fields": (
                "purpose",
                "fixed_fee",
                "percentage_fee",
                "is_active",
                "updated_at",
            )
        }),
    )


# =========================================================
# MPESA TRANSACTION ADMIN
# =========================================================
@admin.register(MpesaTransaction)
class MpesaTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "phone",
        "matched_user_phone",
        "channel",
        "payment_method",
        "origin",
        "direction",
        "purpose",
        "reference",
        "matched_reference_type",
        "amount",
        "status",
        "allocation_status",
        "allocated_by",
        "mpesa_receipt_number",
        "ledger_posted",
        "created_at",
    )

    list_filter = (
        "status",
        "allocation_status",
        "channel",
        "payment_method",
        "origin",
        "direction",
        "purpose",
        "matched_reference_type",
        "ledger_posted",
        "created_at",
        "allocated_at",
        "allocated_by",
        "callback_received_at",
    )

    search_fields = (
        "phone",
        "matched_user_phone",
        "reference",
        "external_reference_raw",
        "checkout_request_id",
        "conversation_id",
        "merchant_request_id",
        "originator_conversation_id",
        "mpesa_receipt_number",
        "result_desc",
        "allocation_notes",
        "user__phone",
        "user__username",
        "allocated_by__username",
    )

    ordering = ("-id",)

    readonly_fields = (
        "created_at",
        "updated_at",
        "allocated_at",
        "callback_received_at",
    )

    actions = [
        mark_mpesa_pending,
        mark_mpesa_success,
        mark_mpesa_failed,
        mark_mpesa_cancelled,
        mark_mpesa_manual_review,
        mark_mpesa_manually_allocated,
        mark_mpesa_invalid_reference,
    ]

    inlines = [PaymentLedgerInline]

    fieldsets = (
        ("Core", {
            "fields": (
                "user",
                "phone",
                "matched_user_phone",
                "direction",
                "channel",
                "payment_method",
                "origin",
                "purpose",
                "status",
            )
        }),
        ("Amounts", {
            "fields": (
                "amount",
                "base_amount",
                "transaction_fee",
            )
        }),
        ("Business Reference", {
            "fields": (
                "reference",
                "external_reference_raw",
                "matched_reference_type",
                "ledger_posted",
            )
        }),
        ("Allocation Workflow", {
            "fields": (
                "allocation_status",
                "allocation_notes",
                "allocated_by",
                "allocated_at",
            )
        }),
        ("STK Identifiers", {
            "fields": (
                "merchant_request_id",
                "checkout_request_id",
            )
        }),
        ("B2C Identifiers", {
            "fields": (
                "conversation_id",
                "originator_conversation_id",
            )
        }),
        ("Result", {
            "fields": (
                "result_code",
                "result_desc",
                "mpesa_receipt_number",
                "transaction_date",
                "callback_received_at",
            )
        }),
        ("Target Object", {
            "fields": (
                "target_content_type",
                "target_object_id",
            )
        }),
        ("Payloads / Audit", {
            "fields": (
                "request_payload",
                "callback_payload",
            )
        }),
        ("Timestamps", {
            "fields": (
                "created_at",
                "updated_at",
            )
        }),
    )

    def save_model(self, request, obj, form, change):
        if getattr(obj, "allocation_status", "") == "MANUALLY_ALLOCATED" and not obj.allocated_by:
            obj.allocated_by = request.user
        if getattr(obj, "allocation_status", "") in ("AUTO_ALLOCATED", "MANUALLY_ALLOCATED", "PARTIALLY_ALLOCATED") and not obj.allocated_at:
            obj.allocated_at = timezone.now()
        super().save_model(request, obj, form, change)


# =========================================================
# PAYMENT LEDGER ADMIN
# =========================================================
@admin.register(PaymentLedger)
class PaymentLedgerAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "entry_type",
        "category",
        "amount",
        "reference",
        "mpesa_tx",
        "short_narration",
        "created_at",
    )

    list_filter = (
        "entry_type",
        "category",
        "created_at",
    )

    search_fields = (
        "user__phone",
        "user__username",
        "reference",
        "narration",
        "mpesa_tx__mpesa_receipt_number",
        "mpesa_tx__checkout_request_id",
        "mpesa_tx__conversation_id",
        "mpesa_tx__external_reference_raw",
    )

    ordering = ("-id",)

    readonly_fields = (
        "created_at",
    )

    fieldsets = (
        ("Core", {
            "fields": (
                "user",
                "entry_type",
                "category",
                "amount",
            )
        }),
        ("Narration / Reference", {
            "fields": (
                "narration",
                "reference",
            )
        }),
        ("Relations", {
            "fields": (
                "mpesa_tx",
                "target_content_type",
                "target_object_id",
            )
        }),
        ("Timestamp", {
            "fields": ("created_at",)
        }),
    )

    def short_narration(self, obj):
        if not obj.narration:
            return ""
        return obj.narration[:50]

    short_narration.short_description = "Narration"


# =========================================================
# WITHDRAWAL REQUEST ADMIN
# =========================================================
@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "phone",
        "amount",
        "source",
        "status",
        "approved_by",
        "approved_at",
        "mpesa_tx",
        "is_final",
        "created_at",
    )

    list_filter = (
        "status",
        "source",
        "created_at",
        "approved_at",
        "rejected_at",
    )

    search_fields = (
        "user__phone",
        "user__username",
        "phone",
        "rejection_reason",
        "mpesa_tx__mpesa_receipt_number",
        "mpesa_tx__conversation_id",
        "mpesa_tx__reference",
        "mpesa_tx__external_reference_raw",
    )

    ordering = ("-id",)

    readonly_fields = (
        "approved_at",
        "rejected_at",
        "created_at",
        "updated_at",
        "is_final_display",
        "can_withdraw_merry_display",
    )

    actions = [
        approve_withdrawals,
        reject_withdrawals,
        mark_withdrawals_processing,
        mark_withdrawals_paid,
        mark_withdrawals_failed,
        cancel_withdrawals,
    ]

    fieldsets = (
        ("Request", {
            "fields": (
                "user",
                "phone",
                "amount",
                "source",
                "status",
            )
        }),
        ("Target Object", {
            "fields": (
                "target_content_type",
                "target_object_id",
            )
        }),
        ("Approval", {
            "fields": (
                "approved_by",
                "approved_at",
            )
        }),
        ("Rejection", {
            "fields": (
                "rejected_by",
                "rejected_at",
                "rejection_reason",
            )
        }),
        ("Mpesa Link", {
            "fields": (
                "mpesa_tx",
            )
        }),
        ("Checks", {
            "fields": (
                "is_final_display",
                "can_withdraw_merry_display",
            )
        }),
        ("Timestamps", {
            "fields": (
                "created_at",
                "updated_at",
            )
        }),
    )

    def is_final_display(self, obj):
        return obj.is_final

    is_final_display.boolean = True
    is_final_display.short_description = "Final"

    def can_withdraw_merry_display(self, obj):
        return obj.can_withdraw_merry

    can_withdraw_merry_display.boolean = True
    can_withdraw_merry_display.short_description = "Merry Allowed"

    def save_model(self, request, obj, form, change):
        if obj.status == "APPROVED" and not obj.approved_by:
            obj.approved_by = request.user
            obj.approved_at = timezone.now()

        if obj.status == "REJECTED" and not obj.rejected_by:
            obj.rejected_by = request.user
            obj.rejected_at = timezone.now()
            if not obj.rejection_reason:
                obj.rejection_reason = "Rejected by admin"

        super().save_model(request, obj, form, change)