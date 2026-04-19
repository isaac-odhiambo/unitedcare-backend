from __future__ import annotations

from decimal import Decimal
from typing import Optional, Tuple

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone


# ==========================================================
# GROUP
# ==========================================================
class Group(models.Model):
    GROUP_TYPES = (
        ("BURIAL", "Burial/Welfare"),
        ("WEDDING", "Wedding"),
        ("EDUCATION", "Education/School Fees"),
        ("INVESTMENT", "Investment"),
        ("SAVINGS", "Savings"),
        ("EMERGENCY", "Emergency Support"),
        ("DEVELOPMENT", "Community Development"),
        ("OTHER", "Other"),
    )

    VISIBILITY_CHOICES = (
        ("PUBLIC", "Public"),
        ("PRIVATE", "Private"),
    )

    JOIN_POLICY_CHOICES = (
        ("OPEN", "Open"),
        ("APPROVAL", "Approval Required"),
        ("CLOSED", "Closed"),
    )

    name = models.CharField(max_length=150, unique=True)
    group_type = models.CharField(max_length=20, choices=GROUP_TYPES, default="OTHER")
    description = models.TextField(blank=True, default="")
    objective = models.CharField(max_length=255, blank=True, default="")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="groups_created",
    )

    visibility = models.CharField(max_length=20, choices=VISIBILITY_CHOICES, default="PUBLIC")
    join_policy = models.CharField(max_length=20, choices=JOIN_POLICY_CHOICES, default="APPROVAL")

    is_active = models.BooleanField(default=True)
    max_members = models.PositiveIntegerField(default=0, help_text="0 means unlimited members")

    # Manual payment identifier for references like UN1, WF12, MG7
    # Rule:
    # - letters only
    # - unique system-wide
    # - backend will later parse: <payment_code><user_id>
    payment_code = models.CharField(
        max_length=10,
        unique=True,
        null=True,
        blank=True,
        help_text="Unique letters-only code used in manual payment references, e.g. UN, WF, MG.",
    )

    # Optional contribution settings
    requires_contributions = models.BooleanField(default=False)
    contribution_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    contribution_frequency = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Example: WEEKLY, MONTHLY, ONCE, ADHOC",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["group_type", "is_active"]),
            models.Index(fields=["join_policy", "is_active"]),
            models.Index(fields=["visibility", "is_active"]),
            models.Index(fields=["payment_code"]),
        ]

    def clean(self):
        if self.max_members < 0:
            raise ValidationError("max_members cannot be negative.")

        if self.requires_contributions and self.contribution_amount <= 0:
            raise ValidationError(
                "Contribution amount must be greater than 0 if contributions are required."
            )

        if self.payment_code:
            self.payment_code = self.payment_code.strip().upper()

            if not self.payment_code.isalpha():
                raise ValidationError(
                    {"payment_code": "Payment code must contain letters only, e.g. UN, WF, MG."}
                )

    def save(self, *args, **kwargs):
        if self.payment_code:
            self.payment_code = self.payment_code.strip().upper()
        self.full_clean()
        super().save(*args, **kwargs)

    def active_members_count(self) -> int:
        return self.memberships.filter(is_active=True).count()

    def available_slots(self) -> Optional[int]:
        if not self.max_members or self.max_members <= 0:
            return None
        remaining = self.max_members - self.active_members_count()
        return remaining if remaining > 0 else 0

    def can_accept_member(self) -> Tuple[bool, str]:
        if not self.is_active:
            return False, "This group is inactive."

        if self.join_policy == "CLOSED":
            return False, "This group is closed for joining."

        if self.max_members and self.max_members > 0:
            remaining = self.available_slots() or 0
            if remaining <= 0:
                return False, "This group has reached maximum members."

        return True, "OK"

    @property
    def manual_payment_reference_prefix(self) -> str:
        return (self.payment_code or "").strip().upper()

    def build_member_reference(self, user_id: int) -> str:
        code = (self.payment_code or "").strip().upper()
        if not code:
            raise ValidationError("This group does not have a payment_code yet.")
        return f"{code}{int(user_id)}"

    def __str__(self):
        return f"{self.name} ({self.get_group_type_display()})"


# ==========================================================
# MEMBERSHIP
# ==========================================================
class GroupMembership(models.Model):
    ROLE_CHOICES = (
        ("MEMBER", "Member"),
        ("ADMIN", "Admin"),
        ("TREASURER", "Treasurer"),
        ("SECRETARY", "Secretary"),
    )

    group = models.ForeignKey(
        Group,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="group_memberships",
    )

    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="MEMBER")
    is_active = models.BooleanField(default=True)
    joined_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["group", "user"], name="uniq_group_user_membership"),
        ]
        indexes = [
            models.Index(fields=["group", "is_active"]),
            models.Index(fields=["user", "is_active"]),
            models.Index(fields=["group", "role"]),
        ]

    def __str__(self):
        return f"{self.group.name} - {self.user} ({self.role})"


# ==========================================================
# GROUP DEPENDANTS
# ==========================================================
class GroupDependant(models.Model):
    RELATIONSHIP_CHOICES = (
        ("SPOUSE", "Spouse"),
        ("CHILD", "Child"),
        ("SIBLING", "Sibling"),
        ("PARENT", "Parent"),
        ("OTHER", "Other"),
    )

    membership = models.ForeignKey(
        GroupMembership,
        on_delete=models.CASCADE,
        related_name="dependants",
    )
    name = models.CharField(max_length=120)
    relationship = models.CharField(max_length=20, choices=RELATIONSHIP_CHOICES, default="OTHER")
    date_of_birth = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["membership", "is_active"]),
            models.Index(fields=["relationship", "is_active"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["membership", "name", "relationship"],
                name="uniq_membership_dependant_name_relationship",
            ),
        ]

    def clean(self):
        self.name = (self.name or "").strip()
        self.note = (self.note or "").strip()

        if not self.name:
            raise ValidationError({"name": "Dependant name is required."})

        if self.membership_id:
            if not self.membership.is_active:
                raise ValidationError("You can only add dependants to an active membership.")

    @property
    def group(self):
        return self.membership.group

    @property
    def user(self):
        return self.membership.user

    def __str__(self):
        return (
            f"{self.name} ({self.get_relationship_display()}) - "
            f"{self.membership.group.name} / user {self.membership.user_id}"
        )


# ==========================================================
# JOIN REQUESTS
# ==========================================================
class GroupJoinRequest(models.Model):
    STATUS_CHOICES = (
        ("PENDING", "Pending"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
        ("CANCELLED", "Cancelled"),
    )

    group = models.ForeignKey(
        Group,
        on_delete=models.CASCADE,
        related_name="join_requests",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="group_join_requests",
    )

    note = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING")

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_group_join_requests",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["group", "user"],
                condition=Q(status="PENDING"),
                name="uniq_pending_group_join_request_per_user",
            ),
        ]
        indexes = [
            models.Index(fields=["group", "status", "created_at"]),
            models.Index(fields=["user", "status", "created_at"]),
        ]

    def clean(self):
        if self.group_id:
            ok, reason = self.group.can_accept_member()
            if not ok:
                raise ValidationError(reason)

        if self.group_id and self.user_id:
            already_member = GroupMembership.objects.filter(
                group_id=self.group_id,
                user_id=self.user_id,
                is_active=True,
            ).exists()
            if already_member:
                raise ValidationError("You are already an active member of this group.")

    @transaction.atomic
    def approve(self, admin_user):
        if self.status != "PENDING":
            raise ValidationError("Only pending requests can be approved.")

        req = (
            GroupJoinRequest.objects.select_for_update()
            .select_related("group", "user")
            .get(pk=self.pk)
        )

        ok, reason = req.group.can_accept_member()
        if not ok:
            raise ValidationError(reason)

        membership, created = GroupMembership.objects.get_or_create(
            group=req.group,
            user=req.user,
            defaults={
                "role": "MEMBER",
                "is_active": True,
                "joined_at": timezone.now(),
            },
        )

        if not created and not membership.is_active:
            membership.is_active = True
            membership.joined_at = membership.joined_at or timezone.now()
            membership.save(update_fields=["is_active", "joined_at"])

        GroupFund.objects.get_or_create(group=req.group)
        GroupMemberShare.objects.get_or_create(group=req.group, user=req.user)

        req.status = "APPROVED"
        req.reviewed_by = admin_user
        req.reviewed_at = timezone.now()
        req.save(update_fields=["status", "reviewed_by", "reviewed_at"])

        return membership

    def reject(self, admin_user, note: str = ""):
        if self.status != "PENDING":
            raise ValidationError("Only pending requests can be rejected.")

        self.status = "REJECTED"
        self.reviewed_by = admin_user
        self.reviewed_at = timezone.now()
        if note:
            self.note = note[:255]
        self.save(update_fields=["status", "reviewed_by", "reviewed_at", "note"])

    def cancel(self, user):
        if self.user_id != user.id:
            raise ValidationError("You can only cancel your own join request.")
        if self.status != "PENDING":
            raise ValidationError("Only pending requests can be cancelled.")

        self.status = "CANCELLED"
        self.save(update_fields=["status"])

    def __str__(self):
        return f"GroupJoinRequest#{self.id} group={self.group_id} user={self.user_id} {self.status}"


# ==========================================================
# GROUP FUND
# ==========================================================
class GroupFund(models.Model):
    """
    Group-owned pooled money.
    """
    group = models.OneToOneField(Group, on_delete=models.CASCADE, related_name="fund")
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    reserved_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=Q(balance__gte=0),
                name="groupfund_balance_non_negative",
            ),
            models.CheckConstraint(
                check=Q(reserved_amount__gte=0),
                name="groupfund_reserved_non_negative",
            ),
        ]

    @property
    def available_balance(self) -> Decimal:
        available = (self.balance or Decimal("0.00")) - (self.reserved_amount or Decimal("0.00"))
        return available if available > 0 else Decimal("0.00")

    def clean(self):
        if self.reserved_amount > self.balance:
            raise ValidationError("Reserved amount cannot exceed balance.")

    def __str__(self):
        return f"GroupFund group={self.group_id} balance={self.balance}"


# ==========================================================
# MEMBER SHARE
# ==========================================================
class GroupMemberShare(models.Model):
    """
    Tracks each member's accumulated contribution share inside a group.
    Useful for savings/investment/welfare accountability and future collateral logic.
    """
    group = models.ForeignKey(
        Group,
        on_delete=models.CASCADE,
        related_name="member_shares",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="group_shares",
    )

    total_contributed = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    reserved_share = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["group", "user"], name="uniq_group_member_share"),
            models.CheckConstraint(
                check=Q(total_contributed__gte=0),
                name="groupshare_total_non_negative",
            ),
            models.CheckConstraint(
                check=Q(reserved_share__gte=0),
                name="groupshare_reserved_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["group", "user"]),
        ]

    @property
    def available_share(self) -> Decimal:
        available = (self.total_contributed or Decimal("0.00")) - (self.reserved_share or Decimal("0.00"))
        return available if available > 0 else Decimal("0.00")

    def clean(self):
        if self.reserved_share > self.total_contributed:
            raise ValidationError("Reserved share cannot exceed total contributed.")

    def __str__(self):
        return f"Share g={self.group_id} u={self.user_id} total={self.total_contributed}"


# ==========================================================
# CONTRIBUTIONS
# ==========================================================
class GroupContribution(models.Model):
    """
    Pure contribution history record.

    Important:
    - This model does NOT update GroupFund or GroupMemberShare directly.
    - All accounting mutations should happen in groups/services.py
      inside post_group_contribution().
    """
    SOURCE_CHOICES = (
        ("MANUAL", "Manual"),
        ("MPESA", "M-Pesa"),
        ("BANK", "Bank"),
        ("OTHER", "Other"),
    )

    group = models.ForeignKey(
        Group,
        on_delete=models.CASCADE,
        related_name="contributions",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="group_contributions",
    )

    amount = models.DecimalField(max_digits=14, decimal_places=2)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="MANUAL")
    reference = models.CharField(max_length=120, null=True, blank=True)
    note = models.CharField(max_length=255, null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["group", "user", "created_at"]),
            models.Index(fields=["reference"]),
            models.Index(fields=["source", "created_at"]),
        ]

    def clean(self):
        if self.amount is None or self.amount <= 0:
            raise ValidationError("Contribution amount must be greater than 0.")

        if self.group_id and self.user_id:
            is_member = GroupMembership.objects.filter(
                group_id=self.group_id,
                user_id=self.user_id,
                is_active=True,
            ).exists()
            if not is_member:
                raise ValidationError("Only active group members can contribute.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Contribution g={self.group_id} u={self.user_id} amount={self.amount}"


# ==========================================================
# SHARE HOLD
# ==========================================================
class GroupShareHold(models.Model):
    """
    Lock a member's share for a loan or obligation inside the group.
    loan_id is kept as IntegerField for flexibility across apps.
    """
    group = models.ForeignKey(
        Group,
        on_delete=models.CASCADE,
        related_name="share_holds",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="group_share_holds",
    )

    loan_id = models.IntegerField(db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["group", "user", "loan_id", "is_active"]),
        ]

    def clean(self):
        if self.amount is None or self.amount <= 0:
            raise ValidationError("Hold amount must be greater than 0.")

    def release(self):
        if not self.is_active:
            return
        self.is_active = False
        self.released_at = timezone.now()
        self.save(update_fields=["is_active", "released_at"])

    def __str__(self):
        return (
            f"GroupShareHold loan={self.loan_id} "
            f"group={self.group_id} user={self.user_id} amount={self.amount}"
        )

# # groups/models.py
# from __future__ import annotations

# from decimal import Decimal
# from typing import Optional, Tuple

# from django.conf import settings
# from django.core.exceptions import ValidationError
# from django.db import models, transaction
# from django.db.models import Q
# from django.utils import timezone


# # ==========================================================
# # GROUP
# # ==========================================================
# class Group(models.Model):
#     GROUP_TYPES = (
#         ("BURIAL", "Burial/Welfare"),
#         ("WEDDING", "Wedding"),
#         ("EDUCATION", "Education/School Fees"),
#         ("INVESTMENT", "Investment"),
#         ("SAVINGS", "Savings"),
#         ("EMERGENCY", "Emergency Support"),
#         ("DEVELOPMENT", "Community Development"),
#         ("OTHER", "Other"),
#     )

#     VISIBILITY_CHOICES = (
#         ("PUBLIC", "Public"),
#         ("PRIVATE", "Private"),
#     )

#     JOIN_POLICY_CHOICES = (
#         ("OPEN", "Open"),
#         ("APPROVAL", "Approval Required"),
#         ("CLOSED", "Closed"),
#     )

#     name = models.CharField(max_length=150, unique=True)
#     group_type = models.CharField(max_length=20, choices=GROUP_TYPES, default="OTHER")
#     description = models.TextField(blank=True, default="")
#     objective = models.CharField(max_length=255, blank=True, default="")

#     created_by = models.ForeignKey(
#         settings.AUTH_USER_MODEL,
#         on_delete=models.CASCADE,
#         related_name="groups_created",
#     )

#     visibility = models.CharField(max_length=20, choices=VISIBILITY_CHOICES, default="PUBLIC")
#     join_policy = models.CharField(max_length=20, choices=JOIN_POLICY_CHOICES, default="APPROVAL")

#     is_active = models.BooleanField(default=True)
#     max_members = models.PositiveIntegerField(default=0, help_text="0 means unlimited members")

#     # Manual payment identifier for references like UN1, WF12, MG7
#     # Rule:
#     # - letters only
#     # - unique system-wide
#     # - backend will later parse: <payment_code><user_id>
#     payment_code = models.CharField(
#         max_length=10,
#         unique=True,
#         null=True,
#         blank=True,
#         help_text="Unique letters-only code used in manual payment references, e.g. UN, WF, MG.",
#     )

#     # Optional contribution settings
#     requires_contributions = models.BooleanField(default=False)
#     contribution_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
#     contribution_frequency = models.CharField(
#         max_length=20,
#         blank=True,
#         default="",
#         help_text="Example: WEEKLY, MONTHLY, ONCE, ADHOC",
#     )

#     created_at = models.DateTimeField(auto_now_add=True)
#     updated_at = models.DateTimeField(auto_now=True)

#     class Meta:
#         ordering = ["-id"]
#         indexes = [
#             models.Index(fields=["group_type", "is_active"]),
#             models.Index(fields=["join_policy", "is_active"]),
#             models.Index(fields=["visibility", "is_active"]),
#             models.Index(fields=["payment_code"]),
#         ]

#     def clean(self):
#         if self.max_members < 0:
#             raise ValidationError("max_members cannot be negative.")

#         if self.requires_contributions and self.contribution_amount <= 0:
#             raise ValidationError(
#                 "Contribution amount must be greater than 0 if contributions are required."
#             )

#         if self.payment_code:
#             self.payment_code = self.payment_code.strip().upper()

#             if not self.payment_code.isalpha():
#                 raise ValidationError(
#                     {"payment_code": "Payment code must contain letters only, e.g. UN, WF, MG."}
#                 )

#     def save(self, *args, **kwargs):
#         if self.payment_code:
#             self.payment_code = self.payment_code.strip().upper()
#         self.full_clean()
#         super().save(*args, **kwargs)

#     def active_members_count(self) -> int:
#         return self.memberships.filter(is_active=True).count()

#     def available_slots(self) -> Optional[int]:
#         if not self.max_members or self.max_members <= 0:
#             return None
#         remaining = self.max_members - self.active_members_count()
#         return remaining if remaining > 0 else 0

#     def can_accept_member(self) -> Tuple[bool, str]:
#         if not self.is_active:
#             return False, "This group is inactive."

#         if self.join_policy == "CLOSED":
#             return False, "This group is closed for joining."

#         if self.max_members and self.max_members > 0:
#             remaining = self.available_slots() or 0
#             if remaining <= 0:
#                 return False, "This group has reached maximum members."

#         return True, "OK"

#     @property
#     def manual_payment_reference_prefix(self) -> str:
#         return (self.payment_code or "").strip().upper()

#     def build_member_reference(self, user_id: int) -> str:
#         code = (self.payment_code or "").strip().upper()
#         if not code:
#             raise ValidationError("This group does not have a payment_code yet.")
#         return f"{code}{int(user_id)}"

#     def __str__(self):
#         return f"{self.name} ({self.get_group_type_display()})"


# # ==========================================================
# # MEMBERSHIP
# # ==========================================================
# class GroupMembership(models.Model):
#     ROLE_CHOICES = (
#         ("MEMBER", "Member"),
#         ("ADMIN", "Admin"),
#         ("TREASURER", "Treasurer"),
#         ("SECRETARY", "Secretary"),
#     )

#     group = models.ForeignKey(
#         Group,
#         on_delete=models.CASCADE,
#         related_name="memberships",
#     )
#     user = models.ForeignKey(
#         settings.AUTH_USER_MODEL,
#         on_delete=models.CASCADE,
#         related_name="group_memberships",
#     )

#     role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="MEMBER")
#     is_active = models.BooleanField(default=True)
#     joined_at = models.DateTimeField(default=timezone.now)

#     class Meta:
#         ordering = ["id"]
#         constraints = [
#             models.UniqueConstraint(fields=["group", "user"], name="uniq_group_user_membership"),
#         ]
#         indexes = [
#             models.Index(fields=["group", "is_active"]),
#             models.Index(fields=["user", "is_active"]),
#             models.Index(fields=["group", "role"]),
#         ]

#     def __str__(self):
#         return f"{self.group.name} - {self.user} ({self.role})"


# # ==========================================================
# # JOIN REQUESTS
# # ==========================================================
# class GroupJoinRequest(models.Model):
#     STATUS_CHOICES = (
#         ("PENDING", "Pending"),
#         ("APPROVED", "Approved"),
#         ("REJECTED", "Rejected"),
#         ("CANCELLED", "Cancelled"),
#     )

#     group = models.ForeignKey(
#         Group,
#         on_delete=models.CASCADE,
#         related_name="join_requests",
#     )
#     user = models.ForeignKey(
#         settings.AUTH_USER_MODEL,
#         on_delete=models.CASCADE,
#         related_name="group_join_requests",
#     )

#     note = models.CharField(max_length=255, blank=True, default="")
#     status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING")

#     reviewed_by = models.ForeignKey(
#         settings.AUTH_USER_MODEL,
#         null=True,
#         blank=True,
#         on_delete=models.SET_NULL,
#         related_name="reviewed_group_join_requests",
#     )
#     reviewed_at = models.DateTimeField(null=True, blank=True)

#     created_at = models.DateTimeField(default=timezone.now)

#     class Meta:
#         ordering = ["-id"]
#         constraints = [
#             models.UniqueConstraint(
#                 fields=["group", "user"],
#                 condition=Q(status="PENDING"),
#                 name="uniq_pending_group_join_request_per_user",
#             ),
#         ]
#         indexes = [
#             models.Index(fields=["group", "status", "created_at"]),
#             models.Index(fields=["user", "status", "created_at"]),
#         ]

#     def clean(self):
#         if self.group_id:
#             ok, reason = self.group.can_accept_member()
#             if not ok:
#                 raise ValidationError(reason)

#         if self.group_id and self.user_id:
#             already_member = GroupMembership.objects.filter(
#                 group_id=self.group_id,
#                 user_id=self.user_id,
#                 is_active=True,
#             ).exists()
#             if already_member:
#                 raise ValidationError("You are already an active member of this group.")

#     @transaction.atomic
#     def approve(self, admin_user):
#         if self.status != "PENDING":
#             raise ValidationError("Only pending requests can be approved.")

#         req = (
#             GroupJoinRequest.objects.select_for_update()
#             .select_related("group", "user")
#             .get(pk=self.pk)
#         )

#         ok, reason = req.group.can_accept_member()
#         if not ok:
#             raise ValidationError(reason)

#         membership, created = GroupMembership.objects.get_or_create(
#             group=req.group,
#             user=req.user,
#             defaults={
#                 "role": "MEMBER",
#                 "is_active": True,
#                 "joined_at": timezone.now(),
#             },
#         )

#         if not created and not membership.is_active:
#             membership.is_active = True
#             membership.joined_at = membership.joined_at or timezone.now()
#             membership.save(update_fields=["is_active", "joined_at"])

#         GroupFund.objects.get_or_create(group=req.group)
#         GroupMemberShare.objects.get_or_create(group=req.group, user=req.user)

#         req.status = "APPROVED"
#         req.reviewed_by = admin_user
#         req.reviewed_at = timezone.now()
#         req.save(update_fields=["status", "reviewed_by", "reviewed_at"])

#         return membership

#     def reject(self, admin_user, note: str = ""):
#         if self.status != "PENDING":
#             raise ValidationError("Only pending requests can be rejected.")

#         self.status = "REJECTED"
#         self.reviewed_by = admin_user
#         self.reviewed_at = timezone.now()
#         if note:
#             self.note = note[:255]
#         self.save(update_fields=["status", "reviewed_by", "reviewed_at", "note"])

#     def cancel(self, user):
#         if self.user_id != user.id:
#             raise ValidationError("You can only cancel your own join request.")
#         if self.status != "PENDING":
#             raise ValidationError("Only pending requests can be cancelled.")

#         self.status = "CANCELLED"
#         self.save(update_fields=["status"])

#     def __str__(self):
#         return f"GroupJoinRequest#{self.id} group={self.group_id} user={self.user_id} {self.status}"


# # ==========================================================
# # GROUP FUND
# # ==========================================================
# class GroupFund(models.Model):
#     """
#     Group-owned pooled money.
#     """
#     group = models.OneToOneField(Group, on_delete=models.CASCADE, related_name="fund")
#     balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
#     reserved_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
#     created_at = models.DateTimeField(auto_now_add=True)

#     class Meta:
#         constraints = [
#             models.CheckConstraint(
#                 check=Q(balance__gte=0),
#                 name="groupfund_balance_non_negative",
#             ),
#             models.CheckConstraint(
#                 check=Q(reserved_amount__gte=0),
#                 name="groupfund_reserved_non_negative",
#             ),
#         ]

#     @property
#     def available_balance(self) -> Decimal:
#         available = (self.balance or Decimal("0.00")) - (self.reserved_amount or Decimal("0.00"))
#         return available if available > 0 else Decimal("0.00")

#     def clean(self):
#         if self.reserved_amount > self.balance:
#             raise ValidationError("Reserved amount cannot exceed balance.")

#     def __str__(self):
#         return f"GroupFund group={self.group_id} balance={self.balance}"


# # ==========================================================
# # MEMBER SHARE
# # ==========================================================
# class GroupMemberShare(models.Model):
#     """
#     Tracks each member's accumulated contribution share inside a group.
#     Useful for savings/investment/welfare accountability and future collateral logic.
#     """
#     group = models.ForeignKey(
#         Group,
#         on_delete=models.CASCADE,
#         related_name="member_shares",
#     )
#     user = models.ForeignKey(
#         settings.AUTH_USER_MODEL,
#         on_delete=models.CASCADE,
#         related_name="group_shares",
#     )

#     total_contributed = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
#     reserved_share = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
#     updated_at = models.DateTimeField(auto_now=True)

#     class Meta:
#         constraints = [
#             models.UniqueConstraint(fields=["group", "user"], name="uniq_group_member_share"),
#             models.CheckConstraint(
#                 check=Q(total_contributed__gte=0),
#                 name="groupshare_total_non_negative",
#             ),
#             models.CheckConstraint(
#                 check=Q(reserved_share__gte=0),
#                 name="groupshare_reserved_non_negative",
#             ),
#         ]
#         indexes = [
#             models.Index(fields=["group", "user"]),
#         ]

#     @property
#     def available_share(self) -> Decimal:
#         available = (self.total_contributed or Decimal("0.00")) - (self.reserved_share or Decimal("0.00"))
#         return available if available > 0 else Decimal("0.00")

#     def clean(self):
#         if self.reserved_share > self.total_contributed:
#             raise ValidationError("Reserved share cannot exceed total contributed.")

#     def __str__(self):
#         return f"Share g={self.group_id} u={self.user_id} total={self.total_contributed}"


# # ==========================================================
# # CONTRIBUTIONS
# # ==========================================================
# class GroupContribution(models.Model):
#     """
#     Pure contribution history record.

#     Important:
#     - This model does NOT update GroupFund or GroupMemberShare directly.
#     - All accounting mutations should happen in groups/services.py
#       inside post_group_contribution().
#     """
#     SOURCE_CHOICES = (
#         ("MANUAL", "Manual"),
#         ("MPESA", "M-Pesa"),
#         ("BANK", "Bank"),
#         ("OTHER", "Other"),
#     )

#     group = models.ForeignKey(
#         Group,
#         on_delete=models.CASCADE,
#         related_name="contributions",
#     )
#     user = models.ForeignKey(
#         settings.AUTH_USER_MODEL,
#         on_delete=models.CASCADE,
#         related_name="group_contributions",
#     )

#     amount = models.DecimalField(max_digits=14, decimal_places=2)
#     source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="MANUAL")
#     reference = models.CharField(max_length=120, null=True, blank=True)
#     note = models.CharField(max_length=255, null=True, blank=True)

#     created_at = models.DateTimeField(default=timezone.now)

#     class Meta:
#         ordering = ["-created_at"]
#         indexes = [
#             models.Index(fields=["group", "user", "created_at"]),
#             models.Index(fields=["reference"]),
#             models.Index(fields=["source", "created_at"]),
#         ]

#     def clean(self):
#         if self.amount is None or self.amount <= 0:
#             raise ValidationError("Contribution amount must be greater than 0.")

#         if self.group_id and self.user_id:
#             is_member = GroupMembership.objects.filter(
#                 group_id=self.group_id,
#                 user_id=self.user_id,
#                 is_active=True,
#             ).exists()
#             if not is_member:
#                 raise ValidationError("Only active group members can contribute.")

#     def save(self, *args, **kwargs):
#         self.full_clean()
#         super().save(*args, **kwargs)

#     def __str__(self):
#         return f"Contribution g={self.group_id} u={self.user_id} amount={self.amount}"


# # ==========================================================
# # SHARE HOLD
# # ==========================================================
# class GroupShareHold(models.Model):
#     """
#     Lock a member's share for a loan or obligation inside the group.
#     loan_id is kept as IntegerField for flexibility across apps.
#     """
#     group = models.ForeignKey(
#         Group,
#         on_delete=models.CASCADE,
#         related_name="share_holds",
#     )
#     user = models.ForeignKey(
#         settings.AUTH_USER_MODEL,
#         on_delete=models.CASCADE,
#         related_name="group_share_holds",
#     )

#     loan_id = models.IntegerField(db_index=True)
#     amount = models.DecimalField(max_digits=14, decimal_places=2)

#     is_active = models.BooleanField(default=True)
#     created_at = models.DateTimeField(default=timezone.now)
#     released_at = models.DateTimeField(null=True, blank=True)

#     class Meta:
#         indexes = [
#             models.Index(fields=["group", "user", "loan_id", "is_active"]),
#         ]

#     def clean(self):
#         if self.amount is None or self.amount <= 0:
#             raise ValidationError("Hold amount must be greater than 0.")

#     def release(self):
#         if not self.is_active:
#             return
#         self.is_active = False
#         self.released_at = timezone.now()
#         self.save(update_fields=["is_active", "released_at"])

#     def __str__(self):
#         return (
#             f"GroupShareHold loan={self.loan_id} "
#             f"group={self.group_id} user={self.user_id} amount={self.amount}"
#         )

