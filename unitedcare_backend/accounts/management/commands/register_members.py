from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
import re

User = get_user_model()

# =========================
# 👥 MEMBER LIST
# =========================
members_data = [
    ("Daniel", "0708343174"),
    ("James", "0704697166"),
    ("Thauri", "+254721868628"),
    ("Amos", "0793933481"),
    ("Victor", "0714170957"),
    ("Robert", "0707062028"),
    ("Clare", "0741283677"),
    ("Bashir", "0722481230"),
    ("Edwina", "0719149980"),
    ("Charles", "0708164041"),
    ("Emma", "0707804228"),
    ("Stephene", "0768045262"),
    ("Peter", "0703716271"),
    ("Kenneth", "0792103404"),
    ("Stanley", "0706799733"),

    # ✅ ADDED GEOFFREY
    ("Geoffrey", "0743745544"),
]

DEFAULT_PASSWORD = "password123"


# =========================
# 📞 PHONE NORMALIZER
# =========================
def normalize_phone(phone):
    if not phone:
        return None

    phone = phone.strip().replace(" ", "")

    # convert +2547XXXXXXXX → 07XXXXXXXX
    if phone.startswith("+254"):
        phone = "0" + phone[4:]

    # validate Kenyan format
    if not re.match(r"^(07|01)\d{8}$", phone):
        return None

    return phone


# =========================
# 🚀 COMMAND
# =========================
class Command(BaseCommand):
    help = "Bulk register Merry members"

    def handle(self, *args, **kwargs):

        created = 0
        skipped = 0

        self.stdout.write("\n🚀 Starting member registration...\n")

        for name, phone in members_data:

            phone = normalize_phone(phone)

            # ❌ invalid phone
            if not phone:
                self.stdout.write(self.style.WARNING(f"❌ Invalid phone skipped: {name}"))
                skipped += 1
                continue

            # ❌ duplicate check
            if User.objects.filter(phone=phone).exists():
                self.stdout.write(self.style.WARNING(f"⚠️ Already exists: {name} - {phone}"))
                skipped += 1
                continue

            # ✅ create user
            user = User.objects.create(
                username=name,
                phone=phone,
                email=None,
                role="member",
                status="approved",
                is_active=True,
            )

            user.set_password(DEFAULT_PASSWORD)
            user.save()

            self.stdout.write(self.style.SUCCESS(f"✅ Created: {name} ({phone})"))
            created += 1

        # =========================
        # 📊 SUMMARY
        # =========================
        self.stdout.write("\n======================")
        self.stdout.write(f"Created: {created}")
        self.stdout.write(f"Skipped: {skipped}")
        self.stdout.write("======================\n")