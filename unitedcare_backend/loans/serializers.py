from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import (
    Loan,
    LoanGuarantor,
    LoanInstallment,
    LoanPayment,
    LoanProduct,
    LoanSecurityAllocation,
    MemberCreditProfile,
)
from .services import get_default_loan_product


User = get_user_model()


# ==========================================================
# Small user serializer
# ==========================================================
class SimpleUserSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "full_name"]

    def get_full_name(self, obj):
        full = ""
        if hasattr(obj, "get_full_name"):
            full = (obj.get_full_name() or "").strip()
        if full:
            return full

        first = getattr(obj, "first_name", "") or ""
        last = getattr(obj, "last_name", "") or ""
        joined = f"{first} {last}".strip()
        if joined:
            return joined

        username = getattr(obj, "username", "") or ""
        if username:
            return username

        return str(obj)


# ==========================================================
# Credit Profile
# ==========================================================
class MemberCreditProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = MemberCreditProfile
        fields = [
            "id",
            "user",
            "score",
            "total_loans",
            "loans_completed",
            "loans_defaulted",
            "late_payments",
            "updated_at",
        ]
        read_only_fields = fields


# ==========================================================
# Loan Product
# ==========================================================
class LoanProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoanProduct
        fields = [
            "id",
            "name",
            "interest_type",
            "annual_interest_rate",
            "repayment_frequency",
            "repayment_weekday",
            "max_weeks",
            "late_fee_rate_weekly",
            "is_active",
            "is_default",
        ]
        read_only_fields = fields


# ==========================================================
# Loan Guarantor
# ==========================================================
class LoanGuarantorSerializer(serializers.ModelSerializer):
    guarantor_detail = SimpleUserSerializer(source="guarantor", read_only=True)

    class Meta:
        model = LoanGuarantor
        fields = [
            "id",
            "loan",
            "guarantor",
            "guarantor_detail",
            "accepted",
            "accepted_at",
            "reserved_amount",
            "request_note",
            "admin_note",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "accepted",
            "accepted_at",
            "reserved_amount",
            "admin_note",
            "created_at",
        ]


class AddLoanGuarantorSerializer(serializers.Serializer):
    loan = serializers.PrimaryKeyRelatedField(queryset=Loan.objects.all())
    guarantor = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    request_note = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        loan = attrs["loan"]
        guarantor = attrs["guarantor"]
        request = self.context.get("request")

        if request and loan.borrower_id != request.user.id:
            raise serializers.ValidationError("Only the borrower can add guarantors to this loan.")

        if loan.status not in ("PENDING", "UNDER_REVIEW"):
            raise serializers.ValidationError("You can only add guarantors to a pending/review loan.")

        if loan.borrower_id == guarantor.id:
            raise serializers.ValidationError("Borrower cannot guarantee their own loan.")

        if LoanGuarantor.objects.filter(loan=loan, guarantor=guarantor).exists():
            raise serializers.ValidationError("This guarantor has already been added to the loan.")

        return attrs

    def create(self, validated_data):
        return LoanGuarantor.objects.create(
            loan=validated_data["loan"],
            guarantor=validated_data["guarantor"],
            request_note=validated_data.get("request_note", ""),
        )


# ==========================================================
# Security Allocation
# ==========================================================
class LoanSecurityAllocationSerializer(serializers.ModelSerializer):
    owner_detail = SimpleUserSerializer(source="owner_user", read_only=True)
    guarantor_link_id = serializers.IntegerField(source="guarantor_link.id", read_only=True)

    class Meta:
        model = LoanSecurityAllocation
        fields = [
            "id",
            "loan",
            "source_type",
            "owner_user",
            "owner_detail",
            "guarantor_link_id",
            "savings_account",
            "merry",
            "group",
            "amount",
            "is_active",
            "created_at",
            "released_at",
        ]
        read_only_fields = fields


# ==========================================================
# Installment
# ==========================================================
class LoanInstallmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoanInstallment
        fields = [
            "id",
            "loan",
            "installment_no",
            "due_date",
            "principal_due",
            "interest_due",
            "total_due",
            "late_fee",
            "paid_amount",
            "is_paid",
        ]
        read_only_fields = fields


# ==========================================================
# Payment
# ==========================================================
class LoanPaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoanPayment
        fields = [
            "id",
            "loan",
            "amount",
            "paid_at",
            "method",
            "reference",
        ]
        read_only_fields = ["id", "paid_at"]


class LoanPaymentCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    method = serializers.CharField(required=False, default="MANUAL")
    reference = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate_amount(self, value):
        if Decimal(value) <= 0:
            raise serializers.ValidationError("Payment amount must be greater than 0.")
        return value


# ==========================================================
# Loan Request
# ==========================================================
class LoanRequestSerializer(serializers.Serializer):
    """
    Member-facing request serializer.

    Member supplies:
    - principal
    - term_weeks
    - guarantor_ids
    - optional member note

    Member does NOT supply:
    - product
    - merry
    - group
    - security inputs
    """

    principal = serializers.DecimalField(max_digits=12, decimal_places=2)
    term_weeks = serializers.IntegerField(min_value=1)
    guarantor_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=True,
        default=list,
    )
    member_note = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_principal(self, value):
        if Decimal(value) <= 0:
            raise serializers.ValidationError("Principal must be greater than 0.")
        return value


# ==========================================================
# Admin / Internal Loan Create
# ==========================================================
class LoanCreateAdminSerializer(serializers.ModelSerializer):
    """
    Optional admin/internal serializer if admin wants direct control.
    """

    class Meta:
        model = Loan
        fields = [
            "id",
            "borrower",
            "product",
            "principal",
            "term_weeks",
            "status",
            "member_note",
            "admin_note",
        ]


# ==========================================================
# Loan list/detail
# ==========================================================
class LoanListSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)

    class Meta:
        model = Loan
        fields = [
            "id",
            "borrower",
            "product",
            "product_name",
            "principal",
            "term_weeks",
            "status",
            "is_defaulter",
            "approved_at",
            "rejected_at",
            "completed_at",
            "created_at",
            "total_payable",
            "total_paid",
            "outstanding_balance",
            "security_target",
            "security_reserved_total",
        ]
        read_only_fields = fields


class LoanDetailSerializer(serializers.ModelSerializer):
    borrower_detail = SimpleUserSerializer(source="borrower", read_only=True)
    product_detail = LoanProductSerializer(source="product", read_only=True)
    guarantors = LoanGuarantorSerializer(many=True, read_only=True)
    security_allocations = LoanSecurityAllocationSerializer(many=True, read_only=True)
    installments = LoanInstallmentSerializer(many=True, read_only=True)
    payments = LoanPaymentSerializer(many=True, read_only=True)

    class Meta:
        model = Loan
        fields = [
            "id",
            "borrower",
            "borrower_detail",
            "product",
            "product_detail",
            "principal",
            "term_weeks",
            "status",
            "is_defaulter",
            "approved_at",
            "rejected_at",
            "completed_at",
            "created_at",
            "total_payable",
            "total_paid",
            "outstanding_balance",
            "security_target",
            "security_reserved_total",
            "member_note",
            "admin_note",
            "guarantors",
            "security_allocations",
            "installments",
            "payments",
        ]
        read_only_fields = fields


# ==========================================================
# Eligibility preview
# ==========================================================
class LoanEligibilityPreviewSerializer(serializers.Serializer):
    eligible = serializers.BooleanField(read_only=True)
    max_allowed = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    available_savings = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    has_active_loan = serializers.BooleanField(read_only=True)
    missing_deposit_months = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
    )
    reason = serializers.CharField(read_only=True)


# ==========================================================
# Guarantor candidate list
# ==========================================================
class GuarantorCandidateSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "full_name"]

    def get_full_name(self, obj):
        full = ""
        if hasattr(obj, "get_full_name"):
            full = (obj.get_full_name() or "").strip()
        if full:
            return full

        first = getattr(obj, "first_name", "") or ""
        last = getattr(obj, "last_name", "") or ""
        joined = f"{first} {last}".strip()
        if joined:
            return joined

        username = getattr(obj, "username", "") or ""
        if username:
            return username

        return str(obj)
class LoanRejectSerializer(serializers.Serializer):
    rejection_reason = serializers.CharField()