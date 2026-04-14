from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.utils import timezone
from django.utils.html import format_html

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
    MerryWallet,
    MerryWalletTransaction,
)
from .services import (
    add_seats_to_existing_member,
    reassign_existing_clean_seat,
    confirm_payment_and_allocate,
)


# =========================================================
# SHARED HELPERS
# =========================================================
def get_reusable_or_free_seat_numbers(merry):
    """
    Selectable seats:
    - never-used seat numbers within 1..max_seats
    - inactive seat numbers within 1..max_seats

    Not selectable:
    - active seat numbers
    """
    if not merry or not merry.max_seats or merry.max_seats <= 0:
        return []

    active_taken = set(
        MerrySeat.objects.filter(merry=merry, is_active=True).values_list("seat_no", flat=True)
    )

    selectable = []
    for seat_no in range(1, merry.max_seats + 1):
        if seat_no not in active_taken:
            selectable.append(seat_no)

    return selectable


def build_seat_status_table_html(merry):
    if not merry:
        return "Select/save a merry first to view seat status."

    seats = list(
        MerrySeat.objects.filter(merry=merry)
        .select_related("member", "member__user")
        .order_by("seat_no", "id")
    )

    if merry.max_seats and merry.max_seats > 0:
        active_lookup = {}
        inactive_lookup = {}

        for seat in seats:
            if seat.is_active:
                active_lookup[seat.seat_no] = seat
            elif seat.seat_no not in active_lookup and seat.seat_no not in inactive_lookup:
                inactive_lookup[seat.seat_no] = seat

        rows = []
        for seat_no in range(1, merry.max_seats + 1):
            if seat_no in active_lookup:
                seat = active_lookup[seat_no]
                rows.append(
                    f"""
                    <tr>
                        <td style="border:1px solid #ddd; padding:6px;">{seat_no}</td>
                        <td style="border:1px solid #ddd; padding:6px; color:#b91c1c;"><strong>Taken (Active)</strong></td>
                        <td style="border:1px solid #ddd; padding:6px;">{seat.member.user}</td>
                        <td style="border:1px solid #ddd; padding:6px;">Yes</td>
                        <td style="border:1px solid #ddd; padding:6px;">{seat.payout_position or "-"}</td>
                        <td style="border:1px solid #ddd; padding:6px; color:#6b7280;">Not selectable</td>
                    </tr>
                    """
                )
            elif seat_no in inactive_lookup:
                seat = inactive_lookup[seat_no]
                rows.append(
                    f"""
                    <tr>
                        <td style="border:1px solid #ddd; padding:6px;">{seat_no}</td>
                        <td style="border:1px solid #ddd; padding:6px; color:#92400e;"><strong>Inactive / Reusable</strong></td>
                        <td style="border:1px solid #ddd; padding:6px;">{seat.member.user}</td>
                        <td style="border:1px solid #ddd; padding:6px;">No</td>
                        <td style="border:1px solid #ddd; padding:6px;">{seat.payout_position or "-"}</td>
                        <td style="border:1px solid #ddd; padding:6px; color:#166534;">Selectable</td>
                    </tr>
                    """
                )
            else:
                rows.append(
                    f"""
                    <tr>
                        <td style="border:1px solid #ddd; padding:6px;">{seat_no}</td>
                        <td style="border:1px solid #ddd; padding:6px; color:#166534;"><strong>Available</strong></td>
                        <td style="border:1px solid #ddd; padding:6px;">-</td>
                        <td style="border:1px solid #ddd; padding:6px;">-</td>
                        <td style="border:1px solid #ddd; padding:6px;">-</td>
                        <td style="border:1px solid #ddd; padding:6px; color:#166534;">Selectable</td>
                    </tr>
                    """
                )

        return format_html(
            """
            <div style="margin-top:10px;">
                <p><strong>Seat Status Table</strong></p>
                <p>
                    <span style="color:#b91c1c;"><strong>Taken (Active)</strong></span> = currently assigned and not selectable.<br>
                    <span style="color:#92400e;"><strong>Inactive / Reusable</strong></span> = previously used but inactive and selectable.<br>
                    <span style="color:#166534;"><strong>Available</strong></span> = never used and selectable.
                </p>
                <div style="overflow-x:auto; max-height:320px; border:1px solid #ddd;">
                    <table style="width:100%; border-collapse:collapse;">
                        <thead style="background:#f8f8f8; position:sticky; top:0;">
                            <tr>
                                <th style="border:1px solid #ddd; padding:6px;">Seat No</th>
                                <th style="border:1px solid #ddd; padding:6px;">Status</th>
                                <th style="border:1px solid #ddd; padding:6px;">Current / Last Member</th>
                                <th style="border:1px solid #ddd; padding:6px;">Seat Active</th>
                                <th style="border:1px solid #ddd; padding:6px;">Payout Position</th>
                                <th style="border:1px solid #ddd; padding:6px;">Selection</th>
                            </tr>
                        </thead>
                        <tbody>{}</tbody>
                    </table>
                </div>
            </div>
            """,
            format_html("".join(rows)),
        )

    if not seats:
        return format_html(
            """
            <div style="margin-top:10px;">
                <p><strong>Seat Status Table</strong></p>
                <p>This merry has unlimited seats and no seat range to select from.</p>
                <p>Seat checkbox selection works best when <strong>max_seats</strong> is set.</p>
            </div>
            """
        )

    rows = []
    for seat in seats:
        label = "Taken (Active)" if seat.is_active else "Inactive / Reusable"
        color = "#b91c1c" if seat.is_active else "#92400e"
        rows.append(
            f"""
            <tr>
                <td style="border:1px solid #ddd; padding:6px;">{seat.seat_no}</td>
                <td style="border:1px solid #ddd; padding:6px; color:{color};"><strong>{label}</strong></td>
                <td style="border:1px solid #ddd; padding:6px;">{seat.member.user}</td>
                <td style="border:1px solid #ddd; padding:6px;">{"Yes" if seat.is_active else "No"}</td>
                <td style="border:1px solid #ddd; padding:6px;">{seat.payout_position or "-"}</td>
                <td style="border:1px solid #ddd; padding:6px;">Seat selection requires max_seats</td>
            </tr>
            """
        )

    return format_html(
        """
        <div style="margin-top:10px;">
            <p><strong>Seat Status Table</strong></p>
            <p>This merry has unlimited seats. Set <strong>max_seats</strong> to enable admin seat selection by checkboxes.</p>
            <div style="overflow-x:auto; max-height:320px; border:1px solid #ddd;">
                <table style="width:100%; border-collapse:collapse;">
                    <thead style="background:#f8f8f8; position:sticky; top:0;">
                        <tr>
                            <th style="border:1px solid #ddd; padding:6px;">Seat No</th>
                            <th style="border:1px solid #ddd; padding:6px;">Status</th>
                            <th style="border:1px solid #ddd; padding:6px;">Current / Last Member</th>
                            <th style="border:1px solid #ddd; padding:6px;">Seat Active</th>
                            <th style="border:1px solid #ddd; padding:6px;">Payout Position</th>
                            <th style="border:1px solid #ddd; padding:6px;">Selection</th>
                        </tr>
                    </thead>
                    <tbody>{}</tbody>
                </table>
            </div>
        </div>
        """,
        format_html("".join(rows)),
    )


# =========================================================
# FORMS
# =========================================================
class MerryJoinRequestAdminForm(forms.ModelForm):
    available_seat_selection = forms.MultipleChoiceField(
        required=False,
        choices=[],
        widget=forms.CheckboxSelectMultiple,
        help_text="Select only from reusable/available seats below.",
    )

    class Meta:
        model = MerryJoinRequest
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        merry = None
        if self.instance and self.instance.pk and self.instance.merry_id:
            merry = self.instance.merry

        if merry and merry.max_seats and merry.max_seats > 0:
            seat_choices = get_reusable_or_free_seat_numbers(merry)
            self.fields["available_seat_selection"].choices = [
                (str(n), f"Seat {n}") for n in seat_choices
            ]
        else:
            self.fields["available_seat_selection"].choices = []
            self.fields["available_seat_selection"].help_text = (
                "Seat selection works only when max_seats is set on this merry."
            )

    def clean_available_seat_selection(self):
        values = self.cleaned_data.get("available_seat_selection") or []
        try:
            return [int(v) for v in values]
        except (ValueError, TypeError):
            raise forms.ValidationError("Selected seats are invalid.")

    def clean(self):
        cleaned = super().clean()

        status = cleaned.get("status")
        requested_seats = cleaned.get("requested_seats")
        merry = cleaned.get("merry") or getattr(self.instance, "merry", None)
        selected = cleaned.get("available_seat_selection") or []

        if status == "APPROVED":
            if not merry or not merry.max_seats or merry.max_seats <= 0:
                raise forms.ValidationError(
                    "This merry must have max_seats set before admin can assign seats from the seat table."
                )

            if not selected:
                raise forms.ValidationError(
                    "You must select seat(s) from the seat table before approving."
                )

            if requested_seats and len(selected) != requested_seats:
                raise forms.ValidationError(
                    f"You must select exactly {requested_seats} seat(s)."
                )

        return cleaned


class MerryMemberAdminForm(forms.ModelForm):
    available_seat_selection = forms.MultipleChoiceField(
        required=False,
        choices=[],
        widget=forms.CheckboxSelectMultiple,
        help_text="Select only from reusable/available seats below.",
    )

    class Meta:
        model = MerryMember
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        merry = None
        if self.instance and self.instance.pk and self.instance.merry_id:
            merry = self.instance.merry

        if merry and merry.max_seats and merry.max_seats > 0:
            seat_choices = get_reusable_or_free_seat_numbers(merry)
            self.fields["available_seat_selection"].choices = [
                (str(n), f"Seat {n}") for n in seat_choices
            ]
        else:
            self.fields["available_seat_selection"].choices = []
            self.fields["available_seat_selection"].help_text = (
                "Seat selection works only when max_seats is set on this merry."
            )

    def clean_available_seat_selection(self):
        values = self.cleaned_data.get("available_seat_selection") or []
        try:
            return [int(v) for v in values]
        except (ValueError, TypeError):
            raise forms.ValidationError("Selected seats are invalid.")


class MerrySeatAdminForm(forms.ModelForm):
    transfer_to_member = forms.ModelChoiceField(
        queryset=MerryMember.objects.none(),
        required=False,
        help_text="Optional. Select another active member in the same merry to transfer this clean seat.",
    )

    class Meta:
        model = MerrySeat
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        instance = getattr(self, "instance", None)
        if instance and instance.pk and instance.merry_id:
            self.fields["transfer_to_member"].queryset = (
                MerryMember.objects.filter(
                    merry_id=instance.merry_id,
                    is_active=True,
                )
                .select_related("user", "merry")
                .order_by("user__username", "id")
            )
        else:
            self.fields["transfer_to_member"].queryset = MerryMember.objects.none()


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
        due.save(update_fields=["status", "updated_at"])
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
    count = 0

    for payment in queryset.select_related("merry", "beneficiary_member"):
        try:
            with transaction.atomic():
                confirm_payment_and_allocate(
                    payment_id=payment.id,
                    mpesa_receipt_number=payment.mpesa_receipt_number,
                    paid_at=payment.paid_at or timezone.now(),
                )
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
    can_delete = False
    show_change_link = True
    fields = (
        "user",
        "requested_seats",
        "status",
        "note",
        "created_at",
        "reviewed_by",
        "reviewed_at",
    )
    readonly_fields = (
        "user",
        "requested_seats",
        "status",
        "note",
        "created_at",
        "reviewed_by",
        "reviewed_at",
    )

    def has_add_permission(self, request, obj=None):
        return False


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
        "is_advance_payable",
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


class MerryWalletTransactionInline(admin.TabularInline):
    model = MerryWalletTransaction
    extra = 0
    fields = (
        "tx_type",
        "amount",
        "balance_before",
        "balance_after",
        "reference",
        "narration",
        "mpesa_receipt_number",
        "created_at",
    )
    readonly_fields = fields
    can_delete = False
    show_change_link = True


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
    form = MerryMemberAdminForm

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
    readonly_fields = ("joined_at", "seat_status_preview")

    fieldsets = (
        ("Member", {
            "fields": (
                "merry",
                "user",
                "is_active",
                "joined_at",
            )
        }),
        ("Seat Status", {
            "fields": (
                "seat_status_preview",
            ),
            "description": "Select from reusable or available seats only.",
        }),
        ("Select Seats", {
            "fields": (
                "available_seat_selection",
            ),
            "description": "Choose the seat numbers to add to this member.",
        }),
    )

    def seat_count_display(self, obj):
        return obj.seats.filter(is_active=True).count()

    seat_count_display.short_description = "Active Seats"

    def seat_status_preview(self, obj):
        merry = obj.merry if obj and obj.pk and obj.merry_id else None
        return build_seat_status_table_html(merry)

    seat_status_preview.short_description = "Seat Status"

    def save_model(self, request, obj, form, change):
        selected_seats = form.cleaned_data.get("available_seat_selection") or []

        super().save_model(request, obj, form, change)

        if change and selected_seats:
            seats = add_seats_to_existing_member(
                admin_user=request.user,
                member_id=obj.id,
                assigned_seat_numbers=selected_seats,
            )
            self.message_user(
                request,
                f"Added seat(s) to member: {', '.join(str(seat.seat_no) for seat in seats)}",
                level=messages.SUCCESS,
            )


# =========================================================
# SEAT ADMIN
# =========================================================
@admin.register(MerrySeat)
class MerrySeatAdmin(admin.ModelAdmin):
    form = MerrySeatAdminForm

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
    readonly_fields = ("created_at", "seat_status_preview")

    fieldsets = (
        ("Seat", {
            "fields": (
                "merry",
                "member",
                "seat_no",
                "payout_position",
                "is_active",
                "created_at",
            )
        }),
        ("Seat Status", {
            "fields": (
                "seat_status_preview",
            ),
            "description": "Active seats are taken. Inactive seats are reusable.",
        }),
        ("Transfer Clean Seat", {
            "fields": (
                "transfer_to_member",
            ),
            "description": (
                "Optional. Transfer this seat to another active member in the same merry. "
                "Works only if the seat has no dues/payout history."
            ),
        }),
    )

    def member_user_display(self, obj):
        return obj.member.user

    member_user_display.short_description = "User"

    def seat_status_preview(self, obj):
        merry = obj.merry if obj and obj.pk and obj.merry_id else None
        return build_seat_status_table_html(merry)

    seat_status_preview.short_description = "Seat Status"

    def save_model(self, request, obj, form, change):
        transfer_to_member = form.cleaned_data.get("transfer_to_member")

        super().save_model(request, obj, form, change)

        if change and transfer_to_member and transfer_to_member.id != obj.member_id:
            updated_seat = reassign_existing_clean_seat(
                admin_user=request.user,
                seat_id=obj.id,
                new_member_id=transfer_to_member.id,
            )
            self.message_user(
                request,
                f"Seat {updated_seat.seat_no} transferred to {updated_seat.member.user}.",
                level=messages.SUCCESS,
            )


# =========================================================
# JOIN REQUEST ADMIN
# =========================================================
@admin.register(MerryJoinRequest)
class MerryJoinRequestAdmin(admin.ModelAdmin):
    form = MerryJoinRequestAdminForm

    list_display = (
        "id",
        "merry",
        "merry_open_display",
        "user",
        "requested_seats",
        "status",
        "member_note_short",
        "assigned_seats_display",
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
    readonly_fields = (
        "created_at",
        "reviewed_at",
        "seat_status_preview",
        "member_note_display",
        "assigned_seats_display",
    )
    actions = [reject_join_requests]

    fieldsets = (
        ("Request", {
            "fields": (
                "merry",
                "user",
                "requested_seats",
                "status",
            )
        }),
        ("Member Note", {
            "fields": (
                "note",
                "member_note_display",
            ),
            "description": "Member request note submitted from the app.",
        }),
        ("Seat Status", {
            "fields": (
                "seat_status_preview",
            ),
            "description": "Select from reusable or available seats only.",
        }),
        ("Assign Seats for Approval", {
            "fields": (
                "available_seat_selection",
            ),
            "description": "Choose the seat numbers to assign to this join request before approving.",
        }),
        ("Assigned Seats", {
            "fields": (
                "assigned_seats_display",
            ),
            "description": "Visible after approval. Shows active seats assigned to the approved member in this merry.",
        }),
        ("Review", {
            "fields": (
                "reviewed_by",
                "reviewed_at",
                "created_at",
            )
        }),
    )

    def get_readonly_fields(self, request, obj=None):
        ro = [
            "created_at",
            "reviewed_at",
            "seat_status_preview",
            "member_note_display",
            "assigned_seats_display",
        ]
        if obj and obj.status == "APPROVED":
            ro.extend([
                "merry",
                "user",
                "requested_seats",
                "status",
                "note",
                "reviewed_by",
                "available_seat_selection",
            ])
        return ro

    def merry_open_display(self, obj):
        return obj.merry.is_open

    merry_open_display.boolean = True
    merry_open_display.short_description = "Merry Open"

    def member_note_short(self, obj):
        if not obj.note:
            return "-"
        text = obj.note.strip()
        return text if len(text) <= 40 else f"{text[:40]}..."

    member_note_short.short_description = "Member Note"

    def member_note_display(self, obj):
        if not obj or not obj.pk or not obj.note:
            return "No note provided by member."
        return obj.note

    member_note_display.short_description = "Full Member Note"

    def assigned_seats_display(self, obj):
        if not obj or not obj.pk:
            return "Save the join request first to view assigned seats."

        if obj.status != "APPROVED":
            return "Seats will appear here after approval."

        member = (
            MerryMember.objects.filter(
                merry=obj.merry,
                user=obj.user,
                is_active=True,
            )
            .prefetch_related("seats")
            .first()
        )

        if not member:
            return "No active member record found yet."

        seat_numbers = list(
            member.seats.filter(is_active=True)
            .order_by("seat_no")
            .values_list("seat_no", flat=True)
        )

        if not seat_numbers:
            return "No active seats assigned."

        return ", ".join(str(seat_no) for seat_no in seat_numbers)

    assigned_seats_display.short_description = "Assigned Seats"

    def seat_status_preview(self, obj):
        merry = obj.merry if obj and obj.pk and obj.merry_id else None
        return build_seat_status_table_html(merry)

    seat_status_preview.short_description = "Seat Status"

    def save_model(self, request, obj, form, change):
        selected_seats = form.cleaned_data.get("available_seat_selection") or []

        if change:
            old_obj = MerryJoinRequest.objects.select_related("merry", "user").get(pk=obj.pk)

            if old_obj.status == "PENDING" and obj.status == "APPROVED":
                member, seats = old_obj.approve(
                    request.user,
                    assigned_seat_numbers=selected_seats,
                )
                self.message_user(
                    request,
                    f"Join request approved. Seats assigned: {', '.join(str(seat.seat_no) for seat in seats)}",
                    level=messages.SUCCESS,
                )
                return

            if old_obj.status == "PENDING" and obj.status == "REJECTED":
                old_obj.reject(request.user, note=obj.note or "")
                self.message_user(
                    request,
                    "Join request rejected.",
                    level=messages.WARNING,
                )
                return

            if old_obj.status == "APPROVED":
                self.message_user(
                    request,
                    "Approved join requests are historical records and were not reprocessed.",
                    level=messages.INFO,
                )
                return

        super().save_model(request, obj, form, change)


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
        "is_advance_payable",
        "updated_at",
    )
    list_filter = (
        "status",
        "period_key",
        "slot_no",
        "merry",
        "due_date",
        "is_advance_payable",
    )
    search_fields = (
        "merry__name",
        "seat__member__user__username",
        "seat__member__user__phone",
        "period_key",
    )
    ordering = ("due_date", "slot_no", "id")
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
# WALLET ADMIN
# =========================================================
@admin.register(MerryWallet)
class MerryWalletAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "balance",
        "last_updated_display",
        "tx_count_display",
        "created_at",
    )
    list_filter = ("created_at", "updated_at")
    search_fields = (
        "user__username",
        "user__phone",
        "user__email",
    )
    ordering = ("-updated_at", "-id")
    list_select_related = ("user",)
    readonly_fields = (
        "created_at",
        "updated_at",
        "tx_count_display",
    )
    inlines = [MerryWalletTransactionInline]

    fieldsets = (
        ("Wallet", {
            "fields": (
                "user",
                "balance",
                "created_at",
                "updated_at",
                "tx_count_display",
            )
        }),
    )

    def last_updated_display(self, obj):
        return obj.updated_at

    last_updated_display.short_description = "Updated At"

    def tx_count_display(self, obj):
        return obj.user.merry_wallet_transactions.count()

    tx_count_display.short_description = "Transactions"


@admin.register(MerryWalletTransaction)
class MerryWalletTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "tx_type",
        "amount",
        "balance_before",
        "balance_after",
        "reference",
        "mpesa_receipt_number",
        "created_at",
    )
    list_filter = (
        "tx_type",
        "created_at",
    )
    search_fields = (
        "user__username",
        "user__phone",
        "reference",
        "narration",
        "mpesa_receipt_number",
    )
    ordering = ("-id",)
    list_select_related = ("user",)
    readonly_fields = (
        "user",
        "tx_type",
        "amount",
        "balance_before",
        "balance_after",
        "reference",
        "narration",
        "mpesa_receipt_number",
        "created_at",
    )

    fieldsets = (
        ("Wallet Transaction", {
            "fields": (
                "user",
                "tx_type",
                "amount",
                "balance_before",
                "balance_after",
            )
        }),
        ("Reference", {
            "fields": (
                "reference",
                "mpesa_receipt_number",
                "narration",
                "created_at",
            )
        }),
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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