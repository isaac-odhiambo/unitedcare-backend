# payments/serializers.py
from decimal import Decimal
from rest_framework import serializers

from .models import WithdrawalRequest, PaymentLedger, MpesaTransaction
from .balances import get_user_balance
from .utils import calculate_b2c_fee


def _source_to_category(source: str) -> str:
    s = (source or "").upper()
    if s in ("SAVINGS", "MERRY", "GROUP"):
        return s
    return "SAVINGS"


class WithdrawalCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = WithdrawalRequest
        fields = ["phone", "amount", "source"]

    def validate_amount(self, value: Decimal):
        if value is None or value <= Decimal("0"):
            raise serializers.ValidationError("Amount must be greater than 0.")
        return value

    def validate_source(self, value: str):
        v = (value or "SAVINGS").upper()
        if v not in ("SAVINGS", "MERRY", "GROUP", "OTHER"):
            raise serializers.ValidationError("Invalid source. Use SAVINGS, MERRY, GROUP or OTHER.")
        return v

    def validate(self, attrs):
        """
        Early guard (UX/security):
        - block obvious insufficient balance requests
        NOTE: services.py will enforce again at payout time.
        """
        request = self.context.get("request")
        if not request or not request.user or not request.user.is_authenticated:
            return attrs

        amount = Decimal(str(attrs.get("amount") or "0"))
        source = (attrs.get("source") or "SAVINGS").upper()

        # Only enforce balance for known sources
        if source in ("SAVINGS", "MERRY", "GROUP"):
            category = _source_to_category(source)
            available = get_user_balance(user=request.user, category=category)

            # Your current payout logic: payout = amount - fee (fee is inside amount)
            # Therefore the required balance is at least "amount"
            # If you ever change to "fee on top", change this to amount + fee.
            if amount > available:
                raise serializers.ValidationError(
                    {"amount": f"Insufficient {category} balance. Available: {available}."}
                )

            # Optional: prevent too-small withdrawals after fee
            fee = calculate_b2c_fee(amount)
            payout_amount = amount - fee
            if payout_amount <= Decimal("0"):
                raise serializers.ValidationError(
                    {"amount": "Amount is too small after withdrawal fee. Increase amount."}
                )

        return attrs

    def create(self, validated_data):
        request = self.context["request"]
        # Serializer only creates request. Admin approval + payout handled by services/views.
        return WithdrawalRequest.objects.create(user=request.user, **validated_data)


class WithdrawalSerializer(serializers.ModelSerializer):
    class Meta:
        model = WithdrawalRequest
        fields = "__all__"
        read_only_fields = "__all__"


class WithdrawalApproveSerializer(serializers.Serializer):
    # keep for future fields (remarks, admin_pin etc.)
    pass


class WithdrawalRejectSerializer(serializers.Serializer):
    rejection_reason = serializers.CharField(required=False, allow_blank=True)


class PaymentLedgerSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentLedger
        fields = "__all__"
        read_only_fields = "__all__"


class MpesaTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = MpesaTransaction
        fields = "__all__"
        read_only_fields = "__all__"