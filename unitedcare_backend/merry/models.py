# merry/models.py
# ROSCA turn-linked compatibility version
# ---------------------------------------------------------
# Goals:
# - Keep existing tables/fields as compatible as possible
# - Preserve unrelated wallet / payment / join-request logic
# - Add turn-linked payout structure for true ROSCA history
# - Add penalty policy on merry creation
# - Allow DAILY / WEEKLY / MONTHLY payout frequency
# - Keep period_key / slot_no / payouts_per_period for migration safety
# - Support full migration of old dues/payouts into turn-linked structure
# ---------------------------------------------------------

from __future__ import annotations

from datetime import date, datetime, timedelta
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


def q2(value) -> Decimal:
    return Decimal(str(value or "0")).quantize(Decimal("0.01"))


def _week_period_key(d=None) -> str:
    d = d or timezone.localdate()
    y, w, _ = d.isocalendar()
    return f"{y:04d}-W{w:02d}"


def _month_period_key(d=None) -> str:
    d = d or timezone.localdate()
    return f"{d.year:04d}-{d.month:02d}"


def _day_period_key(d=None) -> str:
    d = d or timezone.localdate()
    return d.isoformat()


def _parse_week_period_key(period_key: str) -> tuple[int, int]:
    try:
        year_part, week_part = period_key.split("-W")
        return int(year_part), int(week_part)
    except Exception as exc:
        raise ValidationError(f"Invalid week period_key format: {period_key}") from exc


def _parse_month_period_key(period_key: str) -> tuple[int, int]:
    try:
        year_part, month_part = period_key.split("-")
        return int(year_part), int(month_part)
    except Exception as exc:
        raise ValidationError(f"Invalid month period_key format: {period_key}") from exc


def _parse_day_period_key(period_key: str) -> date:
    try:
        return date.fromisoformat(period_key)
    except Exception as exc:
        raise ValidationError(f"Invalid day period_key format: {period_key}") from exc


def _first_weekday_in_month(year: int, month: int, weekday: int) -> date:
    first_day = date(year, month, 1)
    days_ahead = (weekday - first_day.weekday()) % 7
    return first_day + timedelta(days=days_ahead)


def _nth_weekday_in_month(year: int, month: int, weekday: int, n: int) -> Optional[date]:
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


def _date_only(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


# ----------------------------
# Core Merry
# ----------------------------
class MerryGoRound(models.Model):
    ORDER_TYPES = (
        ("manual", "Manual"),
        ("random", "Random"),
    )

    PAYOUT_FREQUENCY = (
        ("DAILY", "Daily"),
        ("WEEKLY", "Weekly"),
        ("MONTHLY", "Monthly"),
    )

    PENALTY_MODES = (
        ("NONE", "None"),
        ("FLAT", "Flat"),
        ("DAILY", "Daily"),
    )

    name = models.CharField(max_length=255)
    contribution_amount = models.DecimalField(max_digits=12, decimal_places=2)  # per seat per payout
    cycle_duration_weeks = models.PositiveIntegerField(default=1)

    payout_order_type = models.CharField(max_length=10, choices=ORDER_TYPES, default="manual")
    payout_frequency = models.CharField(max_length=10, choices=PAYOUT_FREQUENCY, default="WEEKLY")

    # Legacy compatibility field.
    # Queue-based ROSCA uses one payout event at a time.
    payouts_per_period = models.PositiveIntegerField(default=1)

    is_open = models.BooleanField(default=True)

    # 0 means unlimited
    max_seats = models.PositiveIntegerField(default=0, help_text="0 means unlimited seats")

    next_payout_date = models.DateField(null=True, blank=True)

    # ----------------------------
    # Penalty policy
    # ----------------------------
    penalty_mode = models.CharField(
        max_length=10,
        choices=PENALTY_MODES,
        default="NONE",
        help_text="NONE, FLAT, or DAILY",
    )
    flat_penalty_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    daily_penalty_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    penalty_grace_days = models.PositiveIntegerField(default=0)
    penalty_cap_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Optional max total penalty per due.",
    )

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
            models.Index(fields=["penalty_mode", "created_at"]),
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

        if self.penalty_mode not in {"NONE", "FLAT", "DAILY"}:
            raise ValidationError("penalty_mode must be NONE, FLAT or DAILY.")

        if self.flat_penalty_amount is not None and self.flat_penalty_amount < 0:
            raise ValidationError("flat_penalty_amount cannot be negative.")

        if self.daily_penalty_amount is not None and self.daily_penalty_amount < 0:
            raise ValidationError("daily_penalty_amount cannot be negative.")

        if self.penalty_cap_amount is not None and self.penalty_cap_amount < 0:
            raise ValidationError("penalty_cap_amount cannot be negative.")

        if self.penalty_mode == "FLAT" and q2(self.flat_penalty_amount) <= Decimal("0.00"):
            raise ValidationError("flat_penalty_amount must be > 0 when penalty_mode is FLAT.")

        if self.penalty_mode == "DAILY" and q2(self.daily_penalty_amount) <= Decimal("0.00"):
            raise ValidationError("daily_penalty_amount must be > 0 when penalty_mode is DAILY.")

    # -------- queue-based payout helpers --------
    def effective_payouts_per_period(self) -> int:
        """
        Compatibility helper:
        keep the field in the model, but active ROSCA flow should use one payout at a time.
        """
        return 1

    def current_period_key(self, dt=None) -> str:
        dt = dt or timezone.localdate()

        if self.payout_frequency == "DAILY":
            return _day_period_key(dt)

        if self.payout_frequency == "MONTHLY":
            return _month_period_key(dt)

        return _week_period_key(dt)

    def required_amount_per_seat_per_period(self) -> Decimal:
        """
        Compatibility helper. In queue-based ROSCA this is one contribution per payout event.
        """
        return q2(self.contribution_amount or Decimal("0"))

    def total_pool_per_slot(self) -> Decimal:
        seats_count = self.seats.filter(is_active=True).count()
        return q2(Decimal(seats_count) * (self.contribution_amount or Decimal("0")))

    def total_pool_per_period(self) -> Decimal:
        """
        Compatibility helper. In queue mode current period ~= one active payout.
        """
        return self.total_pool_per_slot()

    def period_start_date(self, period_key: Optional[str] = None) -> Optional[date]:
        period_key = period_key or self.current_period_key()

        if self.payout_frequency == "DAILY":
            return _parse_day_period_key(period_key)

        if self.payout_frequency == "MONTHLY":
            year, month = _parse_month_period_key(period_key)
            return date(year, month, 1)

        year, week = _parse_week_period_key(period_key)
        return date.fromisocalendar(year, week, 1)

    def period_end_date(self, period_key: Optional[str] = None) -> Optional[date]:
        period_key = period_key or self.current_period_key()

        if self.payout_frequency == "DAILY":
            return _parse_day_period_key(period_key)

        if self.payout_frequency == "MONTHLY":
            year, month = _parse_month_period_key(period_key)
            return _last_day_of_month(year, month)

        year, week = _parse_week_period_key(period_key)
        return date.fromisocalendar(year, week, 7)

    def get_slot_due_date(self, period_key: str, slot_no: int) -> Optional[date]:
        """
        Compatibility helper.

        Queue-based ROSCA does not rely on multi-slot config anymore.
        We keep this method because older code/admin may still call it.

        Active expectation:
          - slot_no should be 1
          - due date should resolve to the period date / start date

        Legacy slot-config support is kept as a fallback.
        """
        if slot_no == 1:
            start = self.period_start_date(period_key)
            return start

        # Legacy fallback path only
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

        if self.payout_frequency == "DAILY":
            return _parse_day_period_key(period_key)

        year, week = _parse_week_period_key(period_key)
        monday = date.fromisocalendar(year, week, 1)
        return monday + timedelta(days=weekday)

    def add_schedule_step(self, base_date: date) -> date:
        if self.payout_frequency == "DAILY":
            return base_date + timedelta(days=1)

        if self.payout_frequency == "MONTHLY":
            year = base_date.year
            month = base_date.month + 1
            if month > 12:
                month = 1
                year += 1
            day = min(base_date.day, _last_day_of_month(year, month).day)
            return date(year, month, day)

        weeks = max(1, int(self.cycle_duration_weeks or 1))
        return base_date + timedelta(weeks=weeks)

    def active_cycle_seat_count(self) -> int:
        return self.seats.filter(is_active=True).count()

    def cycle_number_for_turn(self, turn_no: int) -> int:
        seats_count = self.active_cycle_seat_count()
        if seats_count <= 0:
            return 1
        return ((int(turn_no) - 1) // seats_count) + 1

    # -------- penalty helpers --------
    def penalty_starts_on(self, due_date: Optional[date]) -> Optional[date]:
        if not due_date:
            return None
        return due_date + timedelta(days=int(self.penalty_grace_days or 0) + 1)

    def calculate_penalty_for_due(
        self,
        *,
        base_amount: Decimal,
        due_date: Optional[date],
        as_of: Optional[date] = None,
        existing_penalty_amount: Optional[Decimal] = None,
        flat_already_applied: bool = False,
    ) -> tuple[Decimal, int]:
        """
        Returns (penalty_amount, days_overdue).

        - NONE  -> no penalty
        - FLAT  -> one-time penalty once overdue starts
        - DAILY -> daily fixed amount from penalty start date
        """
        as_of = as_of or timezone.localdate()
        base_amount = q2(base_amount or Decimal("0"))
        existing_penalty_amount = q2(existing_penalty_amount or Decimal("0"))

        if self.penalty_mode == "NONE":
            return Decimal("0.00"), 0

        penalty_start = self.penalty_starts_on(due_date)
        if not penalty_start or as_of < penalty_start:
            return Decimal("0.00"), 0

        days_overdue = (as_of - penalty_start).days + 1
        if days_overdue < 0:
            days_overdue = 0

        penalty = Decimal("0.00")

        if self.penalty_mode == "FLAT":
            penalty = q2(self.flat_penalty_amount or Decimal("0.00"))
            if flat_already_applied and existing_penalty_amount > 0:
                penalty = existing_penalty_amount

        elif self.penalty_mode == "DAILY":
            per_day = q2(self.daily_penalty_amount or Decimal("0.00"))
            penalty = q2(per_day * Decimal(days_overdue))

        if self.penalty_cap_amount is not None:
            cap = q2(self.penalty_cap_amount)
            if penalty > cap:
                penalty = cap

        return q2(penalty), int(days_overdue)

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

    def ordered_active_payout_seats(self) -> List["MerrySeat"]:
        seats = list(
            self.seats.filter(is_active=True)
            .select_related("member", "member__user")
            .order_by("payout_position", "seat_no", "id")
        )
        return seats

    def next_turn_no(self) -> int:
        mx = self.payouts.aggregate(m=Max("turn_no")).get("m") or 0
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

    # -------- compatibility schedule generation --------
    @transaction.atomic
    def ensure_dues_for_period(self, period_key: Optional[str] = None) -> int:
        """
        Compatibility helper for older admin/actions.

        Queue-based ROSCA now prepares only one payout event at a time.
        So even if older code calls this, we generate only slot_no=1 rows.
        """
        period_key = period_key or self.current_period_key()
        created = 0
        due_amt = self.contribution_amount or Decimal("0")

        active_seats = list(
            self.seats.filter(is_active=True).select_related("member", "member__user")
        )

        for seat in active_seats:
            due_date = self.get_slot_due_date(period_key, 1)

            _, was_created = MerryContributionDue.objects.get_or_create(
                merry=self,
                seat=seat,
                period_key=period_key,
                slot_no=1,
                defaults={
                    "due_amount": due_amt,
                    "base_amount": due_amt,
                    "paid_amount": Decimal("0"),
                    "penalty_amount": Decimal("0"),
                    "status": "PENDING",
                    "due_date": due_date,
                    "is_advance_payable": False,
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
            "seat", "seat__member", "seat__member__user", "payout"
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
        return q2(amt or Decimal("0"))

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
                    "penalty": Decimal("0"),
                }

            by_user[u.id]["paid"] += q2(d.paid_amount or Decimal("0"))
            by_user[u.id]["required"] += q2(d.due_amount or Decimal("0"))
            by_user[u.id]["penalty"] += q2(d.penalty_amount or Decimal("0"))

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
                    "penalty": Decimal("0"),
                }
            by_user[uid]["seats"] = int(row["c"])

        for v in by_user.values():
            due_total = q2(v["required"] or Decimal("0"))
            paid_total = q2(v["paid"] or Decimal("0"))
            out = due_total - paid_total
            v["outstanding"] = out if out > 0 else Decimal("0")

        result = list(by_user.values())
        result.sort(key=lambda x: (x["outstanding"] == Decimal("0"), x["name"]))
        return result

    def __str__(self) -> str:
        return self.name


class MerrySlotConfig(models.Model):
    """
    Legacy compatibility model.

    Queue-based ROSCA does not actively need multi-slot config anymore,
    but we keep the table/model to avoid destructive migrations right now.
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

    def joined_on(self) -> date:
        return _date_only(self.joined_at) or timezone.localdate()

    def __str__(self):
        return f"{self.user_id} - {self.merry.name}"


# ----------------------------
# Seats/Shares
# ----------------------------
class MerrySeat(models.Model):
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

    def eligible_from_date(self) -> date:
        return self.member.joined_on()

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
            if not self.pk:
                existing_member = MerryMember.objects.filter(
                    merry_id=self.merry_id,
                    user_id=self.user_id,
                    is_active=True,
                ).exists()
                if existing_member:
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
# Payouts (seat-based, permanent turn history)
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

    # Permanent turn-linked ROSCA fields
    turn_no = models.PositiveIntegerField(default=1, db_index=True)
    cycle_no = models.PositiveIntegerField(default=1, db_index=True)
    scheduled_date = models.DateField(null=True, blank=True, db_index=True)

    # Legacy compatibility
    period_key = models.CharField(max_length=20, db_index=True)
    slot_no = models.PositiveIntegerField(default=1)

    amount = models.DecimalField(max_digits=12, decimal_places=2)

    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default="SCHEDULED")
    paid_at = models.DateTimeField(null=True, blank=True)

    notes = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["turn_no", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["merry", "turn_no"],
                name="uniq_payout_turn_no_per_merry",
            ),
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
            models.Index(fields=["merry", "turn_no"]),
            models.Index(fields=["merry", "cycle_no", "turn_no"]),
            models.Index(fields=["merry", "scheduled_date"]),
            models.Index(fields=["merry", "period_key", "slot_no"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def clean(self):
        if self.seat_id and self.merry_id and self.seat.merry_id != self.merry_id:
            raise ValidationError("Payout.merry must match seat.merry")
        if self.slot_no < 1:
            raise ValidationError("slot_no must be >= 1")
        if self.turn_no < 1:
            raise ValidationError("turn_no must be >= 1")
        if self.cycle_no < 1:
            raise ValidationError("cycle_no must be >= 1")
        if self.amount is not None and self.amount <= 0:
            raise ValidationError("amount must be > 0")

    def effective_due_date(self) -> Optional[date]:
        return self.scheduled_date or self.merry.get_slot_due_date(self.period_key, self.slot_no)

    def __str__(self):
        return (
            f"MerryPayout#{self.id} merry={self.merry_id} seat={self.seat_id} "
            f"turn={self.turn_no} cycle={self.cycle_no} scheduled={self.scheduled_date} {self.status}"
        )


# ----------------------------
# Scheduled dues (per seat, turn-linked, compatibility-safe)
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

    # New permanent linkage to exact payout turn
    payout = models.ForeignKey(
        MerryPayout,
        on_delete=models.CASCADE,
        related_name="dues",
        null=True,
        blank=True,
    )

    # Legacy compatibility
    period_key = models.CharField(max_length=20, db_index=True)
    slot_no = models.PositiveIntegerField(default=1)

    # Historic due values
    due_amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))

    # Turn-based stored accounting
    base_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    penalty_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    penalty_applied_at = models.DateTimeField(null=True, blank=True)
    penalty_last_calculated_at = models.DateTimeField(null=True, blank=True)
    days_overdue = models.PositiveIntegerField(default=0)

    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default="PENDING")
    due_date = models.DateField(null=True, blank=True)

    is_advance_payable = models.BooleanField(default=False)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["due_date", "slot_no", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["seat", "period_key", "slot_no"],
                name="uniq_due_per_seat_period_slot",
            ),
            models.UniqueConstraint(
                fields=["seat", "payout"],
                condition=Q(payout__isnull=False),
                name="uniq_due_per_seat_per_payout",
            ),
        ]
        indexes = [
            models.Index(fields=["merry", "payout"]),
            models.Index(fields=["merry", "period_key", "slot_no"]),
            models.Index(fields=["status", "updated_at"]),
            models.Index(fields=["due_date", "status"]),
            models.Index(fields=["days_overdue", "status"]),
        ]

    def clean(self):
        if self.slot_no < 1:
            raise ValidationError("slot_no must be >= 1")
        if self.merry_id and self.seat_id and self.seat.merry_id != self.merry_id:
            raise ValidationError("Due.merry must match seat.merry")
        if self.payout_id and self.merry_id and self.payout.merry_id != self.merry_id:
            raise ValidationError("Due.merry must match payout.merry")
        if self.payout_id and self.seat_id and self.payout.seat_id == self.seat_id:
            # The beneficiary payout seat should not owe itself for that turn in most ROSCA designs.
            # We keep this as a soft model-level guard only if your business flow wants strict enforcement.
            pass
        if self.due_amount is not None and self.due_amount < 0:
            raise ValidationError("due_amount cannot be negative")
        if self.base_amount is not None and self.base_amount < 0:
            raise ValidationError("base_amount cannot be negative")
        if self.penalty_amount is not None and self.penalty_amount < 0:
            raise ValidationError("penalty_amount cannot be negative")
        if self.paid_amount is not None and self.paid_amount < 0:
            raise ValidationError("paid_amount cannot be negative")

    def effective_base_amount(self) -> Decimal:
        if self.base_amount and self.base_amount > 0:
            return q2(self.base_amount)
        return q2(self.due_amount or Decimal("0"))

    def total_due_amount(self) -> Decimal:
        return q2(self.effective_base_amount() + q2(self.penalty_amount or Decimal("0")))

    def outstanding(self) -> Decimal:
        out = self.total_due_amount() - q2(self.paid_amount or Decimal("0"))
        return out if out > 0 else Decimal("0.00")

    def outstanding_base_only(self) -> Decimal:
        out = self.effective_base_amount() - q2(self.paid_amount or Decimal("0"))
        return out if out > 0 else Decimal("0.00")

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

    def is_member_eligible_for_turn(self) -> bool:
        """
        Member starts contributing from first turn whose due date is on/after joined_at.
        """
        joined_on = self.seat.member.joined_on()
        target_due_date = self.due_date
        if target_due_date is None and self.payout_id:
            target_due_date = self.payout.effective_due_date()
        if target_due_date is None:
            return True
        return target_due_date >= joined_on



    def recalc_status(self) -> str:
        """
        Keep status aligned with payment progress and due date.
        """
        paid = q2(self.paid_amount or Decimal("0"))
        total_due = self.total_due_amount()

        if self.status == "CANCELLED":
            return self.status

        if paid >= total_due and total_due > Decimal("0.00"):
            self.status = "PAID"
            return self.status

        if paid > Decimal("0.00"):
            self.status = "PARTIAL"
            return self.status

        if self.due_date and self.due_date < timezone.localdate():
            self.status = "OVERDUE"
            return self.status

        self.status = "PENDING"
        return self.status

    def refresh_penalty(self, as_of: Optional[date] = None, save: bool = False) -> tuple[Decimal, int]:
        """
        Recalculate penalty based on merry policy.

        Important rule:
        - days_overdue should still be tracked even when penalty_mode == "NONE"
        - penalty_amount remains 0.00 when penalties are disabled
        """
        as_of = as_of or timezone.localdate()
        merry = self.merry

        changed = False
        now_ts = timezone.now()

        # Closed dues should not carry overdue days or penalties
        if self.status in ["PAID", "CANCELLED"]:
            if q2(self.penalty_amount or Decimal("0")) != Decimal("0.00"):
                self.penalty_amount = Decimal("0.00")
                changed = True
            if int(self.days_overdue or 0) != 0:
                self.days_overdue = 0
                changed = True

            self.penalty_last_calculated_at = now_ts
            changed = True

            total_due = self.total_due_amount()
            if q2(self.due_amount or Decimal("0")) != total_due:
                self.due_amount = total_due
                changed = True

            self.recalc_status()

            if save and changed:
                self.save(
                    update_fields=[
                        "penalty_amount",
                        "days_overdue",
                        "penalty_last_calculated_at",
                        "due_amount",
                        "status",
                        "updated_at",
                    ]
                )

            return q2(self.penalty_amount or Decimal("0")), int(self.days_overdue or 0)

        # No due date means nothing can be overdue yet
        if not self.due_date:
            if q2(self.penalty_amount or Decimal("0")) != Decimal("0.00"):
                self.penalty_amount = Decimal("0.00")
                changed = True
            if int(self.days_overdue or 0) != 0:
                self.days_overdue = 0
                changed = True

            self.penalty_last_calculated_at = now_ts
            changed = True

            total_due = self.total_due_amount()
            if q2(self.due_amount or Decimal("0")) != total_due:
                self.due_amount = total_due
                changed = True

            self.recalc_status()

            if save and changed:
                self.save(
                    update_fields=[
                        "penalty_amount",
                        "days_overdue",
                        "penalty_last_calculated_at",
                        "due_amount",
                        "status",
                        "updated_at",
                    ]
                )

            return q2(self.penalty_amount or Decimal("0")), int(self.days_overdue or 0)

        # Always calculate overdue days from date, even when penalty is disabled
        overdue_days = max(0, (as_of - self.due_date).days)

        if (merry.penalty_mode or "NONE").upper() == "NONE":
            penalty = Decimal("0.00")
        else:
            existing_penalty = q2(self.penalty_amount or Decimal("0"))
            flat_already_applied = existing_penalty > 0 and merry.penalty_mode == "FLAT"

            penalty, _ = merry.calculate_penalty_for_due(
                base_amount=self.effective_base_amount(),
                due_date=self.due_date,
                as_of=as_of,
                existing_penalty_amount=existing_penalty,
                flat_already_applied=flat_already_applied,
            )
            penalty = q2(penalty)

        if q2(self.penalty_amount or Decimal("0")) != q2(penalty):
            self.penalty_amount = q2(penalty)
            changed = True

        if int(self.days_overdue or 0) != int(overdue_days):
            self.days_overdue = int(overdue_days)
            changed = True

        self.penalty_last_calculated_at = now_ts
        changed = True

        if self.penalty_amount > 0 and self.penalty_applied_at is None:
            self.penalty_applied_at = now_ts
            changed = True

        total_due = self.total_due_amount()
        if q2(self.due_amount or Decimal("0")) != total_due:
            self.due_amount = total_due
            changed = True

        self.recalc_status()

        if save and changed:
            update_fields = [
                "penalty_amount",
                "days_overdue",
                "penalty_last_calculated_at",
                "due_amount",
                "status",
                "updated_at",
            ]
            if self.penalty_applied_at is not None:
                update_fields.append("penalty_applied_at")

            self.save(update_fields=update_fields)

        return q2(self.penalty_amount or Decimal("0")), int(self.days_overdue or 0)

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