from decimal import Decimal
from datetime import date

from django.db import migrations


def q2(value):
    return Decimal(str(value or "0")).quantize(Decimal("0.01"))


def parse_period_key(period_key):
    if not period_key:
        return None
    try:
        return date.fromisoformat(str(period_key).strip())
    except Exception:
        return None


def joined_on(member):
    joined_at = getattr(member, "joined_at", None)
    if joined_at is None:
        return None
    if hasattr(joined_at, "date"):
        return joined_at.date()
    return joined_at


def forward(apps, schema_editor):
    MerryGoRound = apps.get_model("merry", "MerryGoRound")
    MerryMember = apps.get_model("merry", "MerryMember")
    MerrySeat = apps.get_model("merry", "MerrySeat")
    MerryPayout = apps.get_model("merry", "MerryPayout")
    MerryContributionDue = apps.get_model("merry", "MerryContributionDue")

    # --------------------------------------------------
    # 1. Backfill payout turn_no, cycle_no, scheduled_date
    # --------------------------------------------------
    for merry in MerryGoRound.objects.all().order_by("id"):
        seats_count = MerrySeat.objects.filter(merry_id=merry.id, is_active=True).count()
        if seats_count <= 0:
            seats_count = 1

        payouts = list(
            MerryPayout.objects.filter(merry_id=merry.id).order_by("id")
        )

        turn_no = 1
        for payout in payouts:
            scheduled_date = getattr(payout, "scheduled_date", None)
            if not scheduled_date:
                scheduled_date = parse_period_key(getattr(payout, "period_key", None))

            payout.turn_no = turn_no
            payout.cycle_no = ((turn_no - 1) // seats_count) + 1
            payout.scheduled_date = scheduled_date
            payout.save(
                update_fields=["turn_no", "cycle_no", "scheduled_date"]
            )
            turn_no += 1

    # --------------------------------------------------
    # 2. Link dues to payout and initialize new due fields
    # --------------------------------------------------
    dues = MerryContributionDue.objects.select_related("seat").all().order_by("merry_id", "id")

    for due in dues:
        payout = None

        # Prefer exact payout match by merry + seat + period_key
        if due.seat_id and due.period_key:
            payout = (
                MerryPayout.objects.filter(
                    merry_id=due.merry_id,
                    seat_id=due.seat_id,
                    period_key=due.period_key,
                )
                .order_by("id")
                .first()
            )

        # Fallback by merry + period_key
        if payout is None and due.period_key:
            payout = (
                MerryPayout.objects.filter(
                    merry_id=due.merry_id,
                    period_key=due.period_key,
                )
                .order_by("id")
                .first()
            )

        scheduled_date = parse_period_key(getattr(due, "period_key", None))
        if payout and getattr(payout, "scheduled_date", None):
            scheduled_date = payout.scheduled_date

        # Respect join date: old dues before join should not remain payable
        eligible = True
        member = getattr(due.seat, "member", None)
        member_joined = joined_on(member) if member else None
        if member_joined and scheduled_date and scheduled_date < member_joined:
            eligible = False

        base_amount = q2(getattr(due, "base_amount", None) or 0)
        if base_amount <= Decimal("0.00"):
            current_due_amount = q2(getattr(due, "due_amount", None) or 0)
            base_amount = current_due_amount

        penalty_amount = Decimal("0.00")
        days_overdue = 0

        # Keep historical rows but cancel any due that predates join date
        if not eligible:
            due.payout_id = payout.id if payout else None
            due.base_amount = base_amount
            due.penalty_amount = Decimal("0.00")
            due.days_overdue = 0
            due.due_date = scheduled_date or getattr(due, "due_date", None)
            due.due_amount = base_amount
            due.status = "CANCELLED"
            due.save(
                update_fields=[
                    "payout",
                    "base_amount",
                    "penalty_amount",
                    "days_overdue",
                    "due_date",
                    "due_amount",
                    "status",
                    "updated_at",
                ]
            )
            continue

        due.payout_id = payout.id if payout else None
        due.base_amount = base_amount
        due.penalty_amount = penalty_amount
        due.days_overdue = days_overdue
        due.due_date = scheduled_date or getattr(due, "due_date", None)
        due.due_amount = q2(base_amount + penalty_amount)

        paid_amount = q2(getattr(due, "paid_amount", None) or 0)
        total_due = q2(due.due_amount or 0)

        if due.status != "CANCELLED":
            if paid_amount >= total_due and total_due > 0:
                due.status = "PAID"
            elif due.due_date and due.due_date < date.today():
                due.status = "OVERDUE"
            elif paid_amount > 0:
                due.status = "PARTIAL"
            else:
                due.status = "PENDING"

        due.save(
            update_fields=[
                "payout",
                "base_amount",
                "penalty_amount",
                "days_overdue",
                "due_date",
                "due_amount",
                "status",
                "updated_at",
            ]
        )

    # --------------------------------------------------
    # 3. Recompute next_payout_date from current scheduled payout
    # --------------------------------------------------
    for merry in MerryGoRound.objects.all().order_by("id"):
        scheduled = (
            MerryPayout.objects.filter(merry_id=merry.id, status="SCHEDULED")
            .order_by("turn_no", "id")
            .first()
        )
        if scheduled and getattr(scheduled, "scheduled_date", None):
            merry.next_payout_date = scheduled.scheduled_date
            merry.save(update_fields=["next_payout_date"])


def reverse(apps, schema_editor):
    MerryContributionDue = apps.get_model("merry", "MerryContributionDue")
    MerryPayout = apps.get_model("merry", "MerryPayout")

    # Safe reverse: keep rows, just clear new linkage/fields
    MerryContributionDue.objects.all().update(
        payout=None,
        penalty_amount=Decimal("0.00"),
        days_overdue=0,
    )

    for payout in MerryPayout.objects.all():
        payout.turn_no = 1
        payout.cycle_no = 1
        payout.scheduled_date = None
        payout.save(update_fields=["turn_no", "cycle_no", "scheduled_date"])


class Migration(migrations.Migration):

    dependencies = [
        ("merry", "0005_alter_merrypayout_options_and_more"),
    ]

    operations = [
        migrations.RunPython(forward, reverse),
    ]