from .models import Notification


def create_notification(
    *,
    user,
    title,
    message,
    notification_type="INFO",
    action_url=None,
    created_by=None,
    loan_id=None,
    merry_id=None,
    group_id=None,
):
    return Notification.objects.create(
        user=user,
        created_by=created_by,
        title=title,
        message=message,
        notification_type=notification_type,
        action_url=action_url,
        loan_id=loan_id,
        merry_id=merry_id,
        group_id=group_id,
    )