from rest_framework import serializers
from .models import SavingsAccount, SavingsTransaction


class SavingsAccountSerializer(serializers.ModelSerializer):
    available_balance = serializers.CharField(read_only=True)

    class Meta:
        model = SavingsAccount
        fields = (
            "id",
            "user",
            "name",
            "account_type",
            "balance",
            "reserved_amount",
            "available_balance",
            "locked_until",
            "target_amount",
            "target_deadline",
            "is_active",
            "created_at",
        )
        read_only_fields = ("id", "user", "balance", "reserved_amount", "available_balance", "created_at")


class CreateSavingsAccountSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    account_type = serializers.ChoiceField(choices=["FLEXIBLE", "FIXED", "TARGET"])
    locked_until = serializers.DateField(required=False, allow_null=True)
    target_amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, allow_null=True)
    target_deadline = serializers.DateField(required=False, allow_null=True)


class SavingsTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = SavingsTransaction
        fields = ("id", "account", "txn_type", "amount", "reference", "note", "created_at")
        read_only_fields = ("id", "created_at")


class ManualDepositSerializer(serializers.Serializer):
    account_id = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    reference = serializers.CharField(required=False, allow_blank=True)
    note = serializers.CharField(required=False, allow_blank=True)