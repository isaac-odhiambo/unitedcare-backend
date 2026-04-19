from django.contrib import admin, messages

from .models import (
    Group,
    GroupMembership,
    GroupJoinRequest,
    GroupFund,
    GroupMemberShare,
    GroupContribution,
    GroupShareHold,
    GroupDependant,
)
from .services import release_group_share_for_loan


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
@admin.action(description="Release selected share holds safely")
def release_share_holds(modeladmin, request, queryset):
    active_qs = queryset.filter(is_active=True).select_related("group", "user")
    if not active_qs.exists():
        modeladmin.message_user(
            request,
            "No active share holds selected.",
            level=messages.WARNING,
        )
        return

    released_count = 0
    processed_pairs = set()

    for hold in active_qs:
        key = (hold.group_id, int(hold.loan_id))
        if key in processed_pairs:
            continue

        pair_count = active_qs.filter(
            group_id=hold.group_id,
            loan_id=hold.loan_id,
            is_active=True,
        ).count()

        try:
            release_group_share_for_loan(
                group_id=hold.group_id,
                loan_id=int(hold.loan_id),
            )
            processed_pairs.add(key)
            released_count += pair_count
        except Exception as exc:
            modeladmin.message_user(
                request,
                f"Failed to release holds for group={hold.group_id}, loan={hold.loan_id}: {exc}",
                level=messages.ERROR,
            )

    if released_count:
        modeladmin.message_user(
            request,
            f"{released_count} share hold(s) released safely.",
            level=messages.SUCCESS,
        )


# =========================================================
# DEPENDANT ACTIONS
# =========================================================
@admin.action(description="Activate selected dependants")
def activate_dependants(modeladmin, request, queryset):
    updated = queryset.update(is_active=True)
    modeladmin.message_user(
        request,
        f"{updated} dependant(s) activated.",
        level=messages.SUCCESS,
    )


@admin.action(description="Deactivate selected dependants")
def deactivate_dependants(modeladmin, request, queryset):
    updated = queryset.update(is_active=False)
    modeladmin.message_user(
        request,
        f"{updated} dependant(s) deactivated.",
        level=messages.WARNING,
    )


@admin.action(description="Set selected dependants as spouse")
def make_dependants_spouse(modeladmin, request, queryset):
    updated = queryset.update(relationship="SPOUSE")
    modeladmin.message_user(
        request,
        f"{updated} dependant(s) updated to SPOUSE.",
        level=messages.SUCCESS,
    )


@admin.action(description="Set selected dependants as child")
def make_dependants_child(modeladmin, request, queryset):
    updated = queryset.update(relationship="CHILD")
    modeladmin.message_user(
        request,
        f"{updated} dependant(s) updated to CHILD.",
        level=messages.SUCCESS,
    )


@admin.action(description="Set selected dependants as sibling")
def make_dependants_sibling(modeladmin, request, queryset):
    updated = queryset.update(relationship="SIBLING")
    modeladmin.message_user(
        request,
        f"{updated} dependant(s) updated to SIBLING.",
        level=messages.SUCCESS,
    )


@admin.action(description="Set selected dependants as parent")
def make_dependants_parent(modeladmin, request, queryset):
    updated = queryset.update(relationship="PARENT")
    modeladmin.message_user(
        request,
        f"{updated} dependant(s) updated to PARENT.",
        level=messages.SUCCESS,
    )


@admin.action(description="Set selected dependants as other")
def make_dependants_other(modeladmin, request, queryset):
    updated = queryset.update(relationship="OTHER")
    modeladmin.message_user(
        request,
        f"{updated} dependant(s) updated to OTHER.",
        level=messages.SUCCESS,
    )


# =========================================================
# READ-ONLY INLINE MIXINS
# =========================================================
class ReadOnlyTabularInline(admin.TabularInline):
    extra = 0
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ReadOnlyStackedInline(admin.StackedInline):
    extra = 0
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# =========================================================
# INLINES
# =========================================================
class GroupFundInline(ReadOnlyStackedInline):
    model = GroupFund
    max_num = 1
    fields = ("balance", "reserved_amount", "available_balance_display", "created_at")
    readonly_fields = ("balance", "reserved_amount", "available_balance_display", "created_at")

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


class GroupMemberShareInline(ReadOnlyTabularInline):
    model = GroupMemberShare
    fields = (
        "user",
        "total_contributed",
        "reserved_share",
        "available_share_display",
        "updated_at",
    )
    readonly_fields = (
        "user",
        "total_contributed",
        "reserved_share",
        "available_share_display",
        "updated_at",
    )

    def available_share_display(self, obj):
        return obj.available_share if obj else "—"

    available_share_display.short_description = "Available Share"


class GroupContributionInline(ReadOnlyTabularInline):
    model = GroupContribution
    fields = ("user", "amount", "source", "reference", "note", "created_at")
    readonly_fields = ("user", "amount", "source", "reference", "note", "created_at")


class GroupShareHoldInline(ReadOnlyTabularInline):
    model = GroupShareHold
    fields = ("user", "loan_id", "amount", "is_active", "created_at", "released_at")
    readonly_fields = ("user", "loan_id", "amount", "is_active", "created_at", "released_at")


class GroupDependantInline(admin.TabularInline):
    model = GroupDependant
    extra = 0
    fields = (
        "name",
        "relationship",
        "date_of_birth",
        "is_active",
        "created_at",
    )
    readonly_fields = ("created_at",)
    show_change_link = True


# =========================================================
# GROUP ADMIN
# =========================================================
@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "payment_code",
        "group_type",
        "join_policy",
        "visibility",
        "is_active",
        "member_count",
        "active_member_count",
        "dependant_count",
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
        "payment_code",
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
                "payment_code",
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

    def dependant_count(self, obj):
        return GroupDependant.objects.filter(
            membership__group=obj,
            is_active=True,
        ).count()

    dependant_count.short_description = "Active Dependants"

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
        "dependant_count",
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
    inlines = [GroupDependantInline]

    def dependant_count(self, obj):
        return obj.dependants.filter(is_active=True).count()

    dependant_count.short_description = "Active Dependants"


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
    readonly_fields = (
        "group",
        "balance",
        "reserved_amount",
        "available_balance_display",
        "created_at",
    )

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

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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
    readonly_fields = (
        "group",
        "user",
        "total_contributed",
        "reserved_share",
        "available_share_display",
        "updated_at",
    )

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

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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
    readonly_fields = (
        "group",
        "user",
        "amount",
        "source",
        "reference",
        "note",
        "created_at",
    )

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

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return True

    def has_delete_permission(self, request, obj=None):
        return False


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
        "=loan_id",
    )
    ordering = ("-created_at",)
    readonly_fields = (
        "group",
        "user",
        "loan_id",
        "amount",
        "is_active",
        "created_at",
        "released_at",
    )
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

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# =========================================================
# GROUP DEPENDANT ADMIN
# =========================================================
@admin.register(GroupDependant)
class GroupDependantAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "relationship",
        "membership",
        "user_display",
        "group_display",
        "is_active",
        "created_at",
        "updated_at",
    )
    list_filter = (
        "relationship",
        "is_active",
        "created_at",
        "updated_at",
        "membership__group",
    )
    search_fields = (
        "name",
        "note",
        "membership__group__name",
        "membership__user__username",
        "membership__user__phone",
        "membership__user__email",
    )
    ordering = ("-id",)
    readonly_fields = ("created_at", "updated_at")
    actions = [
        activate_dependants,
        deactivate_dependants,
        make_dependants_spouse,
        make_dependants_child,
        make_dependants_sibling,
        make_dependants_parent,
        make_dependants_other,
    ]
    autocomplete_fields = ("membership",)

    fieldsets = (
        ("Dependant", {
            "fields": (
                "membership",
                "name",
                "relationship",
                "date_of_birth",
                "note",
                "is_active",
            )
        }),
        ("Dates", {
            "fields": (
                "created_at",
                "updated_at",
            )
        }),
    )

    def user_display(self, obj):
        return obj.membership.user

    user_display.short_description = "User"

    def group_display(self, obj):
        return obj.membership.group

    group_display.short_description = "Group"