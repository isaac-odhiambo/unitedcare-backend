from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Notification
from .serializers import NotificationSerializer, CreateNotificationSerializer
from .utils import create_notification

User = get_user_model()


class IsAdminOrSuperuser(permissions.BasePermission):
    """
    Adjust this logic to match your user model.
    """

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False

        if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
            return True

        role = getattr(user, "role", None)
        if role and str(role).lower() == "admin":
            return True

        return False


class MyNotificationListView(generics.ListAPIView):
    serializer_class = NotificationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Notification.objects.filter(
            user=self.request.user,
            is_deleted=False,
        ).order_by("-created_at")


class UnreadNotificationCountView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        count = Notification.objects.filter(
            user=request.user,
            is_read=False,
            is_deleted=False,
        ).count()
        return Response({"unread_count": count}, status=status.HTTP_200_OK)


class MarkNotificationReadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        try:
            notification = Notification.objects.get(
                id=pk,
                user=request.user,
                is_deleted=False,
            )
        except Notification.DoesNotExist:
            return Response(
                {"detail": "Notification not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not notification.is_read:
            notification.is_read = True
            notification.read_at = timezone.now()
            notification.save(update_fields=["is_read", "read_at"])

        return Response(
            {"detail": "Notification marked as read."},
            status=status.HTTP_200_OK,
        )


class MarkAllNotificationsReadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        Notification.objects.filter(
            user=request.user,
            is_read=False,
            is_deleted=False,
        ).update(
            is_read=True,
            read_at=timezone.now(),
        )

        return Response(
            {"detail": "All notifications marked as read."},
            status=status.HTTP_200_OK,
        )


class DeleteNotificationView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        try:
            notification = Notification.objects.get(
                id=pk,
                user=request.user,
                is_deleted=False,
            )
        except Notification.DoesNotExist:
            return Response(
                {"detail": "Notification not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        notification.is_deleted = True
        notification.save(update_fields=["is_deleted"])

        return Response(
            {"detail": "Notification deleted."},
            status=status.HTTP_200_OK,
        )


class AdminSendNotificationView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAdminOrSuperuser]

    def post(self, request):
        serializer = CreateNotificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            target_user = User.objects.get(id=data["user_id"])
        except User.DoesNotExist:
            return Response(
                {"detail": "Target user not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        notification = create_notification(
            user=target_user,
            created_by=request.user,
            title=data["title"],
            message=data["message"],
            notification_type=data.get("notification_type", "INFO"),
            action_url=data.get("action_url"),
            loan_id=data.get("loan_id"),
            merry_id=data.get("merry_id"),
            group_id=data.get("group_id"),
        )

        return Response(
            {
                "detail": "Notification sent successfully.",
                "notification": NotificationSerializer(notification).data,
            },
            status=status.HTTP_201_CREATED,
        )