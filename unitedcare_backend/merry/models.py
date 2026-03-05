# merry/models.py
# UPDATED — Practical & realistic:
# ✅ Slot-based contributions (e.g., Monday + Friday)
# ✅ Partial payments per slot
# ✅ Overpayments roll into NEXT SLOT (same period) then future periods
# ✅ "Seats/Shares": a user can buy 2+ memberships (e.g., 3 seats => contributes 3000 when due=1000)
# ✅ Admin can generate scheduled dues per period/slot for all seats
# ✅ Clean admin reporting helpers

from __future__ import annotations
from typing import Tuple
from decimal import Decimal
from typing import Optional, Dict, Any, List

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Max, Sum, Count
from django.utils import timezone


# ----------------------------
# Constants / helpers
# ----------------------------
WEEKDAY_CHOICES = (
    (0, "Monday"),
    (1, "Tuesday"),
    (2, "Wednesday"),
    (3, "Thursday"),
    (4, "Friday"),
    (5, "Saturday"),
    (6, "Sunday"),
)


def _week_period_key(d=None) -> str:
    d = d or timezone.localdate()
    y, w, _ = d.isocalendar()
    return f"{y:04d}-W{w:02d}"


def _month_period_key(d=None) -> str:
    d = d or timezone.localdate()
    return f"{d.year:04d}-{d.month:02d}"


# ----------------------------
# Core Merry
# ----------------------------
class MerryGoRound(models.Model):
    ORDER_TYPES = (
        ("manual", "Manual"),
        ("random", "Random"),
    )

    PAYOUT_FREQUENCY = (
        ("WEEKLY", "Weekly"),
        ("MONTHLY", "Monthly"),
    )

    name = models.CharField(max_length=255)
    contribution_amount = models.DecimalField(max_digits=12, decimal_places=2)  # per seat per slot
    cycle_duration_weeks = models.PositiveIntegerField(default=1)  # optional (kept)

    payout_order_type = models.CharField(max_length=10, choices=ORDER_TYPES, default="manual")

    # Frequency controls how we compute period_key
    payout_frequency = models.CharField(max_length=10, choices=PAYOUT_FREQUENCY, default="WEEKLY")

    # If WEEKLY and payouts_per_period=2 => Slot 1 + Slot 2 (e.g. Mon + Fri)
    # Also means: contributions are expected per slot (Option B)
    payouts_per_period = models.PositiveIntegerField(default=1)

    # Used elsewhere (withdrawals)
    next_payout_date = models.DateField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="merries_created",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-id"]

    # -------- period helpers --------
    def current_period_key(self, dt=None) -> str:
        dt = dt or timezone.localdate()
        if self.payout_frequency == "MONTHLY":
            return _month_period_key(dt)
        return _week_period_key(dt)

    def required_amount_per_seat_per_period(self) -> Decimal:
        """
        Expected per seat per period = contribution_amount * payouts_per_period
        (e.g., 1000 * 2 slots = 2000 per seat per week)
        """
        return (self.contribution_amount or Decimal("0")) * Decimal(self.payouts_per_period or 0)

    def total_pool_per_slot(self) -> Decimal:
        """
        Total expected pool for one slot (if all seats paid for that slot).
        """
        seats_count = self.seats.filter(is_active=True).count()
        return Decimal(seats_count) * (self.contribution_amount or Decimal("0"))

    def total_pool_per_period(self) -> Decimal:
        """
        Total expected pool for the full period (all slots).
        """
        return self.total_pool_per_slot() * Decimal(self.payouts_per_period or 0)

    # -------- payout ordering --------
    def next_payout_position(self) -> int:
        """
        Returns next payout_position for a new seat (1..N),
        based on existing seats in the merry.
        """
        mx = self.seats.filter(is_active=True).aggregate(m=Max("payout_position")).get("m") or 0
        return int(mx) + 1

    # -------- admin schedule generation --------
    @transaction.atomic
    def ensure_dues_for_period(self, period_key: Optional[str] = None) -> int:
        """
        Create scheduled dues for ALL active seats for this period and all slots.
        One due = one seat owes contribution_amount for (period_key, slot_no).
        Returns number of created dues.
        """
        period_key = period_key or self.current_period_key()
        if (self.payouts_per_period or 0) < 1:
            raise ValidationError("payouts_per_period must be >= 1")

        created = 0
        due_amt = self.contribution_amount or Decimal("0")

        active_seats = list(self.seats.filter(is_active=True).select_related("member", "member__user"))
        for seat in active_seats:
            for slot_no in range(1, self.payouts_per_period + 1):
                obj, was_created = MerryContributionDue.objects.get_or_create(
                    merry=self,
                    seat=seat,
                    period_key=period_key,
                    slot_no=slot_no,
                    defaults={
                        "due_amount": due_amt,
                        "paid_amount": Decimal("0"),
                        "status": "PENDING",
                        "due_date": None,
                    },
                )
                if was_created:
                    created += 1

        return created

    # -------- admin dashboard helpers --------
    def admin_all_members_qs(self):
        return self.members.select_related("user").order_by("-is_active", "id")

    def admin_all_seats_qs(self):
        return self.seats.select_related("member", "member__user").order_by("-is_active", "payout_position", "id")

    def admin_due_qs(self, period_key: Optional[str] = None, slot_no: Optional[int] = None):
        period_key = period_key or self.current_period_key()
        qs = MerryContributionDue.objects.filter(merry=self, period_key=period_key).select_related(
            "seat", "seat__member", "seat__member__user"
        )
        if slot_no is not None:
            qs = qs.filter(slot_no=slot_no)
        return qs.order_by("slot_no", "seat_id")

    def admin_who_contributed(self, period_key: Optional[str] = None, slot_no: Optional[int] = None):
        """
        Dues with paid_amount > 0 (PARTIAL or PAID) for the given slot/period.
        """
        return self.admin_due_qs(period_key=period_key, slot_no=slot_no).filter(
            paid_amount__gt=Decimal("0")
        ).order_by("-updated_at", "-id")

    def admin_total_collected(self, period_key: Optional[str] = None) -> Decimal:
        """
        Total collected from confirmed payments for this period.
        """
        period_key = period_key or self.current_period_key()
        amt = (
            self.payments.filter(status="CONFIRMED", period_key=period_key)
            .aggregate(s=Sum("amount"))
            .get("s")
        )
        return amt or Decimal("0")

    def admin_outstanding_by_member(self, period_key: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Outstanding balances aggregated per USER (member), respecting multiple seats.
        For each user:
          required = seats_count * contribution_amount * payouts_per_period
          paid = sum of allocations into dues for those seats (for that period)
          outstanding = max(required - paid, 0)
        """
        period_key = period_key or self.current_period_key()
        due_rows = (
            MerryContributionDue.objects.filter(merry=self, period_key=period_key)
            .select_related("seat__member__user")
        )

        # aggregate in Python (simple + correct)
        by_user: Dict[int, Dict[str, Any]] = {}
        for d in due_rows:
            u = d.seat.member.user
            if u.id not in by_user:
                by_user[u.id] = {
                    "user_id": u.id,
                    "name": getattr(u, "username", str(u.id)),
                    "period_key": period_key,
                    "seats": 0,
                    "required": Decimal("0"),
                    "paid": Decimal("0"),
                    "outstanding": Decimal("0"),
                }

            by_user[u.id]["paid"] += (d.paid_amount or Decimal("0"))
            by_user[u.id]["required"] += (d.due_amount or Decimal("0"))

        # count seats per user (active seats only)
        seat_counts = (
            self.seats.filter(is_active=True)
            .values("member__user_id")
            .annotate(c=Count("id"))
        )
        for row in seat_counts:
            uid = row["member__user_id"]
            if uid not in by_user:
                # user has seats but no dues generated (possible if schedule not created)
                u = self.members.filter(user_id=uid).select_related("user").first()
                by_user[uid] = {
                    "user_id": uid,
                    "name": getattr(u.user, "username", str(uid)) if u else str(uid),
                    "period_key": period_key,
                    "seats": int(row["c"]),
                    "required": Decimal("0"),
                    "paid": Decimal("0"),
                    "outstanding": Decimal("0"),
                }
            by_user[uid]["seats"] = int(row["c"])

        for v in by_user.values():
            out = (v["required"] or Decimal("0")) - (v["paid"] or Decimal("0"))
            v["outstanding"] = out if out > 0 else Decimal("0")

        # unpaid first
        result = list(by_user.values())
        result.sort(key=lambda x: (x["outstanding"] == Decimal("0"), x["name"]))
        return result

    def __str__(self) -> str:
        return self.name


class MerrySlotConfig(models.Model):
    """
    Admin: define what weekday each slot happens.
    Example (weekly 2 slots):
      slot 1 => Monday
      slot 2 => Friday
    """
    merry = models.ForeignKey(MerryGoRound, on_delete=models.CASCADE, related_name="slot_configs")
    slot_no = models.PositiveIntegerField()
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAY_CHOICES)

    class Meta:
        ordering = ["slot_no"]
        constraints = [
            models.UniqueConstraint(fields=["merry", "slot_no"], name="uniq_slot_config_per_merry"),
        ]

    def clean(self):
        if self.slot_no < 1:
            raise ValidationError("slot_no must be >= 1")
        if self.merry and self.slot_no > (self.merry.payouts_per_period or 0):
            raise ValidationError("slot_no cannot exceed merry.payouts_per_period")

    def __str__(self):
        return f"{self.merry_id} slot {self.slot_no} => {self.get_weekday_display()}"


# ----------------------------
# Membership (User joins Merry once)
# ----------------------------
class MerryMember(models.Model):
    merry = models.ForeignKey(MerryGoRound, related_name="members", on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="merry_memberships")

    joined_at = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["merry", "user"], name="uniq_user_per_merry"),
        ]

    def __str__(self):
        return f"{self.user_id} - {self.merry.name}"


# ----------------------------
# ✅ Seats/Shares: user can have 2+ "memberships"
# ----------------------------
class MerrySeat(models.Model):
    """
    Each seat behaves like a separate participation unit:
      - owes contribution_amount per slot
      - gets its own payout turn (payout_position)
    If Isaac wants to contribute 3000 when due=1000, Isaac gets 3 seats.
    """
    merry = models.ForeignKey(MerryGoRound, on_delete=models.CASCADE, related_name="seats")
    member = models.ForeignKey(MerryMember, on_delete=models.CASCADE, related_name="seats")

    seat_no = models.PositiveIntegerField()  # 1..N per member within a merry
    payout_position = models.PositiveIntegerField(null=True, blank=True)  # seat's turn order

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["payout_position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["member", "seat_no"], name="uniq_seat_no_per_member"),
            models.UniqueConstraint(fields=["merry", "payout_position"], name="uniq_payout_position_per_merry_seat"),
        ]

    def clean(self):
        if self.seat_no < 1:
            raise ValidationError("seat_no must be >= 1")
        if self.member_id and self.merry_id and self.member.merry_id != self.merry_id:
            raise ValidationError("Seat merry must match member.merry")

    def __str__(self):
        return f"Seat#{self.id} merry={self.merry_id} user={self.member.user_id} seat_no={self.seat_no}"


# ----------------------------
# Join Requests (admin approval)
# ----------------------------
class MerryJoinRequest(models.Model):
    STATUS = (
        ("PENDING", "Pending"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
        ("CANCELLED", "Cancelled"),
    )

    merry = models.ForeignKey(MerryGoRound, on_delete=models.CASCADE, related_name="join_requests")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="merry_join_requests")

    # ✅ NEW: requested number of seats (shares)
    requested_seats = models.PositiveIntegerField(default=1)

    status = models.CharField(max_length=20, choices=STATUS, default="PENDING")
    note = models.CharField(max_length=255, blank=True, default="")

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_merry_join_requests",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(fields=["merry", "user"], name="uniq_join_request_per_merry_user"),
        ]
        indexes = [
            models.Index(fields=["status", "created_at"]),
        ]

    def clean(self):
        if self.requested_seats < 1:
            raise ValidationError("requested_seats must be >= 1")
        if self.merry_id and self.user_id:
            if MerryMember.objects.filter(merry_id=self.merry_id, user_id=self.user_id, is_active=True).exists():
                raise ValidationError("You are already a member of this merry.")

    @transaction.atomic
    def approve(self, admin_user) -> Tuple[MerryMember, List[MerrySeat]]:
        if self.status != "PENDING":
            raise ValidationError("Only PENDING requests can be approved.")

        jr = MerryJoinRequest.objects.select_for_update().select_related("merry").get(id=self.id)

        # create or fetch member
        member, _ = MerryMember.objects.get_or_create(
            merry=jr.merry,
            user=jr.user,
            defaults={"joined_at": timezone.now(), "is_active": True},
        )

        # create seats
        seats_created: List[MerrySeat] = []

        # determine next seat_no within this member
        existing_max_seat_no = member.seats.aggregate(m=Max("seat_no")).get("m") or 0
        seat_no_start = int(existing_max_seat_no) + 1

        for i in range(jr.requested_seats):
            payout_position: Optional[int] = None
            if jr.merry.payout_order_type == "manual":
                payout_position = jr.merry.next_payout_position()

            seat = MerrySeat.objects.create(
                merry=jr.merry,
                member=member,
                seat_no=seat_no_start + i,
                payout_position=payout_position,
                is_active=True,
                created_at=timezone.now(),
            )
            seats_created.append(seat)

        jr.status = "APPROVED"
        jr.reviewed_by = admin_user
        jr.reviewed_at = timezone.now()
        jr.save(update_fields=["status", "reviewed_by", "reviewed_at"])

        return member, seats_created

    def reject(self, admin_user, note: str = "") -> None:
        if self.status != "PENDING":
            raise ValidationError("Only PENDING requests can be rejected.")
        self.status = "REJECTED"
        self.reviewed_by = admin_user
        self.reviewed_at = timezone.now()
        if note:
            self.note = note[:255]
        self.save(update_fields=["status", "reviewed_by", "reviewed_at", "note"])

    def cancel(self, user) -> None:
        if self.user_id != user.id:
            raise ValidationError("You can only cancel your own join request.")
        if self.status != "PENDING":
            raise ValidationError("Only PENDING requests can be cancelled.")
        self.status = "CANCELLED"
        self.save(update_fields=["status"])

    def __str__(self):
        return f"JoinRequest#{self.id} merry={self.merry_id} user={self.user_id} seats={self.requested_seats} {self.status}"


# ----------------------------
# ✅ Scheduled slot dues (per SEAT)
# ----------------------------
class MerryContributionDue(models.Model):
    STATUS_CHOICES = (
        ("PENDING", "Pending"),
        ("PARTIAL", "Partial"),
        ("PAID", "Paid"),
        ("CANCELLED", "Cancelled"),
    )

    merry = models.ForeignKey(MerryGoRound, on_delete=models.CASCADE, related_name="dues")
    seat = models.ForeignKey(MerrySeat, on_delete=models.CASCADE, related_name="dues")

    period_key = models.CharField(max_length=20, db_index=True)
    slot_no = models.PositiveIntegerField()

    due_amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING")

    # Optional: for admin display
    due_date = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]
        constraints = [
            # ✅ per seat per period per slot
            models.UniqueConstraint(fields=["seat", "period_key", "slot_no"], name="uniq_due_per_seat_period_slot"),
        ]
        indexes = [
            models.Index(fields=["period_key", "slot_no"]),
            models.Index(fields=["status", "updated_at"]),
        ]

    def clean(self):
        if self.slot_no < 1:
            raise ValidationError("slot_no must be >= 1")
        if self.seat and self.seat.merry and self.slot_no > (self.seat.merry.payouts_per_period or 0):
            raise ValidationError("slot_no cannot exceed merry.payouts_per_period")
        if self.merry_id and self.seat_id and self.seat.merry_id != self.merry_id:
            raise ValidationError("Due.merry must match seat.merry")

    def recalc_status(self):
        if self.status == "CANCELLED":
            return
        paid = self.paid_amount or Decimal("0")
        due = self.due_amount or Decimal("0")
        if paid <= 0:
            self.status = "PENDING"
        elif paid < due:
            self.status = "PARTIAL"
        else:
            self.status = "PAID"

    def outstanding(self) -> Decimal:
        out = (self.due_amount or Decimal("0")) - (self.paid_amount or Decimal("0"))
        return out if out > 0 else Decimal("0")

    def __str__(self):
        return f"Due#{self.id} seat={self.seat_id} {self.period_key} slot={self.slot_no} {self.status}"


# ----------------------------
# ✅ Payments (STK events) — flexible amounts, any time
# ----------------------------
class MerryPayment(models.Model):
    STATUS_CHOICES = (
        ("PENDING", "Pending"),
        ("CONFIRMED", "Confirmed"),
        ("FAILED", "Failed"),
        ("CANCELLED", "Cancelled"),
    )

    merry = models.ForeignKey(MerryGoRound, on_delete=models.CASCADE, related_name="payments")

    # The logged-in member who gets the credit (beneficiary)
    beneficiary_member = models.ForeignKey(MerryMember, on_delete=models.CASCADE, related_name="payments")

    # Optional: who initiated in-app (usually same as beneficiary_member.user)
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="merry_payments_initiated",
    )

    # ✅ Phone that received STK prompt (payer line)
    payer_phone = models.CharField(max_length=20, db_index=True)

    # Store the period at initiation time (used as starting point for allocation)
    period_key = models.CharField(max_length=20, db_index=True)

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING")
    paid_at = models.DateTimeField(null=True, blank=True)

    mpesa_receipt_number = models.CharField(max_length=64, null=True, blank=True, db_index=True)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["mpesa_receipt_number"]),
            models.Index(fields=["payer_phone", "created_at"]),
        ]

    def clean(self):
        if self.amount is not None and self.amount <= 0:
            raise ValidationError("amount must be > 0")
        if self.beneficiary_member_id and self.merry_id and self.beneficiary_member.merry_id != self.merry_id:
            raise ValidationError("Payment.merry must match beneficiary_member.merry")

    def __str__(self):
        return f"Payment#{self.id} merry={self.merry_id} member={self.beneficiary_member_id} {self.status} amount={self.amount}"


# ----------------------------
# ✅ Allocation: spreads payment across dues (slot-first)
# ----------------------------
class MerryPaymentAllocation(models.Model):
    payment = models.ForeignKey(MerryPayment, on_delete=models.CASCADE, related_name="allocations")
    due = models.ForeignKey(MerryContributionDue, on_delete=models.CASCADE, related_name="allocations")
    amount_allocated = models.DecimalField(max_digits=12, decimal_places=2)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["id"]
        constraints = [
            # one row per payment+due (we update amount_allocated as we allocate more)
            models.UniqueConstraint(fields=["payment", "due"], name="uniq_allocation_per_payment_due"),
        ]

    def clean(self):
        if self.amount_allocated is not None and self.amount_allocated <= 0:
            raise ValidationError("amount_allocated must be > 0")

    def __str__(self):
        return f"Alloc#{self.id} pay={self.payment_id} due={self.due_id} amt={self.amount_allocated}"


# ----------------------------
# Payouts — pay a SEAT (since seats have independent turns)
# ----------------------------
class MerryPayout(models.Model):
    STATUS_CHOICES = (
        ("SCHEDULED", "Scheduled"),
        ("PROCESSING", "Processing"),
        ("PAID", "Paid"),
        ("FAILED", "Failed"),
        ("CANCELLED", "Cancelled"),
    )

    merry = models.ForeignKey(MerryGoRound, on_delete=models.CASCADE, related_name="payouts")

    # ✅ seat-based payout (fair for multi-seat members)
    seat = models.ForeignKey(MerrySeat, on_delete=models.CASCADE, related_name="payouts")

    period_key = models.CharField(max_length=20, db_index=True)
    slot_no = models.PositiveIntegerField(default=1)

    amount = models.DecimalField(max_digits=12, decimal_places=2)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="SCHEDULED")
    paid_at = models.DateTimeField(null=True, blank=True)

    notes = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-id"]
        constraints = [
            # allow multiple payouts per period (slot-based)
            models.UniqueConstraint(fields=["merry", "period_key", "slot_no"], name="uniq_payout_per_period_slot"),
            # prevent paying same seat twice in the same period
            models.UniqueConstraint(fields=["merry", "seat", "period_key"], name="uniq_seat_payout_per_period"),
        ]
        indexes = [
            models.Index(fields=["period_key", "created_at"]),
            models.Index(fields=["slot_no", "created_at"]),
        ]

    def clean(self):
        if self.seat_id and self.merry_id and self.seat.merry_id != self.merry_id:
            raise ValidationError("Payout.merry must match seat.merry")
        if self.slot_no < 1:
            raise ValidationError("slot_no must be >= 1")
        if self.merry and self.slot_no > (self.merry.payouts_per_period or 0):
            raise ValidationError("slot_no cannot exceed merry.payouts_per_period")

    def __str__(self):
        return (
            f"MerryPayout#{self.id} merry={self.merry_id} seat={self.seat_id} "
            f"period={self.period_key} slot={self.slot_no} {self.status}"
        )