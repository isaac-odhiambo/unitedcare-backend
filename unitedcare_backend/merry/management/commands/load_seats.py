from django.core.management.base import BaseCommand
from accounts.models import User
from merry.models import MerryGoRound, MerryMember, MerrySeat


class Command(BaseCommand):
    help = "Load seat mapping into MerryGoRound"

    def handle(self, *args, **kwargs):

        merry = MerryGoRound.objects.get(id=3)

        seat_map = [
            (1, "0708343174"),
            (2, "0706799733"),
            (3, None),
            (4, "0708343174"),
            (5, "0704697166"),
            (6, "0721868628"),
            (7, "0793933481"),
            (8, "0703716271"),
            (9, "0703716271"),
            (32, "0703716271"),
            (10, "0714170957"),
            (11, "0707062028"),
            (12, None),
            (13, "0741283677"),
            (15, "0743745544"),
            (16, "0722481230"),
            (17, "0746349176"),
            (18, "0704588872"),
            (21, "0701956902"),
            (25, "0719149980"),
            (26, "0708713031"),
            (29, "0707804228"),
            (30, "0768045262"),
            (34, "0705804410"),
            (35, "0792001687"),
            (36, "0796562854"),
            (37, "0792103404"),
        ]

        created = 0
        updated = 0
        skipped = 0

        for seat, phone in seat_map:

            if not phone:
                self.stdout.write(f"SKIP seat {seat}")
                skipped += 1
                continue

            user = User.objects.filter(phone=phone).first()

            if not user:
                self.stdout.write(f"MISSING USER: {phone}")
                skipped += 1
                continue

            # Ensure member exists
            member, member_created = MerryMember.objects.get_or_create(
                merry=merry,
                user=user,
                defaults={"is_active": True}
            )

            # Create or update seat (CORRECT FIELD: seat_no)
            seat_obj, seat_created = MerrySeat.objects.update_or_create(
                merry=merry,
                member=member,
                seat_no=seat,
                defaults={
                    "payout_position": seat
                }
            )

            if seat_created:
                created += 1
                self.stdout.write(f"CREATED seat {seat} -> {phone}")
            else:
                updated += 1
                self.stdout.write(f"UPDATED seat {seat} -> {phone}")

        self.stdout.write("\n===== SUMMARY =====")
        self.stdout.write(f"Created: {created}")
        self.stdout.write(f"Updated: {updated}")
        self.stdout.write(f"Skipped: {skipped}")