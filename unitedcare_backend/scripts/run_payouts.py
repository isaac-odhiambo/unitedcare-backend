import os
import sys
from decimal import Decimal
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(BASE_DIR)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "unitedcare_backend.settings")

import django
django.setup()

from django.db import transaction
from django.utils import timezone

from merry.models import MerryGoRound, MerrySeat, MerryPayout


# ===============================
# CONFIG
# ===============================
MERRY_ID = 3
START_DATE = timezone.make_aware(datetime(2026, 3, 27))
AMOUNT = Decimal("1000")

SEED_MODE = True   # 🔥 SET False in production LIVE mode
RESET_FIRST = True  # 🔥 SAFE CLEAN START FOR BACKDATING


# ===============================
# 1. LOAD MERRY
# ===============================
merry = MerryGoRound.objects.get(id=MERRY_ID)


# ===============================
# 2. LOAD SEATS
# ===============================
seats = list(
    MerrySeat.objects.filter(merry=merry, is_active=True)
    .order_by("seat_no")
)

if not seats:
    raise Exception("No active seats found")

print(f"TOTAL SEATS: {len(seats)}")


# ===============================
# 3. RESET (ONLY FOR BACKDATING)
# ===============================
if RESET_FIRST:
    deleted, _ = MerryPayout.objects.filter(merry=merry).delete()
    print(f"RESET DONE → Deleted {deleted} payouts")


# ===============================
# 4. GENERATE MONDAY + FRIDAY DATES
# ===============================
def generate_payout_dates(start_date, total):
    dates = []
    current = start_date

    while len(dates) < total:
        if current.weekday() in [0, 4]:  # Monday & Friday
            dates.append(current)
        current += timedelta(days=1)

    return dates


dates = generate_payout_dates(START_DATE, len(seats))

print(f"TOTAL DATES: {len(dates)}")


# ===============================
# 5. SEED PAYOUTS (SAFE + IDPOTENT)
# ===============================
with transaction.atomic():

    for i, (seat, payout_date) in enumerate(zip(seats, dates), start=1):

        period_key = payout_date.date().isoformat()

        status = "PAID" if i <= 10 else "PENDING"

        payout, created = MerryPayout.objects.update_or_create(
            merry=merry,
            seat=seat,
            period_key=period_key,
            defaults={
                "turn_no": i,
                "slot_no": i,
                "amount": AMOUNT,
                "status": status,
            }
        )

        print(
            f"{'CREATED' if created else 'UPDATED'} → "
            f"TURN {i:03d} | SEAT {seat.seat_no} | {period_key} | {status}"
        )

print("✅ PAYOUT SEEDING COMPLETE (CLEAN + SAFE + NO DUPLICATES)")