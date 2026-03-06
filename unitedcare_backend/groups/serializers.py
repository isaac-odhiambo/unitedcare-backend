# groups/serializers.py (NEW - COMPLETE)
from rest_framework import serializers
from .models import GroupFund, GroupMemberShare, GroupContribution


class GroupFundSerializer(serializers.ModelSerializer):
    available_balance = serializers.CharField(read_only=True)

    class Meta:
        model = GroupFund
        fields = ("group", "balance", "reserved_amount", "available_balance", "created_at")


class GroupMemberShareSerializer(serializers.ModelSerializer):
    available_share = serializers.CharField(read_only=True)

    class Meta:
        model = GroupMemberShare
        fields = ("group", "user", "total_contributed", "reserved_share", "available_share", "updated_at")


class GroupContributionSerializer(serializers.ModelSerializer):
    class Meta:
        model = GroupContribution
        fields = ("id", "group", "user", "amount", "reference", "note", "created_at")
        read_only_fields = ("id", "created_at")


class PostContributionSerializer(serializers.Serializer):
    group_id = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    reference = serializers.CharField(required=False, allow_blank=True)
    note = serializers.CharField(required=False, allow_blank=True)