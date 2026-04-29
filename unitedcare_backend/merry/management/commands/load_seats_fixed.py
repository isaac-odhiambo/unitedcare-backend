from django.core.management.base import BaseCommand
from accounts.models import User
from merry.models import MerryGoRound, MerryMember, MerrySeat


class Command(BaseCommand):
    help = "Load seats for MerryGoRound safely"

    def handle(self, *args, **kwargs):

        merry = MerryGoRound.objects.get(id=3)

        seat_map = [
            (1, "0793933481", "Amos"),
            (2, "0706799733", "Stanley"),
            (3, None, "Moses"),
            (4, "0708343174", "Daniel"),
            (5, "0704697166", "James"),
            (6, "0721868628", "Thauri"),
            (7, "0793933481", "Amos"),
            (8, "0703716271", "Peter"),
            (9, "0703716271", "Peter"),
            (10, "0714170957", "Victor"),
            (11, "0707062028", "Robert"),
            (12, None, "Helen"),
            (13, "0741283677", "Clare"),
            (14, "0703716271", "Peter"),
            (15, "0743745544", "Geoffrey"),
            (16, "0722481230", "Bashir"),
            (17, "0746349176", "Ibrahim"),

            # ✅ FIXED JULIUS (same phone both seats)
            (18, "0704588872", "Julius"),
            (19, "0704588872", "Julius"),

            # ✅ ISAAC (already correct)
            (20, "0701956902", "Isaac"),
            (21, "0701956902", "Isaac"),

            # ✅ FIXED ROSEBELA (same phone both seats)
            (22, "0724451517", "Rosebela"),
            (23, "0724451517", "Rosebela"),

            (24, None, "Felix"),
            (25, "0719149980", "Edwina"),
            (26, "0708713031", "Eric"),

            # ✅ CHARLES (already correct)
            (27, "0708164041", "Charles"),
            (28, "0708164041", "Charles"),

            (29, "0707804228", "Emma"),
            (30, "0768045262", "Stephene"),
            (31, "0703716271", "Peter"),
            (32, "0703716271", "Peter"),
            (33, None, "Abdi"),
            (34, "0705804410", "Kelvin"),
            (35, "0792001687", "Enosh"),
            (36, "0796562854", "Alex"),
            (37, "0792103404", "Kenneth"),
        ]

        created = 0
        updated = 0
        skipped = 0

        for seat_no, phone, name in seat_map:

            if not phone:
                self.stdout.write(f"SKIP seat {seat_no} ({name}) - no phone")
                skipped += 1
                continue

            user = User.objects.filter(phone=phone).first()

            if not user:
                self.stdout.write(f"MISSING USER: {name} ({phone})")
                skipped += 1
                continue

            member, _ = MerryMember.objects.get_or_create(
                merry=merry,
                user=user
            )

            seat_obj, is_created = MerrySeat.objects.update_or_create(
                merry=merry,
                payout_position=seat_no,
                defaults={
                    "seat_no": seat_no,
                    "member": member
                }
            )

            if is_created:
                created += 1
                self.stdout.write(f"CREATED seat {seat_no} -> {name}")
            else:
                updated += 1
                self.stdout.write(f"UPDATED seat {seat_no} -> {name}")

        self.stdout.write(self.style.SUCCESS(
            f"\nDONE: Created={created}, Updated={updated}, Skipped={skipped}"
        ))