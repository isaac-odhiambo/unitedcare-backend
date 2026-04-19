from django.conf import settings
from django.db import models


class Channel(models.Model):
    CHANNEL_TYPES = (
        ("COMMUNITY", "Community"),
        ("GROUP", "Group"),
    )

    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    channel_type = models.CharField(max_length=20, choices=CHANNEL_TYPES)
    group = models.ForeignKey(
        "groups.Group",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="channels",
    )
    is_active = models.BooleanField(default=True)
    allow_member_submissions = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ChannelPost(models.Model):
    POST_STATUS = (
        ("PENDING", "Pending"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
        ("HIDDEN", "Hidden"),
    )

    MESSAGE_TYPES = (
        ("ANNOUNCEMENT", "Announcement"),
        ("REMINDER", "Reminder"),
        ("ENCOURAGEMENT", "Encouragement"),
        ("SUPPORT", "Support"),
        ("CONDOLENCE", "Condolence"),
        ("CELEBRATION", "Celebration"),
        ("NOTICE", "Notice"),
    )

    channel = models.ForeignKey(
        Channel,
        on_delete=models.CASCADE,
        related_name="posts",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="channel_posts",
    )
    title = models.CharField(max_length=180, blank=True)
    content = models.TextField()
    message_type = models.CharField(
        max_length=30,
        choices=MESSAGE_TYPES,
        default="NOTICE",
    )
    status = models.CharField(
        max_length=20,
        choices=POST_STATUS,
        default="PENDING",
    )
    is_pinned = models.BooleanField(default=False)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_channel_posts",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_pinned", "-created_at"]

    def __str__(self):
        return f"{self.channel.name} | {self.user}"