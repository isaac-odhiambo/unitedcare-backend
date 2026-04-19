from django.urls import path

from .views import (
    ApproveChannelPostView,
    ChannelDetailView,
    ChannelListView,
    ChannelPostListView,
    HideChannelPostView,
    MyChannelSubmissionsView,
    PendingChannelPostsView,
    PinChannelPostView,
    RejectChannelPostView,
    SubmitChannelPostView,
    UnpinChannelPostView,
)

urlpatterns = [
    # =========================================
    # CHANNELS
    # =========================================
    path("", ChannelListView.as_view(), name="channel-list"),
    path("<int:pk>/", ChannelDetailView.as_view(), name="channel-detail"),

    # =========================================
    # POSTS (PER CHANNEL)
    # =========================================
    path("<int:channel_id>/posts/", ChannelPostListView.as_view(), name="channel-post-list"),
    path("<int:channel_id>/submit-post/", SubmitChannelPostView.as_view(), name="channel-submit-post"),

    # =========================================
    # USER SUBMISSIONS
    # =========================================
    path("my-submissions/", MyChannelSubmissionsView.as_view(), name="my-channel-submissions"),

    # =========================================
    # MODERATION
    # =========================================
    path("moderation/pending/", PendingChannelPostsView.as_view(), name="channel-pending-posts"),
    path("posts/<int:post_id>/approve/", ApproveChannelPostView.as_view(), name="channel-post-approve"),
    path("posts/<int:post_id>/reject/", RejectChannelPostView.as_view(), name="channel-post-reject"),
    path("posts/<int:post_id>/hide/", HideChannelPostView.as_view(), name="channel-post-hide"),
    path("posts/<int:post_id>/pin/", PinChannelPostView.as_view(), name="channel-post-pin"),
    path("posts/<int:post_id>/unpin/", UnpinChannelPostView.as_view(), name="channel-post-unpin"),
]