from django.db import models
from django.contrib.auth import get_user_model
from decimal import Decimal
import uuid

User = get_user_model()


class MerryGoRound(models.Model):
    ORDER_TYPES = (
        ("manual", "Manual"),
        ("random", "Random"),
    )

    name = models.CharField(max_length=255)
    contribution_amount = models.DecimalField(max_digits=10, decimal_places=2)
    cycle_duration_weeks = models.IntegerField()
    payout_order_type = models.CharField(max_length=10, choices=ORDER_TYPES)

    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    def total_pool(self):
        return self.members.count() * self.contribution_amount

    def current_balance(self):
        total = Contribution.objects.filter(
            member__merry=self,
            paid=True
        ).aggregate(models.Sum("amount"))["amount__sum"]

        return total or Decimal("0.00")

    def __str__(self):
        return self.name


class Member(models.Model):
    merry = models.ForeignKey(
        MerryGoRound,
        related_name="members",
        on_delete=models.CASCADE
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    payout_position = models.IntegerField(null=True, blank=True)

    def __str__(self):
        return f"{self.user} - {self.merry.name}"


class Contribution(models.Model):
    member = models.ForeignKey(Member, on_delete=models.CASCADE)
    week_number = models.IntegerField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    paid = models.BooleanField(default=False)
    paid_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.member.user} - Week {self.week_number}"


class Payout(models.Model):
    merry = models.ForeignKey(MerryGoRound, on_delete=models.CASCADE)
    member = models.ForeignKey(Member, on_delete=models.CASCADE)
    week_number = models.IntegerField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    confirmed = models.BooleanField(default=False)
    confirmed_at = models.DateTimeField(null=True, blank=True)


class Receipt(models.Model):
    receipt_number = models.UUIDField(default=uuid.uuid4, editable=False)
    contribution = models.ForeignKey(Contribution, on_delete=models.CASCADE)
    generated_at = models.DateTimeField(auto_now_add=True)