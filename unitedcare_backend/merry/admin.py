from django.contrib import admin, messages
from django.db import transaction
from django.utils import timezone

from .models import (
    MerryGoRound,
    MerrySlotConfig,
    MerryMember,
    MerrySeat,
    MerryJoinRequest,
    MerryContributionDue,
    MerryPayment,
    MerryPaymentAllocation,
    MerryPayout,
)


# =========================================================
# ACTIONS: MERRY
# =========================================================
@admin.action(description="Generate dues for current period")
def generate_current_period_dues(modeladmin, request, queryset):
    total_created = 0

    for merry in queryset:
        try:
            created = merry.ensure_dues_for_period()
            total_created += created
        except Exception as e:
            modeladmin.message_user(
                request,
                f"{merry.name}: failed to generate dues - {e}",
                level=messages.ERROR,
            )

    modeladmin.message_user(
        request,
        f"{total_created} due record(s) created for selected merry groups.",
        level=messages.SUCCESS,
    )


# =========================================================
# ACTIONS: JOIN REQUESTS
# =========================================================
@admin.action(description="Approve selected join requests")
def approve_join_requests(modeladmin, request, queryset):
    count = 0

    for jr in queryset.select_related("merry", "user"):
        try:
            if jr.status == "PENDING":
                jr.approve(request.user)
                count += 1
        except Exception as e:
            modeladmin.message_user(
                request,
                f"JoinRequest #{jr.id} failed: {e}",
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

    for jr in queryset.select_related("merry", "user"):
        try:
            if jr.status == "PENDING":
                jr.reject(request.user, note="Rejected by admin")
                count += 1
        except Exception as e:
            modeladmin.message_user(
                request,
                f"JoinRequest #{jr.id} failed: {e}",
                level=messages.ERROR,
            )

    if count:
        modeladmin.message_user(
            request,
            f"{count} join request(s) rejected.",
            level=messages.WARNING,
        )


# =========================================================
# ACTIONS: DUES
# =========================================================
@admin.action(description="Mark selected dues as cancelled")
def cancel_dues(modeladmin, request, queryset):
    updated = queryset.exclude(status="PAID").update(status="CANCELLED")
    modeladmin.message_user(
        request,
        f"{updated} due record(s) marked as cancelled.",
        level=messages.WARNING,
    )


@admin.action(description="Recalculate selected due statuses")
def recalc_due_statuses(modeladmin, request, queryset):
    count = 0
    for due in queryset:
        due.recalc_status()
        due.save(update_fields=["status"])
        count += 1

    modeladmin.message_user(
        request,
        f"{count} due record(s) recalculated.",
        level=messages.SUCCESS,
    )


# =========================================================
# ACTIONS: PAYMENTS
# =========================================================
@admin.action(description="Mark selected payments as confirmed")
def confirm_payments(modeladmin, request, queryset):
    """
    Practical admin helper:
    - marks payment CONFIRMED
    - stamps paid_at if missing
    - allocates into dues
    """
    count = 0

    for payment in queryset.select_related("merry", "beneficiary_member"):
        try:
            with transaction.atomic():
                p = MerryPayment.objects.select_for_update().get(pk=payment.pk)

                if p.status == "CONFIRMED":
                    continue

                if p.status in ("FAILED", "CANCELLED"):
                    raise ValueError(f"Cannot confirm payment from status {p.status}.")

                p.status = "CONFIRMED"
                if not p.paid_at:
                    p.paid_at = timezone.now()
                p.save(update_fields=["status", "paid_at"])

                # allocate to dues
                remaining = p.amount
                period_key = p.period_key
                merry = p.merry
                member = p.beneficiary_member

                safety = 0
                while remaining > 0:
                    safety += 1
                    if safety > 2000:
                        raise ValueError("Allocation safety limit reached.")

                    merry.ensure_dues_for_period(period_key=period_key)

                    dues = list(
                        MerryContributionDue.objects.select_for_update()
                        .filter(
                            merry=merry,
                            seat__member=member,
                            seat__is_active=True,
                            period_key=period_key,
                            status__in=["PENDING", "PARTIAL"],
                        )
                        .select_related("seat")
                        .order_by("slot_no", "seat__seat_no", "id")
                    )

                    any_needed = False

                    for due in dues:
                        need = (due.due_amount or 0) - (due.paid_amount or 0)
                        if need <= 0:
                            continue

                        any_needed = True
                        alloc = remaining if remaining < need else need
                        if alloc <= 0:
                            continue

                        allocation, _ = MerryPaymentAllocation.objects.get_or_create(
                            payment=p,
                            due=due,
                            defaults={"amount_allocated": 0},
                        )
                        allocation.amount_allocated = (allocation.amount_allocated or 0) + alloc
                        allocation.full_clean()
                        allocation.save(update_fields=["amount_allocated"])

                        due.paid_amount = (due.paid_amount or 0) + alloc
                        due.recalc_status()
                        due.save(update_fields=["paid_amount", "status", "updated_at"])

                        remaining -= alloc
                        if remaining <= 0:
                            break

                    if remaining <= 0:
                        break

                    if merry.payout_frequency == "MONTHLY":
                        year = int(period_key[:4])
                        month = int(period_key.split("-")[1])
                        month += 1
                        if month == 13:
                            month = 1
                            year += 1
                        period_key = f"{year:04d}-{month:02d}"
                    else:
                        year = int(period_key[:4])
                        week = int(period_key.split("-W")[1])
                        from datetime import date, timedelta
                        d = date.fromisocalendar(year, week, 1) + timedelta(days=7)
                        y, w, _ = d.isocalendar()
                        period_key = f"{y:04d}-W{w:02d}"

                    if not any_needed:
                        continue

                count += 1

        except Exception as e:
            modeladmin.message_user(
                request,
                f"Payment #{payment.id} failed: {e}",
                level=messages.ERROR,
            )

    if count:
        modeladmin.message_user(
            request,
            f"{count} payment(s) marked as confirmed and allocated.",
            level=messages.SUCCESS,
        )


@admin.action(description="Mark selected payments as failed")
def fail_payments(modeladmin, request, queryset):
    updated = queryset.exclude(status="CONFIRMED").exclude(status="CANCELLED").update(status="FAILED")
    modeladmin.message_user(
        request,
        f"{updated} payment(s) marked as failed.",
        level=messages.WARNING,
    )


# =========================================================
# ACTIONS: PAYOUTS
# =========================================================
@admin.action(description="Mark selected payouts as processing")
def mark_payouts_processing(modeladmin, request, queryset):
    updated = queryset.filter(status="SCHEDULED").update(status="PROCESSING")
    modeladmin.message_user(
        request,
        f"{updated} payout(s) marked as processing.",
        level=messages.SUCCESS,
    )


@admin.action(description="Mark selected payouts as paid")
def mark_payouts_paid(modeladmin, request, queryset):
    count = 0

    for payout in queryset:
        if payout.status in ("SCHEDULED", "PROCESSING", "FAILED"):
            payout.status = "PAID"
            if not payout.paid_at:
                payout.paid_at = timezone.now()
            payout.save(update_fields=["status", "paid_at"])
            count += 1

    modeladmin.message_user(
        request,
        f"{count} payout(s) marked as paid.",
        level=messages.SUCCESS,
    )


@admin.action(description="Mark selected payouts as failed")
def mark_payouts_failed(modeladmin, request, queryset):
    updated = queryset.exclude(status="PAID").exclude(status="CANCELLED").update(status="FAILED")
    modeladmin.message_user(
        request,
        f"{updated} payout(s) marked as failed.",
        level=messages.WARNING,
    )


@admin.action(description="Cancel selected payouts")
def cancel_payouts(modeladmin, request, queryset):
    updated = queryset.exclude(status="PAID").update(status="CANCELLED")
    modeladmin.message_user(
        request,
        f"{updated} payout(s) cancelled.",
        level=messages.WARNING,
    )


# =========================================================
# INLINES
# =========================================================
class MerrySlotConfigInline(admin.TabularInline):
    model = MerrySlotConfig
    extra = 0


class MerryMemberInline(admin.TabularInline):
    model = MerryMember
    extra = 0
    fields = ("user", "joined_at", "is_active")
    readonly_fields = ("joined_at",)


class MerrySeatInline(admin.TabularInline):
    model = MerrySeat
    extra = 0
    fields = ("member", "seat_no", "payout_position", "is_active", "created_at")
    readonly_fields = ("created_at",)


class MerryJoinRequestInline(admin.TabularInline):
    model = MerryJoinRequest
    extra = 0
    fields = ("user", "requested_seats", "status", "created_at", "reviewed_by", "reviewed_at")
    readonly_fields = ("created_at", "reviewed_by", "reviewed_at")


class MerryContributionDueInline(admin.TabularInline):
    model = MerryContributionDue
    extra = 0
    fields = (
        "seat",
        "period_key",
        "slot_no",
        "due_amount",
        "paid_amount",
        "status",
        "due_date",
        "updated_at",
    )
    readonly_fields = ("updated_at",)


class MerryPayoutInline(admin.TabularInline):
    model = MerryPayout
    extra = 0
    fields = ("seat", "period_key", "slot_no", "amount", "status", "paid_at")
    readonly_fields = ("paid_at",)


class MerryPaymentAllocationInline(admin.TabularInline):
    model = MerryPaymentAllocation
    extra = 0
    fields = ("due", "amount_allocated", "created_at")
    readonly_fields = ("created_at",)


# =========================================================
# MERRYGOROUND ADMIN
# =========================================================
@admin.register(MerryGoRound)
class MerryGoRoundAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "created_by",
        "contribution_amount",
        "payout_frequency",
        "payouts_per_period",
        "payout_order_type",
        "is_open",
        "max_seats",
        "available_seats_display",
        "next_payout_date",
        "active_members_count",
        "active_seats_count",
        "total_pool_per_slot_display",
        "total_pool_per_period_display",
        "created_at",
    )
    list_filter = (
        "is_open",
        "payout_order_type",
        "payout_frequency",
        "payouts_per_period",
        "created_at",
    )
    search_fields = (
        "name",
        "created_by__username",
        "created_by__phone",
    )
    ordering = ("-id",)
    list_select_related = ("created_by",)
    readonly_fields = (
        "created_at",
        "current_period_key_display",
        "required_amount_per_seat_per_period_display",
        "total_pool_per_slot_display",
        "total_pool_per_period_display",
        "available_seats_display",
    )
    actions = [generate_current_period_dues]
    inlines = [
        MerrySlotConfigInline,
        MerryMemberInline,
        MerrySeatInline,
        MerryJoinRequestInline,
        MerryPayoutInline,
    ]

    fieldsets = (
        ("Core", {
            "fields": (
                "name",
                "created_by",
                "contribution_amount",
                "cycle_duration_weeks",
            )
        }),
        ("Payout Setup", {
            "fields": (
                "payout_order_type",
                "payout_frequency",
                "payouts_per_period",
                "next_payout_date",
            )
        }),
        ("Joining / Capacity", {
            "fields": (
                "is_open",
                "max_seats",
                "available_seats_display",
            )
        }),
        ("Admin Summary", {
            "fields": (
                "current_period_key_display",
                "required_amount_per_seat_per_period_display",
                "total_pool_per_slot_display",
                "total_pool_per_period_display",
                "created_at",
            )
        }),
    )

    def active_members_count(self, obj):
        return obj.members.filter(is_active=True).count()

    active_members_count.short_description = "Active Members"

    def active_seats_count(self, obj):
        return obj.seats.filter(is_active=True).count()

    active_seats_count.short_description = "Active Seats"

    def current_period_key_display(self, obj):
        return obj.current_period_key()

    current_period_key_display.short_description = "Current Period"

    def required_amount_per_seat_per_period_display(self, obj):
        return obj.required_amount_per_seat_per_period()

    required_amount_per_seat_per_period_display.short_description = "Required / Seat / Period"

    def total_pool_per_slot_display(self, obj):
        return obj.total_pool_per_slot()

    total_pool_per_slot_display.short_description = "Pool / Slot"

    def total_pool_per_period_display(self, obj):
        return obj.total_pool_per_period()

    total_pool_per_period_display.short_description = "Pool / Period"

    def available_seats_display(self, obj):
        v = obj.available_seats()
        return "Unlimited" if v is None else v

    available_seats_display.short_description = "Available Seats"


# =========================================================
# SLOT CONFIG ADMIN
# =========================================================
@admin.register(MerrySlotConfig)
class MerrySlotConfigAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "merry",
        "slot_no",
        "weekday",
    )
    list_filter = ("weekday", "merry")
    search_fields = ("merry__name",)
    ordering = ("merry", "slot_no")
    list_select_related = ("merry",)


# =========================================================
# MEMBER ADMIN
# =========================================================
@admin.register(MerryMember)
class MerryMemberAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "merry",
        "user",
        "joined_at",
        "is_active",
        "seat_count_display",
    )
    list_filter = ("is_active", "joined_at", "merry")
    search_fields = (
        "merry__name",
        "user__username",
        "user__phone",
    )
    ordering = ("-id",)
    list_select_related = ("merry", "user")
    readonly_fields = ("joined_at",)

    def seat_count_display(self, obj):
        return obj.seats.filter(is_active=True).count()

    seat_count_display.short_description = "Active Seats"


# =========================================================
# SEAT ADMIN
# =========================================================
@admin.register(MerrySeat)
class MerrySeatAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "merry",
        "member",
        "member_user_display",
        "seat_no",
        "payout_position",
        "is_active",
        "created_at",
    )
    list_filter = ("is_active", "merry", "created_at")
    search_fields = (
        "merry__name",
        "member__user__username",
        "member__user__phone",
    )
    ordering = ("merry", "payout_position", "id")
    list_select_related = ("merry", "member", "member__user")
    readonly_fields = ("created_at",)

    def member_user_display(self, obj):
        return obj.member.user

    member_user_display.short_description = "User"


# =========================================================
# JOIN REQUEST ADMIN
# =========================================================
@admin.register(MerryJoinRequest)
class MerryJoinRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "merry",
        "merry_open_display",
        "user",
        "requested_seats",
        "status",
        "reviewed_by",
        "reviewed_at",
        "created_at",
    )
    list_filter = (
        "status",
        "created_at",
        "reviewed_at",
        "merry",
        "merry__is_open",
    )
    search_fields = (
        "merry__name",
        "user__username",
        "user__phone",
        "note",
    )
    ordering = ("-id",)
    list_select_related = ("merry", "user", "reviewed_by")
    readonly_fields = ("created_at", "reviewed_at")
    actions = [approve_join_requests, reject_join_requests]

    fieldsets = (
        ("Request", {
            "fields": (
                "merry",
                "user",
                "requested_seats",
                "status",
                "note",
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

    def merry_open_display(self, obj):
        return obj.merry.is_open

    merry_open_display.boolean = True
    merry_open_display.short_description = "Merry Open"


# =========================================================
# CONTRIBUTION DUE ADMIN
# =========================================================
@admin.register(MerryContributionDue)
class MerryContributionDueAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "merry",
        "seat",
        "seat_user_display",
        "period_key",
        "slot_no",
        "due_amount",
        "paid_amount",
        "outstanding_display",
        "status",
        "due_date",
        "updated_at",
    )
    list_filter = (
        "status",
        "period_key",
        "slot_no",
        "merry",
        "due_date",
    )
    search_fields = (
        "merry__name",
        "seat__member__user__username",
        "seat__member__user__phone",
        "period_key",
    )
    ordering = ("-id",)
    list_select_related = ("merry", "seat", "seat__member", "seat__member__user")
    readonly_fields = ("created_at", "updated_at")
    actions = [cancel_dues, recalc_due_statuses]

    def seat_user_display(self, obj):
        return obj.seat.member.user

    seat_user_display.short_description = "User"

    def outstanding_display(self, obj):
        return obj.outstanding()

    outstanding_display.short_description = "Outstanding"


# =========================================================
# PAYMENT ADMIN
# =========================================================
@admin.register(MerryPayment)
class MerryPaymentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "merry",
        "beneficiary_member",
        "beneficiary_user_display",
        "payer_phone",
        "period_key",
        "amount",
        "status",
        "mpesa_receipt_number",
        "paid_at",
        "created_at",
    )
    list_filter = (
        "status",
        "period_key",
        "paid_at",
        "created_at",
        "merry",
    )
    search_fields = (
        "merry__name",
        "beneficiary_member__user__username",
        "beneficiary_member__user__phone",
        "payer_phone",
        "mpesa_receipt_number",
    )
    ordering = ("-id",)
    list_select_related = ("merry", "beneficiary_member", "beneficiary_member__user")
    readonly_fields = ("created_at", "paid_at")
    actions = [confirm_payments, fail_payments]
    inlines = [MerryPaymentAllocationInline]

    def beneficiary_user_display(self, obj):
        return obj.beneficiary_member.user

    beneficiary_user_display.short_description = "User"


# =========================================================
# PAYMENT ALLOCATION ADMIN
# =========================================================
@admin.register(MerryPaymentAllocation)
class MerryPaymentAllocationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "payment",
        "due",
        "amount_allocated",
        "created_at",
    )
    list_filter = ("created_at",)
    search_fields = (
        "payment__beneficiary_member__user__username",
        "payment__beneficiary_member__user__phone",
        "payment__merry__name",
        "due__period_key",
    )
    ordering = ("id",)
    list_select_related = ("payment", "due")
    readonly_fields = ("created_at",)


# =========================================================
# PAYOUT ADMIN
# =========================================================
@admin.register(MerryPayout)
class MerryPayoutAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "merry",
        "seat",
        "seat_user_display",
        "period_key",
        "slot_no",
        "amount",
        "status",
        "paid_at",
        "created_at",
    )
    list_filter = (
        "status",
        "period_key",
        "slot_no",
        "paid_at",
        "created_at",
        "merry",
    )
    search_fields = (
        "merry__name",
        "seat__member__user__username",
        "seat__member__user__phone",
        "period_key",
        "notes",
    )
    ordering = ("-id",)
    list_select_related = ("merry", "seat", "seat__member", "seat__member__user")
    readonly_fields = ("created_at", "paid_at")
    actions = [
        mark_payouts_processing,
        mark_payouts_paid,
        mark_payouts_failed,
        cancel_payouts,
    ]

    fieldsets = (
        ("Core", {
            "fields": (
                "merry",
                "seat",
                "period_key",
                "slot_no",
                "amount",
                "status",
            )
        }),
        ("Meta", {
            "fields": (
                "notes",
                "paid_at",
                "created_at",
            )
        }),
    )

    def seat_user_display(self, obj):
        return obj.seat.member.user

    seat_user_display.short_description = "User"