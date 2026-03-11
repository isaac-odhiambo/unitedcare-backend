from django.contrib import admin, messages
from django.utils import timezone

from .models import (
    Group,
    GroupMembership,
    GroupFund,
    GroupMemberShare,
    GroupContribution,
    GroupShareHold,
)


# =========================================================
# MEMBERSHIP ACTIONS
# =========================================================
@admin.action(description="Activate selected memberships")
def activate_memberships(modeladmin, request, queryset):
    updated = queryset.update(is_active=True)
    modeladmin.message_user(
        request,
        f"{updated} membership(s) activated.",
        level=messages.SUCCESS,
    )


@admin.action(description="Deactivate selected memberships")
def deactivate_memberships(modeladmin, request, queryset):
    updated = queryset.update(is_active=False)
    modeladmin.message_user(
        request,
        f"{updated} membership(s) deactivated.",
        level=messages.WARNING,
    )


@admin.action(description="Set selected memberships as group admins")
def make_group_admins(modeladmin, request, queryset):
    updated = queryset.update(role="ADMIN")
    modeladmin.message_user(
        request,
        f"{updated} membership(s) set as ADMIN.",
        level=messages.SUCCESS,
    )


@admin.action(description="Set selected memberships as group members")
def make_group_members(modeladmin, request, queryset):
    updated = queryset.update(role="MEMBER")
    modeladmin.message_user(
        request,
        f"{updated} membership(s) set as MEMBER.",
        level=messages.SUCCESS,
    )


# =========================================================
# SHARE HOLD ACTIONS
# =========================================================
@admin.action(description="Release selected share holds")
def release_share_holds(modeladmin, request, queryset):
    count = 0
    for hold in queryset.filter(is_active=True):
        hold.release()
        count += 1

    modeladmin.message_user(
        request,
        f"{count} share hold(s) released.",
        level=messages.SUCCESS,
    )


# =========================================================
# INLINES
# =========================================================
class GroupMembershipInline(admin.TabularInline):
    model = GroupMembership
    extra = 0
    fields = ("user", "role", "is_active", "joined_at")
    readonly_fields = ("joined_at",)


class GroupContributionInline(admin.TabularInline):
    model = GroupContribution
    extra = 0
    fields = ("user", "amount", "reference", "note", "created_at")
    readonly_fields = ("created_at",)


class GroupMemberShareInline(admin.TabularInline):
    model = GroupMemberShare
    extra = 0
    fields = ("user", "total_contributed", "reserved_share", "available_share_display", "updated_at")
    readonly_fields = ("available_share_display", "updated_at")

    def available_share_display(self, obj):
        return obj.available_share

    available_share_display.short_description = "Available Share"


class GroupShareHoldInline(admin.TabularInline):
    model = GroupShareHold
    extra = 0
    fields = ("user", "loan_id", "amount", "is_active", "created_at", "released_at")
    readonly_fields = ("created_at", "released_at")


class GroupFundInline(admin.StackedInline):
    model = GroupFund
    extra = 0
    max_num = 1
    can_delete = False
    fields = ("balance", "reserved_amount", "available_balance_display", "created_at")
    readonly_fields = ("available_balance_display", "created_at")

    def available_balance_display(self, obj):
        return obj.available_balance

    available_balance_display.short_description = "Available Balance"


# =========================================================
# GROUP ADMIN
# =========================================================
@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "members_count",
        "active_members_count",
        "fund_balance_display",
        "fund_available_display",
        "created_at",
    )
    search_fields = ("name",)
    ordering = ("-id",)
    readonly_fields = ("created_at",)
    inlines = [
        GroupFundInline,
        GroupMembershipInline,
        GroupMemberShareInline,
        GroupContributionInline,
        GroupShareHoldInline,
    ]

    fieldsets = (
        ("Core", {
            "fields": (
                "name",
                "created_at",
            )
        }),
    )

    def members_count(self, obj):
        return obj.memberships.count()

    members_count.short_description = "Members"

    def active_members_count(self, obj):
        return obj.memberships.filter(is_active=True).count()

    active_members_count.short_description = "Active Members"

    def fund_balance_display(self, obj):
        fund = getattr(obj, "fund", None)
        return getattr(fund, "balance", "—")

    fund_balance_display.short_description = "Fund Balance"

    def fund_available_display(self, obj):
        fund = getattr(obj, "fund", None)
        return getattr(fund, "available_balance", "—")

    fund_available_display.short_description = "Available Fund"


# =========================================================
# GROUP MEMBERSHIP ADMIN
# =========================================================
@admin.register(GroupMembership)
class GroupMembershipAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "group",
        "user",
        "role",
        "is_active",
        "joined_at",
    )
    list_filter = (
        "role",
        "is_active",
        "joined_at",
        "group",
    )
    search_fields = (
        "group__name",
        "user__username",
        "user__phone",
    )
    ordering = ("-id",)
    readonly_fields = ("joined_at",)
    actions = [
        activate_memberships,
        deactivate_memberships,
        make_group_admins,
        make_group_members,
    ]


# =========================================================
# GROUP FUND ADMIN
# =========================================================
@admin.register(GroupFund)
class GroupFundAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "group",
        "balance",
        "reserved_amount",
        "available_balance_display",
        "created_at",
    )
    list_filter = ("created_at",)
    search_fields = ("group__name",)
    ordering = ("-id",)
    readonly_fields = ("available_balance_display", "created_at")

    fieldsets = (
        ("Fund", {
            "fields": (
                "group",
                "balance",
                "reserved_amount",
                "available_balance_display",
                "created_at",
            )
        }),
    )

    def available_balance_display(self, obj):
        return obj.available_balance

    available_balance_display.short_description = "Available Balance"


# =========================================================
# GROUP MEMBER SHARE ADMIN
# =========================================================
@admin.register(GroupMemberShare)
class GroupMemberShareAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "group",
        "user",
        "total_contributed",
        "reserved_share",
        "available_share_display",
        "updated_at",
    )
    list_filter = (
        "group",
        "updated_at",
    )
    search_fields = (
        "group__name",
        "user__username",
        "user__phone",
    )
    ordering = ("-updated_at",)
    readonly_fields = ("available_share_display", "updated_at")

    fieldsets = (
        ("Share", {
            "fields": (
                "group",
                "user",
                "total_contributed",
                "reserved_share",
                "available_share_display",
                "updated_at",
            )
        }),
    )

    def available_share_display(self, obj):
        return obj.available_share

    available_share_display.short_description = "Available Share"


# =========================================================
# GROUP CONTRIBUTION ADMIN
# =========================================================
@admin.register(GroupContribution)
class GroupContributionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "group",
        "user",
        "amount",
        "reference",
        "note",
        "created_at",
    )
    list_filter = (
        "group",
        "created_at",
    )
    search_fields = (
        "group__name",
        "user__username",
        "user__phone",
        "reference",
        "note",
    )
    ordering = ("-created_at",)
    readonly_fields = ("created_at",)

    fieldsets = (
        ("Contribution", {
            "fields": (
                "group",
                "user",
                "amount",
                "reference",
                "note",
                "created_at",
            )
        }),
    )


# =========================================================
# GROUP SHARE HOLD ADMIN
# =========================================================
@admin.register(GroupShareHold)
class GroupShareHoldAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "group",
        "user",
        "loan_id",
        "amount",
        "is_active",
        "created_at",
        "released_at",
    )
    list_filter = (
        "is_active",
        "group",
        "created_at",
        "released_at",
    )
    search_fields = (
        "group__name",
        "user__username",
        "user__phone",
        "loan_id",
    )
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "released_at")
    actions = [release_share_holds]

    fieldsets = (
        ("Hold", {
            "fields": (
                "group",
                "user",
                "loan_id",
                "amount",
                "is_active",
                "created_at",
                "released_at",
            )
        }),
    )

    def save_model(self, request, obj, form, change):
        if not obj.is_active and not obj.released_at:
            obj.released_at = timezone.now()
        super().save_model(request, obj, form, change)