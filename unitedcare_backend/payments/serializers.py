from decimal import Decimal
from rest_framework import serializers
from django.contrib.contenttypes.models import ContentType

from .models import MpesaTransaction, PaymentLedger, WithdrawalRequest


# =========================
# Small helpers
# =========================
def _to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


# =========================
# Mpesa Transaction Serializer
# =========================
class MpesaTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = MpesaTransaction
        fields = [
            "id",
            "user",
            "phone",
            "amount",
            "direction",
            "channel",
            "purpose",
            "status",
            "merchant_request_id",
            "checkout_request_id",
            "conversation_id",
            "originator_conversation_id",
            "result_code",
            "result_desc",
            "mpesa_receipt_number",
            "transaction_date",
            "request_payload",
            "callback_payload",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


# =========================
# Ledger / History Serializer
# =========================
class PaymentLedgerSerializer(serializers.ModelSerializer):
    mpesa = serializers.SerializerMethodField()

    class Meta:
        model = PaymentLedger
        fields = [
            "id",
            "entry_type",
            "category",
            "amount",
            "narration",
            "reference",
            "created_at",
            "mpesa",
        ]

    def get_mpesa(self, obj):
        if not obj.mpesa_tx:
            return None
        return {
            "id": obj.mpesa_tx.id,
            "status": obj.mpesa_tx.status,
            "receipt": obj.mpesa_tx.mpesa_receipt_number,
            "channel": obj.mpesa_tx.channel,
            "direction": obj.mpesa_tx.direction,
            "phone": obj.mpesa_tx.phone,
        }


# =========================
# Withdrawal - Create (Member)
# =========================
class WithdrawalCreateSerializer(serializers.ModelSerializer):
    """
    Member creates:
    - amount
    - phone (optional, defaults to user's phone if you want in views)
    - source: SAVINGS / MERRY / OTHER
    - (optional) link to a specific object via content_type + object_id
    """

    # Optional generic target linking
    target_app_label = serializers.CharField(required=False, allow_blank=True)
    target_model = serializers.CharField(required=False, allow_blank=True)
    target_object_id = serializers.IntegerField(required=False)

    class Meta:
        model = WithdrawalRequest
        fields = [
            "id",
            "phone",
            "amount",
            "source",
            "target_app_label",
            "target_model",
            "target_object_id",
            "status",
            "created_at",
        ]
        read_only_fields = ["id", "status", "created_at"]

    def validate_amount(self, value):
        amt = _to_decimal(value)
        if amt <= 0:
            raise serializers.ValidationError("Amount must be greater than zero.")
        return value

    def validate(self, attrs):
        # If they provide target info, they must provide all
        has_any = any(
            [
                attrs.get("target_app_label"),
                attrs.get("target_model"),
                attrs.get("target_object_id") is not None,
            ]
        )
        has_all = (
            bool(attrs.get("target_app_label"))
            and bool(attrs.get("target_model"))
            and attrs.get("target_object_id") is not None
        )
        if has_any and not has_all:
            raise serializers.ValidationError(
                "Provide target_app_label + target_model + target_object_id together."
            )
        return attrs

    def create(self, validated_data):
        # Extract target info if provided
        app_label = validated_data.pop("target_app_label", None)
        model = validated_data.pop("target_model", None)
        obj_id = validated_data.pop("target_object_id", None)

        user = self.context["request"].user
        withdrawal = WithdrawalRequest.objects.create(
            user=user,
            status="PENDING",
            **validated_data,
        )

        if app_label and model and obj_id is not None:
            try:
                ct = ContentType.objects.get(app_label=app_label, model=model)
                withdrawal.target_content_type = ct
                withdrawal.target_object_id = int(obj_id)
                withdrawal.save(update_fields=["target_content_type", "target_object_id"])
            except ContentType.DoesNotExist:
                # Keep request valid even if target is wrong; you can also raise error if you prefer
                raise serializers.ValidationError(
                    {"target_model": "Invalid target model/app_label."}
                )

        return withdrawal


# =========================
# Withdrawal - List/Detail
# =========================
class WithdrawalSerializer(serializers.ModelSerializer):
    mpesa = serializers.SerializerMethodField()
    approved_by_username = serializers.SerializerMethodField()
    rejected_by_username = serializers.SerializerMethodField()

    class Meta:
        model = WithdrawalRequest
        fields = [
            "id",
            "user",
            "phone",
            "amount",
            "source",
            "status",
            "rejection_reason",
            "approved_by",
            "approved_by_username",
            "approved_at",
            "rejected_by",
            "rejected_by_username",
            "rejected_at",
            "created_at",
            "updated_at",
            "mpesa",
        ]
        read_only_fields = fields

    def get_approved_by_username(self, obj):
        return getattr(obj.approved_by, "username", None) if obj.approved_by else None

    def get_rejected_by_username(self, obj):
        return getattr(obj.rejected_by, "username", None) if obj.rejected_by else None

    def get_mpesa(self, obj):
        if not obj.mpesa_tx:
            return None
        return {
            "id": obj.mpesa_tx.id,
            "status": obj.mpesa_tx.status,
            "channel": obj.mpesa_tx.channel,
            "receipt": obj.mpesa_tx.mpesa_receipt_number,
            "result_desc": obj.mpesa_tx.result_desc,
        }


# =========================
# Admin - Approve / Reject
# =========================
class WithdrawalApproveSerializer(serializers.Serializer):
    """
    Admin approves -> set APPROVED.
    After approving, your view/service can start B2C payout and move to PROCESSING.
    """
    approve = serializers.BooleanField(default=True)

    def validate(self, attrs):
        if attrs.get("approve") is not True:
            raise serializers.ValidationError("approve must be true for approval.")
        return attrs


class WithdrawalRejectSerializer(serializers.Serializer):
    rejection_reason = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate(self, attrs):
        # optional reason, but good to have
        return attrs