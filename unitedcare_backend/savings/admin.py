from django.contrib import admin, messages
from django.utils import timezone

from .models import SavingsAccount, SavingsTransaction


# =========================================================
# ACCOUNT ACTIONS
# =========================================================
@admin.action(description="Activate selected savings accounts")
def activate_accounts(modeladmin, request, queryset):
    updated = queryset.update(is_active=True)
    modeladmin.message_user(
        request,
        f"{updated} account(s) activated.",
        level=messages.SUCCESS,
    )


@admin.action(description="Deactivate selected savings accounts")
def deactivate_accounts(modeladmin, request, queryset):
    updated = queryset.update(is_active=False)
    modeladmin.message_user(
        request,
        f"{updated} account(s) deactivated.",
        level=messages.WARNING,
    )


# =========================================================
# INLINES
# =========================================================
class SavingsTransactionInline(admin.TabularInline):
    model = SavingsTransaction
    extra = 0
    fields = (
        "txn_type",
        "amount",
        "reference",
        "note",
        "created_at",
    )
    readonly_fields = ("created_at",)
    show_change_link = True


# =========================================================
# SAVINGS ACCOUNT ADMIN
# =========================================================
@admin.register(SavingsAccount)
class SavingsAccountAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "name",
        "account_type",
        "balance",
        "reserved_amount",
        "available_balance_display",
        "can_withdraw_now_display",
        "is_active",
        "locked_until",
        "target_amount",
        "target_deadline",
        "created_at",
    )

    list_filter = (
        "account_type",
        "is_active",
        "created_at",
        "locked_until",
        "target_deadline",
    )

    search_fields = (
        "user__username",
        "user__phone",
        "name",
    )

    ordering = ("user_id", "name")

    readonly_fields = (
        "available_balance_display",
        "can_withdraw_now_display",
        "created_at",
    )

    actions = [
        activate_accounts,
        deactivate_accounts,
    ]

    inlines = [SavingsTransactionInline]

    list_per_page = 25
    date_hierarchy = "created_at"

    fieldsets = (
        ("Owner", {
            "fields": (
                "user",
                "name",
                "account_type",
                "is_active",
            )
        }),
        ("Balances", {
            "fields": (
                "balance",
                "reserved_amount",
                "available_balance_display",
                "can_withdraw_now_display",
            )
        }),
        ("Fixed Savings", {
            "fields": (
                "locked_until",
            )
        }),
        ("Target Savings", {
            "fields": (
                "target_amount",
                "target_deadline",
            )
        }),
        ("Meta", {
            "fields": (
                "created_at",
            )
        }),
    )

    def available_balance_display(self, obj):
        return obj.available_balance

    available_balance_display.short_description = "Available Balance"

    def can_withdraw_now_display(self, obj):
        return obj.can_withdraw_now()

    can_withdraw_now_display.boolean = True
    can_withdraw_now_display.short_description = "Withdraw Now"


# =========================================================
# SAVINGS TRANSACTION ADMIN
# =========================================================
@admin.register(SavingsTransaction)
class SavingsTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "account",
        "account_user_display",
        "txn_type",
        "amount",
        "reference",
        "short_note",
        "created_at",
    )

    list_filter = (
        "txn_type",
        "created_at",
        "account__account_type",
        "account__is_active",
    )

    search_fields = (
        "account__name",
        "account__user__username",
        "account__user__phone",
        "reference",
        "note",
    )

    ordering = ("-created_at",)

    readonly_fields = ("created_at",)

    list_per_page = 25
    date_hierarchy = "created_at"

    fieldsets = (
        ("Transaction", {
            "fields": (
                "account",
                "txn_type",
                "amount",
                "reference",
                "note",
                "created_at",
            )
        }),
    )

    def account_user_display(self, obj):
        return obj.account.user

    account_user_display.short_description = "User"

    def short_note(self, obj):
        if not obj.note:
            return ""
        return obj.note[:50]

    short_note.short_description = "Note"