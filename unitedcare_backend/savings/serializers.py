from rest_framework import serializers
from .models import SavingsAccount, SavingsTransaction, WithdrawRequest


class SavingsAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = SavingsAccount
        fields = "__all__"
        read_only_fields = ("user", "balance", "created_at")


class SavingsTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = SavingsTransaction
        fields = "__all__"
        read_only_fields = ("created_at",)


class DepositSerializer(serializers.Serializer):
    account_id = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    reference = serializers.CharField(required=False, allow_blank=True)
    note = serializers.CharField(required=False, allow_blank=True)


class WithdrawRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = WithdrawRequest
        fields = "__all__"
        read_only_fields = ("requested_by", "status", "reviewed_by", "reviewed_at", "created_at")