# merry/models.py
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Max
from django.utils import timezone


class MerryGoRound(models.Model):
    ORDER_TYPES = (
        ("manual", "Manual"),
        ("random", "Random"),
    )

    name = models.CharField(max_length=255)
    contribution_amount = models.DecimalField(max_digits=12, decimal_places=2)
    cycle_duration_weeks = models.PositiveIntegerField(default=1)
    payout_order_type = models.CharField(max_length=10, choices=ORDER_TYPES, default="manual")

    # ✅ required by payments WithdrawalRequest.can_withdraw_merry
    next_payout_date = models.DateField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="merries_created",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-id"]

    def total_pool(self) -> Decimal:
        return Decimal(self.members.filter(is_active=True).count()) * (self.contribution_amount or Decimal("0"))

    def next_payout_position(self) -> int:
        """
        Returns the next payout_position to assign (1..N), based on current members.
        Used when approving join requests.
        """
        mx = self.members.filter(is_active=True).aggregate(m=Max("payout_position")).get("m") or 0
        return int(mx) + 1

    def __str__(self) -> str:
        return self.name


class MerryMember(models.Model):
    merry = models.ForeignKey(
        MerryGoRound,
        related_name="members",
        on_delete=models.CASCADE,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="merry_memberships",
    )

    # payout order position (auto-set on approval)
    payout_position = models.PositiveIntegerField(null=True, blank=True)

    joined_at = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            # ✅ same user cannot join same merry twice
            models.UniqueConstraint(fields=["merry", "user"], name="uniq_user_per_merry"),
            # Optional: payout_position unique per merry (prevents duplicates)
            models.UniqueConstraint(fields=["merry", "payout_position"], name="uniq_payout_position_per_merry"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} - {self.merry.name}"


class MerryJoinRequest(models.Model):
    """
    Member join request that requires admin approval.

    Flow:
      - Member: create request -> PENDING
      - Member: cancel request -> CANCELLED (only if PENDING)
      - Admin: approve -> creates MerryMember + auto assigns payout_position
      - Admin: reject -> REJECTED
    """

    STATUS = (
        ("PENDING", "Pending"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
        ("CANCELLED", "Cancelled"),
    )

    merry = models.ForeignKey(MerryGoRound, on_delete=models.CASCADE, related_name="join_requests")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="merry_join_requests")

    status = models.CharField(max_length=20, choices=STATUS, default="PENDING")
    note = models.CharField(max_length=255, blank=True, default="")

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_merry_join_requests",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-id"]
        constraints = [
            # prevent duplicates for the same user + merry
            models.UniqueConstraint(fields=["merry", "user"], name="uniq_join_request_per_merry_user"),
        ]
        indexes = [
            models.Index(fields=["status", "created_at"]),
        ]

    def clean(self):
        # If already a member, request is not allowed
        if self.merry_id and self.user_id:
            if MerryMember.objects.filter(merry_id=self.merry_id, user_id=self.user_id, is_active=True).exists():
                raise ValidationError("You are already a member of this merry.")

    @transaction.atomic
    def approve(self, admin_user) -> MerryMember:
        """
        Approve request:
          - creates membership if missing
          - auto sets payout_position (manual only)
          - marks request APPROVED
        """
        if self.status != "PENDING":
            raise ValidationError("Only PENDING requests can be approved.")

        # Lock this request row
        jr = MerryJoinRequest.objects.select_for_update().get(id=self.id)

        # Safety check again
        if MerryMember.objects.filter(merry=jr.merry, user=jr.user, is_active=True).exists():
            jr.status = "APPROVED"
            jr.reviewed_by = admin_user
            jr.reviewed_at = timezone.now()
            jr.save(update_fields=["status", "reviewed_by", "reviewed_at"])
            return MerryMember.objects.get(merry=jr.merry, user=jr.user)

        payout_position: Optional[int] = None
        if jr.merry.payout_order_type == "manual":
            payout_position = jr.merry.next_payout_position()
        # If "random": leave null for now; you can randomize later in an admin tool/service.

        member = MerryMember.objects.create(
            merry=jr.merry,
            user=jr.user,
            payout_position=payout_position,
            is_active=True,
            joined_at=timezone.now(),
        )

        jr.status = "APPROVED"
        jr.reviewed_by = admin_user
        jr.reviewed_at = timezone.now()
        jr.save(update_fields=["status", "reviewed_by", "reviewed_at"])

        return member

    def reject(self, admin_user, note: str = "") -> None:
        if self.status != "PENDING":
            raise ValidationError("Only PENDING requests can be rejected.")

        self.status = "REJECTED"
        self.reviewed_by = admin_user
        self.reviewed_at = timezone.now()
        if note:
            self.note = note[:255]
        self.save(update_fields=["status", "reviewed_by", "reviewed_at", "note"])

    def cancel(self, user) -> None:
        """
        Member cancels their own request if still pending.
        """
        if self.user_id != user.id:
            raise ValidationError("You can only cancel your own join request.")
        if self.status != "PENDING":
            raise ValidationError("Only PENDING requests can be cancelled.")

        self.status = "CANCELLED"
        self.save(update_fields=["status"])

    def __str__(self) -> str:
        return f"JoinRequest#{self.id} merry={self.merry_id} user={self.user_id} {self.status}"


class MerryContribution(models.Model):
    """
    Records a member's scheduled contribution.
    Payment is done through Payments app STK push:
      - MpesaTransaction.target_object points here (GenericForeignKey)
      - PaymentLedger posts:
          CREDIT MERRY (base amount)
          DEBIT  TRANSACTION_FEE (fee, e.g. 50)
    """
    STATUS_CHOICES = (
        ("PENDING", "Pending"),
        ("PAID", "Paid"),
        ("FAILED", "Failed"),
        ("CANCELLED", "Cancelled"),
    )

    member = models.ForeignKey(
        MerryMember,
        on_delete=models.CASCADE,
        related_name="contributions",
    )

    week_number = models.PositiveIntegerField()
    amount = models.DecimalField(max_digits=12, decimal_places=2)  # base amount (e.g. 1000)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING")
    paid_at = models.DateTimeField(null=True, blank=True)

    # Optional references for quick lookups (MpesaTx already links via GenericForeignKey)
    mpesa_receipt_number = models.CharField(max_length=64, null=True, blank=True, db_index=True)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-id"]
        constraints = [
            # ✅ prevent duplicate contribution records for same member + week
            models.UniqueConstraint(fields=["member", "week_number"], name="uniq_merry_contribution_per_week"),
        ]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["week_number"]),
        ]

    def __str__(self) -> str:
        return f"MerryContribution#{self.id} member={self.member_id} week={self.week_number} {self.status}"


class MerryPayout(models.Model):
    """
    Records payout to a member (usually via B2C withdrawal request or direct payout).
    Typically you will:
      - create WithdrawalRequest(source="MERRY", target_object=merry)
      - member requests them; admin approves/pays using payments app
      - on success, ledger debits MERRY + fee bucket
    """
    STATUS_CHOICES = (
        ("SCHEDULED", "Scheduled"),
        ("PROCESSING", "Processing"),
        ("PAID", "Paid"),
        ("FAILED", "Failed"),
        ("CANCELLED", "Cancelled"),
    )

    merry = models.ForeignKey(MerryGoRound, on_delete=models.CASCADE, related_name="payouts")
    member = models.ForeignKey(MerryMember, on_delete=models.CASCADE, related_name="payouts")

    week_number = models.PositiveIntegerField()
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="SCHEDULED")
    paid_at = models.DateTimeField(null=True, blank=True)

    # optional audit fields
    notes = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-id"]
        constraints = [
            # ✅ only one payout per merry per week
            models.UniqueConstraint(fields=["merry", "week_number"], name="uniq_payout_per_merry_week"),
            # ✅ member should not be paid twice in same week in same merry
            models.UniqueConstraint(fields=["merry", "member", "week_number"], name="uniq_member_payout_per_week"),
        ]

    def __str__(self) -> str:
        return f"MerryPayout#{self.id} merry={self.merry_id} member={self.member_id} week={self.week_number} {self.status}"