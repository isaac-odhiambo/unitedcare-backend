import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'unitedcare_backend.settings')

app = Celery('unitedcare_backend')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

CELERY_BEAT_SCHEDULE = {
    "apply-late-fees-nightly": {
        "task": "loans.tasks.apply_late_fees_and_tag_defaulters",
        "schedule": 60 * 60 * 24,  # daily
    }
}