# groups/admin.py
from django.contrib import admin, messages
from django.utils import timezone

from .models import (
    Group,
    GroupMembership,
    GroupJoinRequest,
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


@admin.action(description="Set selected memberships as treasurers")
def make_treasurers(modeladmin, request, queryset):
    updated = queryset.update(role="TREASURER")
    modeladmin.message_user(
        request,
        f"{updated} membership(s) set as TREASURER.",
        level=messages.SUCCESS,
    )


@admin.action(description="Set selected memberships as secretaries")
def make_secretaries(modeladmin, request, queryset):
    updated = queryset.update(role="SECRETARY")
    modeladmin.message_user(
        request,
        f"{updated} membership(s) set as SECRETARY.",
        level=messages.SUCCESS,
    )


# =========================================================
# JOIN REQUEST ACTIONS
# =========================================================
@admin.action(description="Approve selected join requests")
def approve_join_requests(modeladmin, request, queryset):
    count = 0
    for obj in queryset.select_related("group", "user").filter(status="PENDING"):
        try:
            obj.approve(admin_user=request.user)
            count += 1
        except Exception as exc:
            modeladmin.message_user(
                request,
                f"Failed to approve request #{obj.id}: {exc}",
                level=messages.ERROR,
            )

    if count:
        modeladmin.message_user(
            request,
            f"{count} join request(s) approved.",
            level=messages.SUCCESS,
        )


@admin.action(description="Reject selected join requests")
def reject_join_requests(modeladmin, request, queryset):
    count = 0
    for obj in queryset.filter(status="PENDING"):
        try:
            obj.reject(admin_user=request.user, note="Rejected by admin action")
            count += 1
        except Exception as exc:
            modeladmin.message_user(
                request,
                f"Failed to reject request #{obj.id}: {exc}",
                level=messages.ERROR,
            )

    if count:
        modeladmin.message_user(
            request,
            f"{count} join request(s) rejected.",
            level=messages.WARNING,
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
class GroupFundInline(admin.StackedInline):
    model = GroupFund
    extra = 0
    max_num = 1
    can_delete = False
    fields = ("balance", "reserved_amount", "available_balance_display", "created_at")
    readonly_fields = ("available_balance_display", "created_at")

    def available_balance_display(self, obj):
        return obj.available_balance if obj else "—"

    available_balance_display.short_description = "Available Balance"


class GroupMembershipInline(admin.TabularInline):
    model = GroupMembership
    extra = 0
    fields = ("user", "role", "is_active", "joined_at")
    readonly_fields = ("joined_at",)


class GroupJoinRequestInline(admin.TabularInline):
    model = GroupJoinRequest
    extra = 0
    fields = ("user", "status", "note", "reviewed_by", "reviewed_at", "created_at")
    readonly_fields = ("reviewed_by", "reviewed_at", "created_at")


class GroupMemberShareInline(admin.TabularInline):
    model = GroupMemberShare
    extra = 0
    fields = ("user", "total_contributed", "reserved_share", "available_share_display", "updated_at")
    readonly_fields = ("available_share_display", "updated_at")

    def available_share_display(self, obj):
        return obj.available_share if obj else "—"

    available_share_display.short_description = "Available Share"


class GroupContributionInline(admin.TabularInline):
    model = GroupContribution
    extra = 0
    fields = ("user", "amount", "source", "reference", "note", "created_at")
    readonly_fields = ("created_at",)


class GroupShareHoldInline(admin.TabularInline):
    model = GroupShareHold
    extra = 0
    fields = ("user", "loan_id", "amount", "is_active", "created_at", "released_at")
    readonly_fields = ("created_at", "released_at")


# =========================================================
# GROUP ADMIN
# =========================================================
@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "group_type",
        "join_policy",
        "visibility",
        "is_active",
        "member_count",
        "active_member_count",
        "max_members",
        "fund_balance_display",
        "created_by",
        "created_at",
    )
    list_filter = (
        "group_type",
        "visibility",
        "join_policy",
        "is_active",
        "requires_contributions",
        "created_at",
    )
    search_fields = (
        "name",
        "description",
        "objective",
        "created_by__username",
        "created_by__phone",
    )
    ordering = ("-id",)
    readonly_fields = ("created_at", "updated_at")
    inlines = [
        GroupFundInline,
        GroupMembershipInline,
        GroupJoinRequestInline,
        GroupMemberShareInline,
        GroupContributionInline,
        GroupShareHoldInline,
    ]

    fieldsets = (
        ("Core", {
            "fields": (
                "name",
                "group_type",
                "description",
                "objective",
                "created_by",
            )
        }),
        ("Access & Status", {
            "fields": (
                "visibility",
                "join_policy",
                "is_active",
                "max_members",
            )
        }),
        ("Contribution Rules", {
            "fields": (
                "requires_contributions",
                "contribution_amount",
                "contribution_frequency",
            )
        }),
        ("Dates", {
            "fields": (
                "created_at",
                "updated_at",
            )
        }),
    )

    def member_count(self, obj):
        return obj.memberships.count()

    member_count.short_description = "All Members"

    def active_member_count(self, obj):
        return obj.memberships.filter(is_active=True).count()

    active_member_count.short_description = "Active Members"

    def fund_balance_display(self, obj):
        fund = getattr(obj, "fund", None)
        return getattr(fund, "balance", "—")

    fund_balance_display.short_description = "Fund Balance"


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
        "group",
        "joined_at",
    )
    search_fields = (
        "group__name",
        "user__username",
        "user__phone",
        "user__email",
    )
    ordering = ("-id",)
    readonly_fields = ("joined_at",)
    actions = [
        activate_memberships,
        deactivate_memberships,
        make_group_admins,
        make_group_members,
        make_treasurers,
        make_secretaries,
    ]


# =========================================================
# GROUP JOIN REQUEST ADMIN
# =========================================================
@admin.register(GroupJoinRequest)
class GroupJoinRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "group",
        "user",
        "status",
        "reviewed_by",
        "reviewed_at",
        "created_at",
    )
    list_filter = (
        "status",
        "group",
        "created_at",
        "reviewed_at",
    )
    search_fields = (
        "group__name",
        "user__username",
        "user__phone",
        "note",
    )
    ordering = ("-id",)
    readonly_fields = ("reviewed_by", "reviewed_at", "created_at")
    actions = [approve_join_requests, reject_join_requests]

    fieldsets = (
        ("Request", {
            "fields": (
                "group",
                "user",
                "note",
                "status",
            )
        }),
        ("Review", {
            "fields": (
                "reviewed_by",
                "reviewed_at",
                "created_at",
            )
        }),
    )


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
        "user__email",
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
        "source",
        "reference",
        "note",
        "created_at",
    )
    list_filter = (
        "group",
        "source",
        "created_at",
    )
    search_fields = (
        "group__name",
        "user__username",
        "user__phone",
        "user__email",
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
                "source",
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
        "user__email",
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
