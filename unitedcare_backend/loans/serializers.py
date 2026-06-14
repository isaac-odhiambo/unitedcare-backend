from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import serializers

from .models import (
    Loan,
    LoanGuarantor,
    LoanInstallment,
    LoanPayment,
    LoanProduct,
    LoanReminderLog,
    LoanSecurityAllocation,
    MemberCreditProfile,
)

User = get_user_model()


# ==========================================================
# Helpers
# ==========================================================
def money(value) -> Decimal:
    if value is None:
        return Decimal("0.00")
    return Decimal(value)


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
    user_detail = SimpleUserSerializer(source="user", read_only=True)

    class Meta:
        model = MemberCreditProfile
        fields = [
            "id",
            "user",
            "user_detail",
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
            "grace_period_days",
            "late_fee_rate_weekly",
            "default_interest_rate_weekly",
            "is_active",
            "is_default",
            "created_at",
            "updated_at",
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
            "rejected_at",
            "reserved_amount",
            "request_note",
            "admin_note",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "accepted",
            "accepted_at",
            "rejected_at",
            "reserved_amount",
            "admin_note",
            "created_at",
            "updated_at",
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
            raise serializers.ValidationError("You can only add guarantors to a pending or under-review loan.")

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
    installment_balance = serializers.SerializerMethodField()
    full_balance = serializers.SerializerMethodField()
    days_remaining = serializers.SerializerMethodField()
    days_overdue = serializers.SerializerMethodField()
    is_overdue = serializers.SerializerMethodField()

    class Meta:
        model = LoanInstallment
        fields = [
            "id",
            "loan",
            "installment_no",
            "due_date",
            "grace_ends_on",
            "default_interest_start_date",
            "principal_due",
            "interest_due",
            "total_due",
            "late_fee",
            "late_fee_weeks_applied",
            "default_interest",
            "default_interest_weeks_applied",
            "last_default_interest_applied_at",
            "paid_amount",
            "installment_balance",
            "full_balance",
            "status",
            "is_paid",
            "is_overdue",
            "days_remaining",
            "days_overdue",
            "paid_at",
            "defaulted_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_installment_balance(self, obj):
        return max(
            Decimal("0.00"),
            money(obj.total_due) - money(obj.paid_amount),
        )

    def get_full_balance(self, obj):
        return max(
            Decimal("0.00"),
            money(obj.total_due)
            + money(obj.default_interest)
            + money(obj.late_fee)
            - money(obj.paid_amount),
        )

    def get_days_remaining(self, obj):
        today = timezone.now().date()
        return max(0, (obj.due_date - today).days)

    def get_days_overdue(self, obj):
        today = timezone.now().date()
        if obj.is_paid or obj.due_date >= today:
            return 0
        return (today - obj.due_date).days

    def get_is_overdue(self, obj):
        today = timezone.now().date()
        return bool(not obj.is_paid and obj.due_date < today)


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
            "applied_to_principal",
            "applied_to_interest",
            "applied_to_default_interest",
            "applied_to_late_fee",
            "excess_to_savings",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "paid_at",
            "applied_to_principal",
            "applied_to_interest",
            "applied_to_default_interest",
            "applied_to_late_fee",
            "excess_to_savings",
            "created_at",
        ]


class LoanPaymentCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    method = serializers.CharField(required=False, default="MANUAL")
    reference = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate_amount(self, value):
        if Decimal(value) <= 0:
            raise serializers.ValidationError("Payment amount must be greater than 0.")
        return value


# ==========================================================
# Reminder Logs
# ==========================================================
class LoanReminderLogSerializer(serializers.ModelSerializer):
    borrower_detail = SimpleUserSerializer(source="borrower", read_only=True)
    sent_by_detail = SimpleUserSerializer(source="sent_by", read_only=True)
    installment_no = serializers.IntegerField(source="installment.installment_no", read_only=True)
    due_date = serializers.DateField(source="installment.due_date", read_only=True)

    class Meta:
        model = LoanReminderLog
        fields = [
            "id",
            "loan",
            "installment",
            "installment_no",
            "due_date",
            "borrower",
            "borrower_detail",
            "reminder_type",
            "channel",
            "days_remaining",
            "days_overdue",
            "message",
            "sent_by",
            "sent_by_detail",
            "sent_at",
            "was_successful",
            "failure_reason",
        ]
        read_only_fields = fields


class LoanReminderCreateSerializer(serializers.Serializer):
    installment_id = serializers.IntegerField(required=False, allow_null=True)
    channel = serializers.ChoiceField(
        choices=["SMS", "EMAIL", "PUSH", "WHATSAPP", "MANUAL"],
        default="MANUAL",
        required=False,
    )
    message = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_installment_id(self, value):
        if value in (None, ""):
            return None

        try:
            value = int(value)
        except (TypeError, ValueError):
            raise serializers.ValidationError("Installment id must be a valid number.")

        if value <= 0:
            raise serializers.ValidationError("Installment id must be greater than 0.")

        return value


class LoanReminderPreviewSerializer(serializers.Serializer):
    loan_id = serializers.IntegerField(read_only=True)
    installment_id = serializers.IntegerField(read_only=True, allow_null=True)
    reminder_type = serializers.CharField(read_only=True)
    days_remaining = serializers.IntegerField(read_only=True)
    days_overdue = serializers.IntegerField(read_only=True)
    message = serializers.CharField(read_only=True)


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

    Member does not supply:
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
    borrower_detail = SimpleUserSerializer(source="borrower", read_only=True)
    next_unpaid_installment = serializers.SerializerMethodField()
    days_to_next_due = serializers.SerializerMethodField()
    days_overdue = serializers.SerializerMethodField()
    amount_due_now = serializers.SerializerMethodField()

    class Meta:
        model = Loan
        fields = [
            "id",
            "borrower",
            "borrower_detail",
            "product",
            "product_name",
            "principal",
            "term_weeks",
            "status",
            "is_defaulter",
            "requested_at",
            "approved_at",
            "rejected_at",
            "disbursed_at",
            "repayment_started_at",
            "defaulted_at",
            "completed_at",
            "cancelled_at",
            "created_at",
            "updated_at",
            "total_payable",
            "normal_interest_total",
            "default_interest_total",
            "late_fee_total",
            "total_paid",
            "outstanding_balance",
            "security_target",
            "security_reserved_total",
            "next_unpaid_installment",
            "days_to_next_due",
            "days_overdue",
            "amount_due_now",
        ]
        read_only_fields = fields

    def _next_unpaid(self, obj):
        if hasattr(obj, "_cached_next_unpaid_installment"):
            return obj._cached_next_unpaid_installment

        inst = obj.installments.filter(is_paid=False).order_by("installment_no").first()
        obj._cached_next_unpaid_installment = inst
        return inst

    def get_next_unpaid_installment(self, obj):
        inst = self._next_unpaid(obj)
        if not inst:
            return None

        return {
            "id": inst.id,
            "installment_no": inst.installment_no,
            "due_date": inst.due_date,
            "status": inst.status,
            "total_due": inst.total_due,
            "default_interest": getattr(inst, "default_interest", Decimal("0.00")),
            "late_fee": getattr(inst, "late_fee", Decimal("0.00")),
            "paid_amount": inst.paid_amount,
            "full_balance": max(
                Decimal("0.00"),
                money(inst.total_due)
                + money(getattr(inst, "default_interest", Decimal("0.00")))
                + money(getattr(inst, "late_fee", Decimal("0.00")))
                - money(inst.paid_amount),
            ),
        }

    def get_days_to_next_due(self, obj):
        inst = self._next_unpaid(obj)
        if not inst:
            return 0
        today = timezone.now().date()
        return max(0, (inst.due_date - today).days)

    def get_days_overdue(self, obj):
        inst = self._next_unpaid(obj)
        if not inst:
            return 0
        today = timezone.now().date()
        if inst.due_date >= today:
            return 0
        return (today - inst.due_date).days

    def get_amount_due_now(self, obj):
        today = timezone.now().date()
        installments = obj.installments.filter(is_paid=False, due_date__lte=today)
        total = Decimal("0.00")

        for inst in installments:
            total += max(
                Decimal("0.00"),
                money(inst.total_due)
                + money(getattr(inst, "default_interest", Decimal("0.00")))
                + money(getattr(inst, "late_fee", Decimal("0.00")))
                - money(inst.paid_amount),
            )

        return total


class LoanDetailSerializer(serializers.ModelSerializer):
    borrower_detail = SimpleUserSerializer(source="borrower", read_only=True)
    product_detail = LoanProductSerializer(source="product", read_only=True)
    guarantors = LoanGuarantorSerializer(many=True, read_only=True)
    security_allocations = LoanSecurityAllocationSerializer(many=True, read_only=True)
    installments = LoanInstallmentSerializer(many=True, read_only=True)
    payments = LoanPaymentSerializer(many=True, read_only=True)
    reminder_logs = LoanReminderLogSerializer(many=True, read_only=True)
    next_unpaid_installment = serializers.SerializerMethodField()
    amount_due_now = serializers.SerializerMethodField()
    days_to_next_due = serializers.SerializerMethodField()
    days_overdue = serializers.SerializerMethodField()

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
            "requested_at",
            "approved_at",
            "rejected_at",
            "disbursed_at",
            "repayment_started_at",
            "defaulted_at",
            "completed_at",
            "cancelled_at",
            "created_at",
            "updated_at",
            "total_payable",
            "normal_interest_total",
            "default_interest_total",
            "late_fee_total",
            "total_paid",
            "outstanding_balance",
            "security_target",
            "security_reserved_total",
            "member_note",
            "admin_note",
            "next_unpaid_installment",
            "amount_due_now",
            "days_to_next_due",
            "days_overdue",
            "guarantors",
            "security_allocations",
            "installments",
            "payments",
            "reminder_logs",
        ]
        read_only_fields = fields

    def _next_unpaid(self, obj):
        if hasattr(obj, "_cached_detail_next_unpaid_installment"):
            return obj._cached_detail_next_unpaid_installment

        inst = obj.installments.filter(is_paid=False).order_by("installment_no").first()
        obj._cached_detail_next_unpaid_installment = inst
        return inst

    def get_next_unpaid_installment(self, obj):
        inst = self._next_unpaid(obj)
        if not inst:
            return None
        return LoanInstallmentSerializer(inst).data

    def get_amount_due_now(self, obj):
        today = timezone.now().date()
        installments = obj.installments.filter(is_paid=False, due_date__lte=today)
        total = Decimal("0.00")

        for inst in installments:
            total += max(
                Decimal("0.00"),
                money(inst.total_due)
                + money(getattr(inst, "default_interest", Decimal("0.00")))
                + money(getattr(inst, "late_fee", Decimal("0.00")))
                - money(inst.paid_amount),
            )

        return total

    def get_days_to_next_due(self, obj):
        inst = self._next_unpaid(obj)
        if not inst:
            return 0
        today = timezone.now().date()
        return max(0, (inst.due_date - today).days)

    def get_days_overdue(self, obj):
        inst = self._next_unpaid(obj)
        if not inst:
            return 0
        today = timezone.now().date()
        if inst.due_date >= today:
            return 0
        return (today - inst.due_date).days


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
# Loan Security Preview
# ==========================================================
class LoanSecurityPreviewGuarantorSerializer(serializers.Serializer):
    guarantor_id = serializers.IntegerField()
    guarantor_name = serializers.CharField()
    available_security = serializers.DecimalField(max_digits=12, decimal_places=2)
    used_security = serializers.DecimalField(max_digits=12, decimal_places=2)


class LoanSecurityPreviewSerializer(serializers.Serializer):
    eligible = serializers.BooleanField()
    principal = serializers.DecimalField(max_digits=12, decimal_places=2)

    borrower_savings = serializers.DecimalField(max_digits=12, decimal_places=2)
    borrower_merry = serializers.DecimalField(max_digits=12, decimal_places=2)
    borrower_group = serializers.DecimalField(max_digits=12, decimal_places=2)
    borrower_total = serializers.DecimalField(max_digits=12, decimal_places=2)

    guarantor_total = serializers.DecimalField(max_digits=12, decimal_places=2)
    secured_total = serializers.DecimalField(max_digits=12, decimal_places=2)
    shortfall = serializers.DecimalField(max_digits=12, decimal_places=2)

    fully_secured = serializers.BooleanField()
    message = serializers.CharField()

    guarantors = LoanSecurityPreviewGuarantorSerializer(many=True)


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


# ==========================================================
# Reject loan
# ==========================================================
class LoanRejectSerializer(serializers.Serializer):
    rejection_reason = serializers.CharField()

# from decimal import Decimal

# from django.contrib.auth import get_user_model
# from rest_framework import serializers

# from .models import (
#     Loan,
#     LoanGuarantor,
#     LoanInstallment,
#     LoanPayment,
#     LoanProduct,
#     LoanSecurityAllocation,
#     MemberCreditProfile,
# )

# User = get_user_model()


# # ==========================================================
# # Small user serializer
# # ==========================================================
# class SimpleUserSerializer(serializers.ModelSerializer):
#     full_name = serializers.SerializerMethodField()

#     class Meta:
#         model = User
#         fields = ["id", "full_name"]

#     def get_full_name(self, obj):
#         full = ""
#         if hasattr(obj, "get_full_name"):
#             full = (obj.get_full_name() or "").strip()
#         if full:
#             return full

#         first = getattr(obj, "first_name", "") or ""
#         last = getattr(obj, "last_name", "") or ""
#         joined = f"{first} {last}".strip()
#         if joined:
#             return joined

#         username = getattr(obj, "username", "") or ""
#         if username:
#             return username

#         return str(obj)


# # ==========================================================
# # Credit Profile
# # ==========================================================
# class MemberCreditProfileSerializer(serializers.ModelSerializer):
#     class Meta:
#         model = MemberCreditProfile
#         fields = [
#             "id",
#             "user",
#             "score",
#             "total_loans",
#             "loans_completed",
#             "loans_defaulted",
#             "late_payments",
#             "updated_at",
#         ]
#         read_only_fields = fields


# # ==========================================================
# # Loan Product
# # ==========================================================
# class LoanProductSerializer(serializers.ModelSerializer):
#     class Meta:
#         model = LoanProduct
#         fields = [
#             "id",
#             "name",
#             "interest_type",
#             "annual_interest_rate",
#             "repayment_frequency",
#             "repayment_weekday",
#             "max_weeks",
#             "late_fee_rate_weekly",
#             "is_active",
#             "is_default",
#         ]
#         read_only_fields = fields


# # ==========================================================
# # Loan Guarantor
# # ==========================================================
# class LoanGuarantorSerializer(serializers.ModelSerializer):
#     guarantor_detail = SimpleUserSerializer(source="guarantor", read_only=True)

#     class Meta:
#         model = LoanGuarantor
#         fields = [
#             "id",
#             "loan",
#             "guarantor",
#             "guarantor_detail",
#             "accepted",
#             "accepted_at",
#             "reserved_amount",
#             "request_note",
#             "admin_note",
#             "created_at",
#         ]
#         read_only_fields = [
#             "id",
#             "accepted",
#             "accepted_at",
#             "reserved_amount",
#             "admin_note",
#             "created_at",
#         ]


# class AddLoanGuarantorSerializer(serializers.Serializer):
#     loan = serializers.PrimaryKeyRelatedField(queryset=Loan.objects.all())
#     guarantor = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
#     request_note = serializers.CharField(required=False, allow_blank=True, default="")

#     def validate(self, attrs):
#         loan = attrs["loan"]
#         guarantor = attrs["guarantor"]
#         request = self.context.get("request")

#         if request and loan.borrower_id != request.user.id:
#             raise serializers.ValidationError("Only the borrower can add guarantors to this loan.")

#         if loan.status not in ("PENDING", "UNDER_REVIEW"):
#             raise serializers.ValidationError("You can only add guarantors to a pending or under-review loan.")

#         if loan.borrower_id == guarantor.id:
#             raise serializers.ValidationError("Borrower cannot guarantee their own loan.")

#         if LoanGuarantor.objects.filter(loan=loan, guarantor=guarantor).exists():
#             raise serializers.ValidationError("This guarantor has already been added to the loan.")

#         return attrs

#     def create(self, validated_data):
#         return LoanGuarantor.objects.create(
#             loan=validated_data["loan"],
#             guarantor=validated_data["guarantor"],
#             request_note=validated_data.get("request_note", ""),
#         )


# # ==========================================================
# # Security Allocation
# # ==========================================================
# class LoanSecurityAllocationSerializer(serializers.ModelSerializer):
#     owner_detail = SimpleUserSerializer(source="owner_user", read_only=True)
#     guarantor_link_id = serializers.IntegerField(source="guarantor_link.id", read_only=True)

#     class Meta:
#         model = LoanSecurityAllocation
#         fields = [
#             "id",
#             "loan",
#             "source_type",
#             "owner_user",
#             "owner_detail",
#             "guarantor_link_id",
#             "savings_account",
#             "merry",
#             "group",
#             "amount",
#             "is_active",
#             "created_at",
#             "released_at",
#         ]
#         read_only_fields = fields


# # ==========================================================
# # Installment
# # ==========================================================
# class LoanInstallmentSerializer(serializers.ModelSerializer):
#     class Meta:
#         model = LoanInstallment
#         fields = [
#             "id",
#             "loan",
#             "installment_no",
#             "due_date",
#             "principal_due",
#             "interest_due",
#             "total_due",
#             "late_fee",
#             "paid_amount",
#             "is_paid",
#         ]
#         read_only_fields = fields


# # ==========================================================
# # Payment
# # ==========================================================
# class LoanPaymentSerializer(serializers.ModelSerializer):
#     class Meta:
#         model = LoanPayment
#         fields = [
#             "id",
#             "loan",
#             "amount",
#             "paid_at",
#             "method",
#             "reference",
#         ]
#         read_only_fields = ["id", "paid_at"]


# class LoanPaymentCreateSerializer(serializers.Serializer):
#     amount = serializers.DecimalField(max_digits=12, decimal_places=2)
#     method = serializers.CharField(required=False, default="MANUAL")
#     reference = serializers.CharField(required=False, allow_blank=True, allow_null=True)

#     def validate_amount(self, value):
#         if Decimal(value) <= 0:
#             raise serializers.ValidationError("Payment amount must be greater than 0.")
#         return value


# # ==========================================================
# # Loan Request
# # ==========================================================
# class LoanRequestSerializer(serializers.Serializer):
#     """
#     Member-facing request serializer.

#     Member supplies:
#     - principal
#     - term_weeks
#     - guarantor_ids
#     - optional member note

#     Member does NOT supply:
#     - product
#     - merry
#     - group
#     - security inputs
#     """

#     principal = serializers.DecimalField(max_digits=12, decimal_places=2)
#     term_weeks = serializers.IntegerField(min_value=1)
#     guarantor_ids = serializers.ListField(
#         child=serializers.IntegerField(min_value=1),
#         required=False,
#         allow_empty=True,
#         default=list,
#     )
#     member_note = serializers.CharField(required=False, allow_blank=True, default="")

#     def validate_principal(self, value):
#         if Decimal(value) <= 0:
#             raise serializers.ValidationError("Principal must be greater than 0.")
#         return value


# # ==========================================================
# # Admin / Internal Loan Create
# # ==========================================================
# class LoanCreateAdminSerializer(serializers.ModelSerializer):
#     """
#     Optional admin/internal serializer if admin wants direct control.
#     """

#     class Meta:
#         model = Loan
#         fields = [
#             "id",
#             "borrower",
#             "product",
#             "principal",
#             "term_weeks",
#             "status",
#             "member_note",
#             "admin_note",
#         ]


# # ==========================================================
# # Loan list/detail
# # ==========================================================
# class LoanListSerializer(serializers.ModelSerializer):
#     product_name = serializers.CharField(source="product.name", read_only=True)

#     class Meta:
#         model = Loan
#         fields = [
#             "id",
#             "borrower",
#             "product",
#             "product_name",
#             "principal",
#             "term_weeks",
#             "status",
#             "is_defaulter",
#             "approved_at",
#             "rejected_at",
#             "completed_at",
#             "created_at",
#             "total_payable",
#             "total_paid",
#             "outstanding_balance",
#             "security_target",
#             "security_reserved_total",
#         ]
#         read_only_fields = fields


# class LoanDetailSerializer(serializers.ModelSerializer):
#     borrower_detail = SimpleUserSerializer(source="borrower", read_only=True)
#     product_detail = LoanProductSerializer(source="product", read_only=True)
#     guarantors = LoanGuarantorSerializer(many=True, read_only=True)
#     security_allocations = LoanSecurityAllocationSerializer(many=True, read_only=True)
#     installments = LoanInstallmentSerializer(many=True, read_only=True)
#     payments = LoanPaymentSerializer(many=True, read_only=True)

#     class Meta:
#         model = Loan
#         fields = [
#             "id",
#             "borrower",
#             "borrower_detail",
#             "product",
#             "product_detail",
#             "principal",
#             "term_weeks",
#             "status",
#             "is_defaulter",
#             "approved_at",
#             "rejected_at",
#             "completed_at",
#             "created_at",
#             "total_payable",
#             "total_paid",
#             "outstanding_balance",
#             "security_target",
#             "security_reserved_total",
#             "member_note",
#             "admin_note",
#             "guarantors",
#             "security_allocations",
#             "installments",
#             "payments",
#         ]
#         read_only_fields = fields


# # ==========================================================
# # Eligibility preview
# # ==========================================================
# class LoanEligibilityPreviewSerializer(serializers.Serializer):
#     eligible = serializers.BooleanField(read_only=True)
#     max_allowed = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
#     available_savings = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
#     has_active_loan = serializers.BooleanField(read_only=True)
#     missing_deposit_months = serializers.ListField(
#         child=serializers.CharField(),
#         read_only=True,
#     )
#     reason = serializers.CharField(read_only=True)


# # ==========================================================
# # Loan Security Preview (NEW)
# # ==========================================================
# class LoanSecurityPreviewGuarantorSerializer(serializers.Serializer):
#     guarantor_id = serializers.IntegerField()
#     guarantor_name = serializers.CharField()
#     available_security = serializers.DecimalField(max_digits=12, decimal_places=2)
#     used_security = serializers.DecimalField(max_digits=12, decimal_places=2)


# class LoanSecurityPreviewSerializer(serializers.Serializer):
#     eligible = serializers.BooleanField()
#     principal = serializers.DecimalField(max_digits=12, decimal_places=2)

#     borrower_savings = serializers.DecimalField(max_digits=12, decimal_places=2)
#     borrower_merry = serializers.DecimalField(max_digits=12, decimal_places=2)
#     borrower_group = serializers.DecimalField(max_digits=12, decimal_places=2)
#     borrower_total = serializers.DecimalField(max_digits=12, decimal_places=2)

#     guarantor_total = serializers.DecimalField(max_digits=12, decimal_places=2)
#     secured_total = serializers.DecimalField(max_digits=12, decimal_places=2)
#     shortfall = serializers.DecimalField(max_digits=12, decimal_places=2)

#     fully_secured = serializers.BooleanField()
#     message = serializers.CharField()

#     guarantors = LoanSecurityPreviewGuarantorSerializer(many=True)


# # ==========================================================
# # Guarantor candidate list
# # ==========================================================
# class GuarantorCandidateSerializer(serializers.ModelSerializer):
#     full_name = serializers.SerializerMethodField()

#     class Meta:
#         model = User
#         fields = ["id", "full_name"]

#     def get_full_name(self, obj):
#         full = ""
#         if hasattr(obj, "get_full_name"):
#             full = (obj.get_full_name() or "").strip()
#         if full:
#             return full

#         first = getattr(obj, "first_name", "") or ""
#         last = getattr(obj, "last_name", "") or ""
#         joined = f"{first} {last}".strip()
#         if joined:
#             return joined

#         username = getattr(obj, "username", "") or ""
#         if username:
#             return username

#         return str(obj)


# # ==========================================================
# # Reject loan
# # ==========================================================
# class LoanRejectSerializer(serializers.Serializer):
#     rejection_reason = serializers.CharField()