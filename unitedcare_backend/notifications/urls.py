from django.urls import path
from .views import (
    AdminSendNotificationView,
    DeleteNotificationView,
    MarkAllNotificationsReadView,
    MarkNotificationReadView,
    MyNotificationListView,
    UnreadNotificationCountView,
)

urlpatterns = [
    path("", MyNotificationListView.as_view(), name="my-notifications"),
    path("unread-count/", UnreadNotificationCountView.as_view(), name="notification-unread-count"),
    path("read-all/", MarkAllNotificationsReadView.as_view(), name="notification-read-all"),
    path("<int:pk>/read/", MarkNotificationReadView.as_view(), name="notification-read-one"),
    path("<int:pk>/delete/", DeleteNotificationView.as_view(), name="notification-delete-one"),
    path("admin/send/", AdminSendNotificationView.as_view(), name="admin-send-notification"),
]