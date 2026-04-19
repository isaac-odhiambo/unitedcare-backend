from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Channel, ChannelPost
from .serializers import ChannelSerializer, ChannelPostSerializer


def user_is_group_member(user, group):
    """
    Adjust this helper if your actual group membership relation differs.

    Current assumptions supported:
    1. group.memberships.filter(user=user).exists()
    2. group.members.filter(id=user.id).exists()
    """
    if not user or not user.is_authenticated or not group:
        return False

    memberships = getattr(group, "memberships", None)
    if memberships is not None:
        try:
            return memberships.filter(user=user).exists()
        except Exception:
            pass

    members = getattr(group, "members", None)
    if members is not None:
        try:
            return members.filter(id=user.id).exists()
        except Exception:
            pass

    return False


def user_can_moderate_channel(user, channel):
    """
    Staff users can moderate any channel.
    For group channels, this can later be extended to support group leaders/admins.
    """
    if not user or not user.is_authenticated:
        return False

    if user.is_staff:
        return True

    return False


class ChannelListView(generics.ListAPIView):
    serializer_class = ChannelSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user

        community_channels = Channel.objects.filter(
            is_active=True,
            channel_type="COMMUNITY",
        )

        group_channels = Channel.objects.filter(
            is_active=True,
            channel_type="GROUP",
        ).select_related("group")

        visible_group_channel_ids = []
        for channel in group_channels:
            if channel.group and user_is_group_member(user, channel.group):
                visible_group_channel_ids.append(channel.id)

        return Channel.objects.filter(
            id__in=list(community_channels.values_list("id", flat=True)) + visible_group_channel_ids
        ).order_by("name")


class ChannelDetailView(generics.RetrieveAPIView):
    serializer_class = ChannelSerializer
    permission_classes = [permissions.IsAuthenticated]
    queryset = Channel.objects.filter(is_active=True)

    def get_object(self):
        channel = super().get_object()
        user = self.request.user

        if channel.channel_type == "GROUP":
            if not channel.group or not user_is_group_member(user, channel.group):
                raise PermissionDenied("You do not have access to this group channel.")

        return channel


class ChannelPostListView(generics.ListAPIView):
    serializer_class = ChannelPostSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        channel_id = self.kwargs["channel_id"]

        try:
            channel = Channel.objects.select_related("group").get(
                id=channel_id,
                is_active=True,
            )
        except Channel.DoesNotExist:
            return ChannelPost.objects.none()

        if channel.channel_type == "GROUP":
            if not channel.group or not user_is_group_member(user, channel.group):
                return ChannelPost.objects.none()

        return ChannelPost.objects.filter(
            channel=channel,
            status="APPROVED",
        ).select_related("user", "approved_by")


class SubmitChannelPostView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, channel_id):
        user = request.user

        try:
            channel = Channel.objects.select_related("group").get(
                id=channel_id,
                is_active=True,
            )
        except Channel.DoesNotExist:
            return Response(
                {"detail": "Channel not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if channel.channel_type == "GROUP":
            if not channel.group or not user_is_group_member(user, channel.group):
                return Response(
                    {"detail": "You are not a member of this group channel."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        if not channel.allow_member_submissions and not user_can_moderate_channel(user, channel):
            return Response(
                {"detail": "This channel is not accepting member submissions."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = ChannelPostSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        can_publish_directly = user_can_moderate_channel(user, channel)

        post = ChannelPost.objects.create(
            channel=channel,
            user=user,
            title=serializer.validated_data.get("title", ""),
            content=serializer.validated_data["content"],
            message_type=serializer.validated_data.get("message_type", "NOTICE"),
            status="APPROVED" if can_publish_directly else "PENDING",
            approved_by=user if can_publish_directly else None,
            approved_at=timezone.now() if can_publish_directly else None,
        )

        return Response(
            ChannelPostSerializer(post).data,
            status=status.HTTP_201_CREATED,
        )


class MyChannelSubmissionsView(generics.ListAPIView):
    serializer_class = ChannelPostSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ChannelPost.objects.filter(
            user=self.request.user
        ).select_related("channel", "user", "approved_by").order_by("-created_at")


class PendingChannelPostsView(generics.ListAPIView):
    serializer_class = ChannelPostSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user

        if not user.is_staff:
            return ChannelPost.objects.none()

        return ChannelPost.objects.filter(
            status="PENDING"
        ).select_related("channel", "user", "approved_by").order_by("-created_at")


class ApproveChannelPostView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, post_id):
        user = request.user

        try:
            post = ChannelPost.objects.select_related("channel").get(id=post_id)
        except ChannelPost.DoesNotExist:
            return Response(
                {"detail": "Post not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not user_can_moderate_channel(user, post.channel):
            return Response(
                {"detail": "You do not have permission to approve this post."},
                status=status.HTTP_403_FORBIDDEN,
            )

        post.status = "APPROVED"
        post.approved_by = user
        post.approved_at = timezone.now()
        post.rejection_reason = ""
        post.save(update_fields=["status", "approved_by", "approved_at", "rejection_reason", "updated_at"])

        return Response(ChannelPostSerializer(post).data, status=status.HTTP_200_OK)


class RejectChannelPostView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, post_id):
        user = request.user
        rejection_reason = (request.data.get("rejection_reason") or "").strip()

        try:
            post = ChannelPost.objects.select_related("channel").get(id=post_id)
        except ChannelPost.DoesNotExist:
            return Response(
                {"detail": "Post not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not user_can_moderate_channel(user, post.channel):
            return Response(
                {"detail": "You do not have permission to reject this post."},
                status=status.HTTP_403_FORBIDDEN,
            )

        post.status = "REJECTED"
        post.approved_by = None
        post.approved_at = None
        post.rejection_reason = rejection_reason
        post.save(update_fields=["status", "approved_by", "approved_at", "rejection_reason", "updated_at"])

        return Response(ChannelPostSerializer(post).data, status=status.HTTP_200_OK)


class HideChannelPostView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, post_id):
        user = request.user

        try:
            post = ChannelPost.objects.select_related("channel").get(id=post_id)
        except ChannelPost.DoesNotExist:
            return Response(
                {"detail": "Post not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not user_can_moderate_channel(user, post.channel):
            return Response(
                {"detail": "You do not have permission to hide this post."},
                status=status.HTTP_403_FORBIDDEN,
            )

        post.status = "HIDDEN"
        post.save(update_fields=["status", "updated_at"])

        return Response(ChannelPostSerializer(post).data, status=status.HTTP_200_OK)


class PinChannelPostView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, post_id):
        user = request.user

        try:
            post = ChannelPost.objects.select_related("channel").get(id=post_id)
        except ChannelPost.DoesNotExist:
            return Response(
                {"detail": "Post not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not user_can_moderate_channel(user, post.channel):
            return Response(
                {"detail": "You do not have permission to pin this post."},
                status=status.HTTP_403_FORBIDDEN,
            )

        ChannelPost.objects.filter(channel=post.channel, is_pinned=True).update(is_pinned=False)
        post.is_pinned = True
        post.save(update_fields=["is_pinned", "updated_at"])

        return Response(ChannelPostSerializer(post).data, status=status.HTTP_200_OK)


class UnpinChannelPostView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, post_id):
        user = request.user

        try:
            post = ChannelPost.objects.select_related("channel").get(id=post_id)
        except ChannelPost.DoesNotExist:
            return Response(
                {"detail": "Post not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not user_can_moderate_channel(user, post.channel):
            return Response(
                {"detail": "You do not have permission to unpin this post."},
                status=status.HTTP_403_FORBIDDEN,
            )

        post.is_pinned = False
        post.save(update_fields=["is_pinned", "updated_at"])

        return Response(ChannelPostSerializer(post).data, status=status.HTTP_200_OK)