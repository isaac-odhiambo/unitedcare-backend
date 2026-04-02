# merry/models.py
# UPDATED — Practical & realistic:
# ✅ Slot-based contributions (e.g., Monday + Friday)
# ✅ Partial payments per slot
# ✅ Overpayments roll into NEXT SLOT (same period) then future periods
# ✅ "Seats/Shares": a user can buy 2+ memberships (e.g., 3 seats => contributes 3000 when due=1000)
# ✅ Admin can generate scheduled dues per period/slot for all seats
# ✅ Clean admin reporting helpers
# ✅ Join requests support merry open/closed state
# ✅ Optional seat capacity limit
# ✅ Less strict join-request history (only one active pending request at a time)
# ✅ Seat numbers are now GLOBAL per merry (e.g. seat 2, 5, 19)
# ✅ Admin can manually assign seat numbers on approval
# ✅ Added overdue support
# ✅ Added next-due / advance-pay support
# ✅ Added due-date generation from slot config
# ✅ Added merry wallet for excess manual/outside-app payments
# ✅ Added merry wallet transaction history
# ✅ FIXED wallet transaction inline support in admin

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Optional, Dict, Any, List, Tuple

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Max, Sum, Count, Q
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


def _parse_week_period_key(period_key: str) -> tuple[int, int]:
    """
    Example: 2026-W12 -> (2026, 12)
    """
    try:
        year_part, week_part = period_key.split("-W")
        return int(year_part), int(week_part)
    except Exception as exc:
        raise ValidationError(f"Invalid week period_key format: {period_key}") from exc


def _parse_month_period_key(period_key: str) -> tuple[int, int]:
    """
    Example: 2026-03 -> (2026, 3)
    """
    try:
        year_part, month_part = period_key.split("-")
        return int(year_part), int(month_part)
    except Exception as exc:
        raise ValidationError(f"Invalid month period_key format: {period_key}") from exc


def _first_weekday_in_month(year: int, month: int, weekday: int) -> date:
    """
    Returns first occurrence of weekday in given month.
    weekday: Monday=0 ... Sunday=6
    """
    first_day = date(year, month, 1)
    days_ahead = (weekday - first_day.weekday()) % 7
    return first_day + timedelta(days=days_ahead)


def _nth_weekday_in_month(year: int, month: int, weekday: int, n: int) -> Optional[date]:
    """
    Returns nth occurrence of weekday in given month, or None if not present.
    n starts at 1.
    """
    if n < 1:
        return None

    first = _first_weekday_in_month(year, month, weekday)
    candidate = first + timedelta(days=(n - 1) * 7)

    if candidate.month != month:
        return None
    return candidate


def _last_day_of_month(year: int, month: int) -> date:
    if month == 12:
        return date(year + 1, 1, 1) - timedelta(days=1)
    return date(year, month + 1, 1) - timedelta(days=1)


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
    cycle_duration_weeks = models.PositiveIntegerField(default=1)

    payout_order_type = models.CharField(max_length=10, choices=ORDER_TYPES, default="manual")
    payout_frequency = models.CharField(max_length=10, choices=PAYOUT_FREQUENCY, default="WEEKLY")

    # If WEEKLY and payouts_per_period=2 => e.g. Monday + Friday
    payouts_per_period = models.PositiveIntegerField(default=1)

    # New members can request to join
    is_open = models.BooleanField(default=True)

    # 0 means unlimited
    max_seats = models.PositiveIntegerField(default=0, help_text="0 means unlimited seats")

    next_payout_date = models.DateField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="merries_created",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["is_open", "created_at"]),
            models.Index(fields=["payout_frequency", "created_at"]),
        ]

    def clean(self):
        if self.contribution_amount is not None and self.contribution_amount <= 0:
            raise ValidationError("contribution_amount must be greater than 0.")

        if self.cycle_duration_weeks < 1:
            raise ValidationError("cycle_duration_weeks must be at least 1.")

        if self.payouts_per_period < 1:
            raise ValidationError("payouts_per_period must be at least 1.")

        if self.max_seats < 0:
            raise ValidationError("max_seats cannot be negative.")

    # -------- period helpers --------
    def current_period_key(self, dt=None) -> str:
        dt = dt or timezone.localdate()
        if self.payout_frequency == "MONTHLY":
            return _month_period_key(dt)
        return _week_period_key(dt)

    def required_amount_per_seat_per_period(self) -> Decimal:
        return (self.contribution_amount or Decimal("0")) * Decimal(self.payouts_per_period or 0)

    def total_pool_per_slot(self) -> Decimal:
        seats_count = self.seats.filter(is_active=True).count()
        return Decimal(seats_count) * (self.contribution_amount or Decimal("0"))

    def total_pool_per_period(self) -> Decimal:
        return self.total_pool_per_slot() * Decimal(self.payouts_per_period or 0)

    def period_start_date(self, period_key: Optional[str] = None) -> Optional[date]:
        period_key = period_key or self.current_period_key()

        if self.payout_frequency == "MONTHLY":
            year, month = _parse_month_period_key(period_key)
            return date(year, month, 1)

        year, week = _parse_week_period_key(period_key)
        return date.fromisocalendar(year, week, 1)  # Monday

    def period_end_date(self, period_key: Optional[str] = None) -> Optional[date]:
        period_key = period_key or self.current_period_key()

        if self.payout_frequency == "MONTHLY":
            year, month = _parse_month_period_key(period_key)
            return _last_day_of_month(year, month)

        year, week = _parse_week_period_key(period_key)
        return date.fromisocalendar(year, week, 7)  # Sunday

    def get_slot_due_date(self, period_key: str, slot_no: int) -> Optional[date]:
        """
        Uses MerrySlotConfig when available.

        WEEKLY:
          - due date is the configured weekday inside that ISO week.

        MONTHLY:
          - due date is the nth occurrence of configured weekday in that month,
            where n == slot_no. If nth occurrence doesn't exist, falls back to
            the last occurrence within the month.

        If no slot config exists for the slot, returns None.
        """
        slot_cfg = self.slot_configs.filter(slot_no=slot_no).first()
        if not slot_cfg:
            return None

        weekday = int(slot_cfg.weekday)

        if self.payout_frequency == "MONTHLY":
            year, month = _parse_month_period_key(period_key)
            due_dt = _nth_weekday_in_month(year, month, weekday, slot_no)

            if due_dt is not None:
                return due_dt

            last_valid = None
            n = 1
            while True:
                candidate = _nth_weekday_in_month(year, month, weekday, n)
                if candidate is None:
                    break
                last_valid = candidate
                n += 1
            return last_valid

        year, week = _parse_week_period_key(period_key)
        monday = date.fromisocalendar(year, week, 1)
        return monday + timedelta(days=weekday)

    # -------- join/capacity helpers --------
    def active_seats_count(self) -> int:
        return self.seats.filter(is_active=True).count()

    def available_seats(self) -> Optional[int]:
        if not self.max_seats or self.max_seats <= 0:
            return None
        remaining = self.max_seats - self.active_seats_count()
        return remaining if remaining > 0 else 0

    def can_accept_join_request(self, requested_seats: int = 1) -> tuple[bool, str]:
        if not self.is_open:
            return False, "This merry is closed for joining."

        if requested_seats < 1:
            return False, "requested_seats must be at least 1."

        if self.max_seats and self.max_seats > 0:
            remaining = self.available_seats() or 0
            if requested_seats > remaining:
                return False, f"Only {remaining} seat(s) remaining."

        return True, "OK"

    # -------- payout ordering --------
    def next_payout_position(self) -> int:
        mx = self.seats.filter(is_active=True).aggregate(m=Max("payout_position")).get("m") or 0
        return int(mx) + 1

    # -------- global seat-number helpers --------
    def available_seat_numbers(self) -> Optional[List[int]]:
        if not self.max_seats or self.max_seats <= 0:
            return None

        taken = set(
            self.seats.filter(is_active=True).values_list("seat_no", flat=True)
        )
        return [n for n in range(1, self.max_seats + 1) if n not in taken]

    def next_available_seat_numbers(self, count: int) -> List[int]:
        if count < 1:
            raise ValidationError("count must be at least 1.")

        if self.max_seats and self.max_seats > 0:
            available = self.available_seat_numbers() or []
            if len(available) < count:
                raise ValidationError("Not enough available seat numbers.")
            return available[:count]

        existing = set(self.seats.filter(is_active=True).values_list("seat_no", flat=True))
        picked: List[int] = []
        n = 1
        while len(picked) < count:
            if n not in existing:
                picked.append(n)
            n += 1
        return picked

    # -------- admin schedule generation --------
    @transaction.atomic
    def ensure_dues_for_period(self, period_key: Optional[str] = None) -> int:
        period_key = period_key or self.current_period_key()

        if (self.payouts_per_period or 0) < 1:
            raise ValidationError("payouts_per_period must be >= 1")

        created = 0
        due_amt = self.contribution_amount or Decimal("0")

        active_seats = list(
            self.seats.filter(is_active=True).select_related("member", "member__user")
        )

        for seat in active_seats:
            for slot_no in range(1, self.payouts_per_period + 1):
                due_date = self.get_slot_due_date(period_key, slot_no)

                _, was_created = MerryContributionDue.objects.get_or_create(
                    merry=self,
                    seat=seat,
                    period_key=period_key,
                    slot_no=slot_no,
                    defaults={
                        "due_amount": due_amt,
                        "paid_amount": Decimal("0"),
                        "status": "PENDING",
                        "due_date": due_date,
                        "is_advance_payable": True,
                    },
                )
                if was_created:
                    created += 1

        return created

    # -------- admin dashboard helpers --------
    def admin_all_members_qs(self):
        return self.members.select_related("user").order_by("-is_active", "id")

    def admin_all_seats_qs(self):
        return self.seats.select_related("member", "member__user").order_by(
            "-is_active", "payout_position", "id"
        )

    def admin_due_qs(self, period_key: Optional[str] = None, slot_no: Optional[int] = None):
        period_key = period_key or self.current_period_key()
        qs = MerryContributionDue.objects.filter(merry=self, period_key=period_key).select_related(
            "seat", "seat__member", "seat__member__user"
        )
        if slot_no is not None:
            qs = qs.filter(slot_no=slot_no)
        return qs.order_by("slot_no", "seat_id")

    def admin_who_contributed(self, period_key: Optional[str] = None, slot_no: Optional[int] = None):
        return self.admin_due_qs(period_key=period_key, slot_no=slot_no).filter(
            paid_amount__gt=Decimal("0")
        ).order_by("-updated_at", "-id")

    def admin_total_collected(self, period_key: Optional[str] = None) -> Decimal:
        period_key = period_key or self.current_period_key()
        amt = (
            self.payments.filter(status="CONFIRMED", period_key=period_key)
            .aggregate(s=Sum("amount"))
            .get("s")
        )
        return amt or Decimal("0")

    def admin_outstanding_by_member(self, period_key: Optional[str] = None) -> List[Dict[str, Any]]:
        period_key = period_key or self.current_period_key()
        due_rows = (
            MerryContributionDue.objects.filter(merry=self, period_key=period_key)
            .select_related("seat__member__user")
        )

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

        seat_counts = (
            self.seats.filter(is_active=True)
            .values("member__user_id")
            .annotate(c=Count("id"))
        )
        for row in seat_counts:
            uid = row["member__user_id"]
            if uid not in by_user:
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

        result = list(by_user.values())
        result.sort(key=lambda x: (x["outstanding"] == Decimal("0"), x["name"]))
        return result

    def __str__(self) -> str:
        return self.name


class MerrySlotConfig(models.Model):
    """
    Example:
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
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="merry_memberships",
    )

    joined_at = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["merry", "user"], name="uniq_user_per_merry"),
        ]
        indexes = [
            models.Index(fields=["user", "is_active"]),
            models.Index(fields=["merry", "is_active"]),
        ]

    def __str__(self):
        return f"{self.user_id} - {self.merry.name}"


# ----------------------------
# Seats/Shares
# ----------------------------
class MerrySeat(models.Model):
    """
    Each seat behaves like a separate participation unit:
      - owes contribution_amount per slot
      - gets its own payout turn

    seat_no is now GLOBAL inside the merry.
    """
    merry = models.ForeignKey(MerryGoRound, on_delete=models.CASCADE, related_name="seats")
    member = models.ForeignKey(MerryMember, on_delete=models.CASCADE, related_name="seats")

    seat_no = models.PositiveIntegerField()
    payout_position = models.PositiveIntegerField(null=True, blank=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["seat_no", "id"]
        constraints = [
            models.UniqueConstraint(fields=["merry", "seat_no"], name="uniq_seat_no_per_merry"),
            models.UniqueConstraint(
                fields=["merry", "payout_position"],
                condition=Q(payout_position__isnull=False),
                name="uniq_payout_position_per_merry_seat",
            ),
        ]
        indexes = [
            models.Index(fields=["merry", "is_active"]),
            models.Index(fields=["member", "is_active"]),
            models.Index(fields=["merry", "seat_no"]),
        ]

    def clean(self):
        if self.seat_no < 1:
            raise ValidationError("seat_no must be >= 1")
        if self.member_id and self.merry_id and self.member.merry_id != self.merry_id:
            raise ValidationError("Seat merry must match member.merry")
        if self.merry_id and self.merry.max_seats and self.merry.max_seats > 0:
            if self.seat_no > self.merry.max_seats:
                raise ValidationError(
                    f"seat_no cannot exceed max_seats ({self.merry.max_seats}) for this merry."
                )

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
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="merry_join_requests",
    )

    requested_seats = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=50, choices=STATUS, default="PENDING")
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
            models.UniqueConstraint(
                fields=["merry", "user"],
                condition=Q(status="PENDING"),
                name="uniq_pending_join_request_per_merry_user",
            ),
        ]
        indexes = [
            models.Index(fields=["merry", "status", "created_at"]),
            models.Index(fields=["user", "status", "created_at"]),
        ]

    def clean(self):
        if self.requested_seats < 1:
            raise ValidationError("requested_seats must be >= 1")

        if self.requested_seats > 50:
            raise ValidationError("requested_seats cannot be more than 50.")

        if self.merry_id:
            ok, reason = self.merry.can_accept_join_request(self.requested_seats)
            if not ok:
                raise ValidationError(reason)

        if self.merry_id and self.user_id:
            if MerryMember.objects.filter(
                merry_id=self.merry_id,
                user_id=self.user_id,
                is_active=True,
            ).exists():
                raise ValidationError("You are already a member of this merry.")

            pending_qs = MerryJoinRequest.objects.filter(
                merry_id=self.merry_id,
                user_id=self.user_id,
                status="PENDING",
            )
            if self.pk:
                pending_qs = pending_qs.exclude(pk=self.pk)
            if pending_qs.exists():
                raise ValidationError("You already have a pending join request for this merry.")

    @transaction.atomic
    def approve(
        self,
        admin_user,
        assigned_seat_numbers: Optional[List[int]] = None,
    ) -> Tuple[MerryMember, List["MerrySeat"]]:
        if self.status != "PENDING":
            raise ValidationError("Only PENDING requests can be approved.")

        jr = (
            MerryJoinRequest.objects.select_for_update()
            .select_related("merry", "user")
            .get(id=self.id)
        )

        ok, reason = jr.merry.can_accept_join_request(jr.requested_seats)
        if not ok:
            raise ValidationError(reason)

        member, _ = MerryMember.objects.get_or_create(
            merry=jr.merry,
            user=jr.user,
            defaults={"joined_at": timezone.now(), "is_active": True},
        )

        if not member.is_active:
            member.is_active = True
            member.joined_at = member.joined_at or timezone.now()
            member.save(update_fields=["is_active", "joined_at"])

        if assigned_seat_numbers is None:
            seat_numbers = jr.merry.next_available_seat_numbers(jr.requested_seats)
        else:
            seat_numbers = [int(s) for s in assigned_seat_numbers]

            if len(seat_numbers) != jr.requested_seats:
                raise ValidationError(
                    f"Exactly {jr.requested_seats} seat number(s) must be assigned."
                )

            if any(s < 1 for s in seat_numbers):
                raise ValidationError("All assigned seat numbers must be >= 1.")

            if len(set(seat_numbers)) != len(seat_numbers):
                raise ValidationError("Assigned seat numbers must be unique.")

            if jr.merry.max_seats and jr.merry.max_seats > 0:
                invalid = [s for s in seat_numbers if s > jr.merry.max_seats]
                if invalid:
                    raise ValidationError(
                        f"These seat number(s) exceed max_seats ({jr.merry.max_seats}): {invalid}"
                    )

            taken = list(
                MerrySeat.objects.filter(
                    merry=jr.merry,
                    seat_no__in=seat_numbers,
                    is_active=True,
                ).values_list("seat_no", flat=True)
            )
            if taken:
                raise ValidationError(f"These seat number(s) are already taken: {sorted(taken)}")

        seats_created: List[MerrySeat] = []

        for seat_no in seat_numbers:
            payout_position: Optional[int] = None
            if jr.merry.payout_order_type == "manual":
                payout_position = jr.merry.next_payout_position()

            seat = MerrySeat.objects.create(
                merry=jr.merry,
                member=member,
                seat_no=seat_no,
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
        return (
            f"JoinRequest#{self.id} merry={self.merry_id} user={self.user_id} "
            f"seats={self.requested_seats} {self.status}"
        )


# ----------------------------
# Scheduled slot dues (per seat)
# ----------------------------
class MerryContributionDue(models.Model):
    STATUS_CHOICES = (
        ("PENDING", "Pending"),
        ("PARTIAL", "Partial"),
        ("OVERDUE", "Overdue"),
        ("PAID", "Paid"),
        ("CANCELLED", "Cancelled"),
    )

    merry = models.ForeignKey(MerryGoRound, on_delete=models.CASCADE, related_name="dues")
    seat = models.ForeignKey(MerrySeat, on_delete=models.CASCADE, related_name="dues")

    period_key = models.CharField(max_length=20, db_index=True)
    slot_no = models.PositiveIntegerField()

    due_amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))

    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default="PENDING")
    due_date = models.DateField(null=True, blank=True)

    is_advance_payable = models.BooleanField(default=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["due_date", "slot_no", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["seat", "period_key", "slot_no"],
                name="uniq_due_per_seat_period_slot",
            ),
        ]
        indexes = [
            models.Index(fields=["merry", "period_key", "slot_no"]),
            models.Index(fields=["status", "updated_at"]),
            models.Index(fields=["due_date", "status"]),
        ]

    def clean(self):
        if self.slot_no < 1:
            raise ValidationError("slot_no must be >= 1")
        if self.seat and self.seat.merry and self.slot_no > (self.seat.merry.payouts_per_period or 0):
            raise ValidationError("slot_no cannot exceed merry.payouts_per_period")
        if self.merry_id and self.seat_id and self.seat.merry_id != self.merry_id:
            raise ValidationError("Due.merry must match seat.merry")
        if self.due_amount is not None and self.due_amount <= 0:
            raise ValidationError("due_amount must be > 0")
        if self.paid_amount is not None and self.paid_amount < 0:
            raise ValidationError("paid_amount cannot be negative")

    def outstanding(self) -> Decimal:
        out = (self.due_amount or Decimal("0")) - (self.paid_amount or Decimal("0"))
        return out if out > 0 else Decimal("0")

    def is_overdue(self) -> bool:
        if self.status in ["PAID", "CANCELLED"]:
            return False
        return bool(self.due_date and self.due_date < timezone.localdate())

    def is_current(self) -> bool:
        if self.status in ["PAID", "CANCELLED"]:
            return False
        today = timezone.localdate()
        return bool(self.due_date and self.due_date == today)

    def is_future_due(self) -> bool:
        if self.status in ["PAID", "CANCELLED"]:
            return False
        return bool(self.due_date and self.due_date > timezone.localdate())

    def recalc_status(self):
        if self.status == "CANCELLED":
            return

        paid = self.paid_amount or Decimal("0")
        due = self.due_amount or Decimal("0")
        today = timezone.localdate()

        if paid >= due:
            self.status = "PAID"
            return

        is_past_due = bool(self.due_date and self.due_date < today)

        if is_past_due:
            self.status = "OVERDUE"
        elif paid > 0:
            self.status = "PARTIAL"
        else:
            self.status = "PENDING"

    def __str__(self):
        return f"Due#{self.id} seat={self.seat_id} {self.period_key} slot={self.slot_no} {self.status}"


# ----------------------------
# Payments
# ----------------------------
class MerryPayment(models.Model):
    STATUS_CHOICES = (
        ("PENDING", "Pending"),
        ("CONFIRMED", "Confirmed"),
        ("FAILED", "Failed"),
        ("CANCELLED", "Cancelled"),
    )

    merry = models.ForeignKey(MerryGoRound, on_delete=models.CASCADE, related_name="payments")

    beneficiary_member = models.ForeignKey(
        MerryMember,
        on_delete=models.CASCADE,
        related_name="payments",
    )

    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="merry_payments_initiated",
    )

    payer_phone = models.CharField(max_length=50, db_index=True)
    period_key = models.CharField(max_length=20, db_index=True)

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default="PENDING")
    paid_at = models.DateTimeField(null=True, blank=True)

    mpesa_receipt_number = models.CharField(max_length=64, null=True, blank=True, db_index=True)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["merry", "status", "created_at"]),
            models.Index(fields=["beneficiary_member", "created_at"]),
            models.Index(fields=["mpesa_receipt_number"]),
            models.Index(fields=["payer_phone", "created_at"]),
        ]

    def clean(self):
        if self.amount is not None and self.amount <= 0:
            raise ValidationError("amount must be > 0")
        if self.beneficiary_member_id and self.merry_id and self.beneficiary_member.merry_id != self.merry_id:
            raise ValidationError("Payment.merry must match beneficiary_member.merry")

    def __str__(self):
        return (
            f"Payment#{self.id} merry={self.merry_id} "
            f"member={self.beneficiary_member_id} {self.status} amount={self.amount}"
        )


# ----------------------------
# Allocation
# ----------------------------
class MerryPaymentAllocation(models.Model):
    payment = models.ForeignKey(MerryPayment, on_delete=models.CASCADE, related_name="allocations")
    due = models.ForeignKey(MerryContributionDue, on_delete=models.CASCADE, related_name="allocations")
    amount_allocated = models.DecimalField(max_digits=12, decimal_places=2)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["payment", "due"], name="uniq_allocation_per_payment_due"),
        ]

    def clean(self):
        if self.amount_allocated is not None and self.amount_allocated <= 0:
            raise ValidationError("amount_allocated must be > 0")

        if self.payment_id and self.due_id and self.payment.merry_id != self.due.merry_id:
            raise ValidationError("Allocation payment and due must belong to the same merry.")

    def __str__(self):
        return f"Alloc#{self.id} pay={self.payment_id} due={self.due_id} amt={self.amount_allocated}"


# ----------------------------
# Merry Wallet
# ----------------------------
class MerryWallet(models.Model):
    """
    Stores excess merry money for a user.
    Example:
      - user pays 5000 via mus11
      - 4000 clears active merry dues
      - 1000 remains here for future merry dues
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="merry_wallet",
    )
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user_id"]
        verbose_name = "Merry Wallet"
        verbose_name_plural = "Merry Wallets"
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["updated_at"]),
        ]

    def clean(self):
        if self.balance is not None and self.balance < 0:
            raise ValidationError("Wallet balance cannot be negative.")

    def __str__(self):
        return f"MerryWallet user={self.user_id} balance={self.balance}"


class MerryWalletTransaction(models.Model):
    """
    Wallet audit trail.
    CREDIT:
      - excess payment moved into wallet
    DEBIT:
      - wallet used to settle future merry dues
    """
    TX_TYPES = (
        ("CREDIT", "Credit"),
        ("DEBIT", "Debit"),
    )

    wallet = models.ForeignKey(
        MerryWallet,
        on_delete=models.CASCADE,
        related_name="transactions",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="merry_wallet_transactions",
    )

    tx_type = models.CharField(max_length=10, choices=TX_TYPES)

    amount = models.DecimalField(max_digits=14, decimal_places=2)
    balance_before = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    balance_after = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))

    reference = models.CharField(max_length=64, blank=True, default="")
    narration = models.CharField(max_length=255, blank=True, default="")
    mpesa_receipt_number = models.CharField(max_length=64, null=True, blank=True, db_index=True)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["wallet", "created_at"]),
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["tx_type", "created_at"]),
            models.Index(fields=["reference"]),
            models.Index(fields=["mpesa_receipt_number"]),
        ]

    def clean(self):
        if self.amount is not None and self.amount <= 0:
            raise ValidationError("Wallet transaction amount must be > 0.")
        if self.balance_before is not None and self.balance_before < 0:
            raise ValidationError("balance_before cannot be negative.")
        if self.balance_after is not None and self.balance_after < 0:
            raise ValidationError("balance_after cannot be negative.")
        if self.wallet_id and self.user_id and self.wallet.user_id != self.user_id:
            raise ValidationError("Wallet transaction user must match wallet.user.")

    def __str__(self):
        return (
            f"MerryWalletTx#{self.id} wallet={self.wallet_id} user={self.user_id} "
            f"{self.tx_type} amount={self.amount} after={self.balance_after}"
        )


# ----------------------------
# Payouts (seat-based)
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
    seat = models.ForeignKey(MerrySeat, on_delete=models.CASCADE, related_name="payouts")

    period_key = models.CharField(max_length=20, db_index=True)
    slot_no = models.PositiveIntegerField(default=1)

    amount = models.DecimalField(max_digits=12, decimal_places=2)

    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default="SCHEDULED")
    paid_at = models.DateTimeField(null=True, blank=True)

    notes = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["merry", "period_key", "slot_no"],
                name="uniq_payout_per_period_slot",
            ),
            models.UniqueConstraint(
                fields=["merry", "seat", "period_key"],
                name="uniq_seat_payout_per_period",
            ),
        ]
        indexes = [
            models.Index(fields=["merry", "period_key", "slot_no"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def clean(self):
        if self.seat_id and self.merry_id and self.seat.merry_id != self.merry_id:
            raise ValidationError("Payout.merry must match seat.merry")
        if self.slot_no < 1:
            raise ValidationError("slot_no must be >= 1")
        if self.merry and self.slot_no > (self.merry.payouts_per_period or 0):
            raise ValidationError("slot_no cannot exceed merry.payouts_per_period")
        if self.amount is not None and self.amount <= 0:
            raise ValidationError("amount must be > 0")

    def __str__(self):
        return (
            f"MerryPayout#{self.id} merry={self.merry_id} seat={self.seat_id} "
            f"period={self.period_key} slot={self.slot_no} {self.status}"
        )