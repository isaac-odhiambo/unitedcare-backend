from celery import shared_task
from django.utils import timezone
from .models import Contribution
from accounts.utils.sms import send_sms


@shared_task
def send_payment_reminders():
    upcoming = Contribution.objects.filter(
        paid=False,
        week_number__gte=1
    )

    for contribution in upcoming:
        phone = contribution.member.user.phone
        message = f"Reminder: Your contribution of KES {contribution.amount} is due."
        send_sms(phone, message)