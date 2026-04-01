from django.core.management.base import BaseCommand
from payments.mpesa_c2b import register_c2b_urls


class Command(BaseCommand):
    help = "Register M-Pesa C2B validation and confirmation URLs"

    def handle(self, *args, **options):
        result = register_c2b_urls()
        self.stdout.write(self.style.SUCCESS(f"Success: {result}"))