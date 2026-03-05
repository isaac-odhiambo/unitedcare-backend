# accounts/management/commands/check_otp.py
from django.core.management.base import BaseCommand
from accounts.models import OTP

class Command(BaseCommand):
    help = "Check the latest OTP for a phone number"

    def add_arguments(self, parser):
        parser.add_argument("phone", type=str, help="Phone number to check OTP")

    def handle(self, *args, **options):
        phone = options["phone"]
        latest_otp = OTP.objects.filter(phone=phone).order_by("-created_at").first()
        if latest_otp:
            self.stdout.write(f"Latest OTP for {phone}: {latest_otp.code}")
            self.stdout.write(f"Created at: {latest_otp.created_at}")
            self.stdout.write(f"Used? {latest_otp.is_used}")
            self.stdout.write(f"Attempts: {latest_otp.attempts}")
            self.stdout.write(f"Expired? {latest_otp.is_expired()}")
            self.stdout.write(f"Locked? {latest_otp.is_locked()}")
            
            # Optionally mark as used
            latest_otp.mark_used()
            self.stdout.write(self.style.SUCCESS("OTP marked as used ✅"))
        else:
            self.stdout.write(self.style.WARNING("No OTP found for this phone"))