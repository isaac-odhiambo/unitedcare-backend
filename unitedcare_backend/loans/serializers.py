from rest_framework import serializers
from django.contrib.auth import get_user_model

from .models import Loan, LoanProduct, LoanGuarantor, LoanInstallment, LoanPayment

User = get_user_model()


class LoanProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoanProduct
        fields = "__all__"


# ==========================
# Create / Request Loan
# ==========================
class LoanCreateSerializer(serializers.ModelSerializer):
    """
    Used by RequestLoanView.
    Borrower is set in view.
    Must provide exactly one of: merry or group.
    """

    class Meta:
        model = Loan
        fields = (
            "id",
            "merry",
            "group",
            "product",
            "principal",
            "term_weeks",
        )

    def validate(self, attrs):
        merry = attrs.get("merry")
        group = attrs.get("group")

        if bool(merry) == bool(group):
            raise serializers.ValidationError("Provide either merry or group (not both).")

        product = attrs.get("product")
        term_weeks = attrs.get("term_weeks")

        if term_weeks is None or term_weeks <= 0:
            raise serializers.ValidationError({"term_weeks": "term_weeks must be greater than 0."})

        if product and term_weeks > product.max_weeks:
            raise serializers.ValidationError({"term_weeks": "Term exceeds product max weeks."})

        return attrs


# ==========================
# Loan Output Serializer
# ==========================
class LoanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Loan
        fields = "__all__"
        read_only_fields = (
            "borrower",
            "status",
            "approved_at",
            "created_at",
            "total_payable",
            "total_paid",
            "outstanding_balance",
            "is_defaulter",
            # ✅ new security fields should be system-controlled
            "borrower_reserved_savings",
            "borrower_reserved_merry_credit",
            "security_target",
        )


# ==========================
# Add Guarantor
# ==========================
class LoanGuarantorSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoanGuarantor
        fields = "__all__"
        read_only_fields = (
            "accepted",
            "accepted_at",
            # ✅ reserved_amount is system-controlled (set during approval)
            "reserved_amount",
        )


# ==========================
# Accept/Reject Guarantor (if you use it)
# ==========================
class GuarantorAcceptSerializer(serializers.Serializer):
    accepted = serializers.BooleanField()


# ==========================
# Installments
# ==========================
class LoanInstallmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoanInstallment
        fields = "__all__"


# ==========================
# Loan Payments
# ==========================
class LoanPaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoanPayment
        fields = "__all__"
        read_only_fields = ("paid_at", "loan")