from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from rest_framework import generics, permissions, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from notifications.utils import create_notification

from .models import Loan, LoanGuarantor
from .serializers import (
    AddLoanGuarantorSerializer,
    GuarantorCandidateSerializer,
    LoanDetailSerializer,
    LoanEligibilityPreviewSerializer,
    LoanGuarantorSerializer,
    LoanListSerializer,
    LoanPaymentCreateSerializer,
    LoanRequestSerializer,
    LoanSecurityPreviewSerializer,
)
from .services import (
    approve_loan_and_create_schedule,
    apply_payment_to_loan,
    get_loan_eligibility_preview,
    get_loan_security_preview,
    record_loan_payment,
    request_global_loan,
)

User = get_user_model()


# ==========================================================
# Helpers
# ==========================================================
def _is_admin(user) -> bool:
    return bool(
        getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)
    )


def _loan_detail_queryset():
    return Loan.objects.select_related("product", "borrower").prefetch_related(
        "guarantors",
        "guarantors__guarantor",
        "security_allocations",
        "installments",
        "payments",
    )


def _user_field_names() -> set[str]:
    return {f.name for f in User._meta.get_fields()}


# ==========================================================
# Loans: My Loans
# ==========================================================
class MyLoansView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = LoanListSerializer

    def get_queryset(self):
        return (
            Loan.objects.filter(borrower=self.request.user)
            .select_related("product", "borrower")
            .order_by("-id")
        )


# ==========================================================
# Loans: Eligibility Preview
# ==========================================================
class LoanEligibilityPreviewView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        preview = get_loan_eligibility_preview(user=request.user)
        data = LoanEligibilityPreviewSerializer(preview).data
        return Response(data, status=status.HTTP_200_OK)


# ==========================================================
# Loans: Security Preview
# ==========================================================
class LoanSecurityPreviewView(APIView):
    """
    Returns current security breakdown for a requested loan:
    - borrower savings
    - borrower merry security
    - borrower group share security
    - guarantors
    - total secured
    - shortfall
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        principal = request.data.get("principal", 0)
        guarantor_ids = request.data.get("guarantor_ids", []) or []

        try:
            principal = Decimal(str(principal))
        except Exception:
            raise ValidationError({"principal": "Invalid amount."})

        preview = get_loan_security_preview(
            borrower=request.user,
            principal=principal,
            guarantor_ids=guarantor_ids,
        )

        serializer = LoanSecurityPreviewSerializer(preview)
        return Response(serializer.data, status=status.HTTP_200_OK)


# ==========================================================
# Loans: Guarantor Candidates
# ==========================================================
class LoanGuarantorCandidatesView(generics.ListAPIView):
    """
    Platform-level guarantor list.
    Excludes the current user.
    Supports simple search.
    Only returns active/approved users based on the actual User model fields.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = GuarantorCandidateSerializer

    def get_queryset(self):
        qs = User.objects.exclude(id=self.request.user.id)

        q = (self.request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
                | Q(username__icontains=q)
            )

        fields = _user_field_names()

        if "is_active" in fields:
            qs = qs.filter(is_active=True)

        if "is_approved" in fields:
            qs = qs.filter(is_approved=True)
        elif "status" in fields:
            qqs = qs.filter(status="approved")

        return qs.order_by("id")[:50]


# ==========================================================
# Loans: Create (Request)
# ==========================================================
class RequestLoanView(APIView):
    """
    Member requests a platform loan.

    Member sends:
    - principal
    - term_weeks
    - guarantor_ids
    - optional member_note

    Product is selected internally by backend.
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        ser = LoanRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        loan = request_global_loan(
            borrower=request.user,
            principal=ser.validated_data["principal"],
            term_weeks=ser.validated_data["term_weeks"],
            guarantor_ids=ser.validated_data.get("guarantor_ids", []),
            member_note=ser.validated_data.get("member_note", ""),
        )

        create_notification(
            user=request.user,
            title="Loan Request Submitted",
            message="Your loan request was submitted successfully and is waiting for review.",
            notification_type="INFO",
            action_url="/loans",
            loan_id=loan.id,
        )

        loan = _loan_detail_queryset().get(id=loan.id)

        return Response(
            {
                "message": "Loan request submitted successfully.",
                "loan_id": loan.id,
                "status": loan.status,
                "note": "Your loan can be approved once it becomes fully secured.",
                "loan": LoanDetailSerializer(loan).data,
            },
            status=status.HTTP_201_CREATED,
        )


# ==========================================================
# Loans: Detail
# ==========================================================
class LoanDetailView(generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = LoanDetailSerializer
    queryset = _loan_detail_queryset()

    def get_object(self):
        obj = super().get_object()

        if obj.borrower_id == self.request.user.id:
            return obj

        if LoanGuarantor.objects.filter(
            loan=obj, guarantor=self.request.user
        ).exists():
            return obj

        if _is_admin(self.request.user):
            return obj

        raise PermissionDenied("You do not have permission to view this loan.")


# ==========================================================
# Guarantors: Add guarantor
# ==========================================================
class AddGuarantorView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        ser = AddLoanGuarantorSerializer(
            data=request.data,
            context={"request": request},
        )
        ser.is_valid(raise_exception=True)
        guarantor = ser.save()

        create_notification(
            user=guarantor.guarantor,
            title="Guarantee Request",
            message=f"You have been requested to guarantee loan #{guarantor.loan.id}.",
            notification_type="ACTION",
            action_url="/loans/guarantees",
            loan_id=guarantor.loan.id,
        )

        return Response(
            {
                "message": "Guarantor added successfully.",
                "note": "Waiting for guarantor acceptance.",
                "guarantor": LoanGuarantorSerializer(guarantor).data,
            },
            status=status.HTTP_201_CREATED,
        )


# ==========================================================
# Guarantors: My pending guarantee requests
# ==========================================================
class MyGuaranteeRequestsView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = LoanGuarantorSerializer

    def get_queryset(self):
        return (
            LoanGuarantor.objects.filter(
                guarantor=self.request.user,
                accepted=False,
            )
            .select_related("loan", "loan__borrower", "loan__product", "guarantor")
            .order_by("-id")
        )


# ==========================================================
# Guarantors: Accept
# ==========================================================
class AcceptGuaranteeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def patch(self, request, guarantor_id: int):
        try:
            guarantor_link = (
                LoanGuarantor.objects.select_for_update()
                .select_related("loan", "loan__borrower", "guarantor")
                .get(id=guarantor_id)
            )
        except LoanGuarantor.DoesNotExist:
            raise ValidationError("Guarantee request not found.")

        if guarantor_link.guarantor_id != request.user.id:
            raise PermissionDenied("This guarantee request is not yours.")

        if guarantor_link.accepted:
            return Response(
                {"message": "Guarantee already accepted."},
                status=status.HTTP_200_OK,
            )

        if guarantor_link.loan.status not in ("PENDING", "UNDER_REVIEW"):
            raise ValidationError(
                "You can only accept guarantee for pending or under-review loans."
            )

        guarantor_link.accepted = True
        guarantor_link.accepted_at = timezone.now()
        guarantor_link.full_clean()
        guarantor_link.save(update_fields=["accepted", "accepted_at"])

        create_notification(
            user=guarantor_link.loan.borrower,
            title="Guarantee Accepted",
            message=f"Your guarantor has accepted loan #{guarantor_link.loan.id}.",
            notification_type="SUCCESS",
            action_url="/loans",
            loan_id=guarantor_link.loan.id,
        )

        return Response(
            {
                "message": "Guarantee accepted.",
                "note": "The loan can now move to approval once total security reaches 100%.",
            },
            status=status.HTTP_200_OK,
        )


# ==========================================================
# Guarantors: Reject
# ==========================================================
class RejectGuaranteeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def patch(self, request, guarantor_id: int):
        try:
            guarantor_link = (
                LoanGuarantor.objects.select_for_update()
                .select_related("loan", "loan__borrower", "guarantor")
                .get(id=guarantor_id)
            )
        except LoanGuarantor.DoesNotExist:
            raise ValidationError("Guarantee request not found.")

        if guarantor_link.guarantor_id != request.user.id:
            raise PermissionDenied("This guarantee request is not yours.")

        if guarantor_link.accepted:
            raise ValidationError("Cannot reject a guarantee that is already accepted.")

        borrower = guarantor_link.loan.borrower
        loan_id = guarantor_link.loan.id

        guarantor_link.delete()

        create_notification(
            user=borrower,
            title="Guarantee Rejected",
            message=f"A guarantor rejected loan #{loan_id}. Please select another guarantor if needed.",
            notification_type="WARNING",
            action_url="/loans",
            loan_id=loan_id,
        )

        return Response(
            {"message": "Guarantee rejected."},
            status=status.HTTP_200_OK,
        )


# ==========================================================
# Admin: Approve loan
# ==========================================================
class ApproveLoanView(APIView):
    """
    Admin approves a loan, reserves security, and generates schedule.
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def patch(self, request, loan_id: int):
        if not _is_admin(request.user):
            raise PermissionDenied("Only admin can approve loans.")

        try:
            loan = (
                Loan.objects.select_for_update()
                .select_related("product", "borrower")
                .prefetch_related("guarantors", "guarantors__guarantor")
                .get(id=loan_id)
            )
        except Loan.DoesNotExist:
            raise ValidationError("Loan not found.")

        loan = approve_loan_and_create_schedule(loan)

        extra_update_fields = []
        if hasattr(loan, "reviewed_by"):
            loan.reviewed_by = request.user
            extra_update_fields.append("reviewed_by")
        if hasattr(loan, "reviewed_at"):
            loan.reviewed_at = timezone.now()
            extra_update_fields.append("reviewed_at")
        if hasattr(loan, "rejection_reason"):
            loan.rejection_reason = None
            extra_update_fields.append("rejection_reason")

        if extra_update_fields:
            loan.save(update_fields=extra_update_fields)

        create_notification(
            user=loan.borrower,
            created_by=request.user,
            title="Loan Approved",
            message="Your loan request has been approved and your repayment schedule is now ready.",
            notification_type="SUCCESS",
            action_url="/loans",
            loan_id=loan.id,
        )

        loan = _loan_detail_queryset().get(id=loan.id)

        return Response(
            {
                "message": "Loan approved successfully.",
                "loan": LoanDetailSerializer(loan).data,
            },
            status=status.HTTP_200_OK,
        )


# ==========================================================
# Admin: Reject loan
# ==========================================================
class RejectLoanView(APIView):
    """
    Admin rejects a loan and sends rejection reason to borrower.
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def patch(self, request, loan_id: int):
        if not _is_admin(request.user):
            raise PermissionDenied("Only admin can reject loans.")

        rejection_reason = (request.data.get("rejection_reason") or "").strip()
        if not rejection_reason:
            raise ValidationError({"rejection_reason": "This field is required."})

        try:
            loan = (
                Loan.objects.select_for_update()
                .select_related("product", "borrower")
                .prefetch_related("guarantors", "guarantors__guarantor")
                .get(id=loan_id)
            )
        except Loan.DoesNotExist:
            raise ValidationError("Loan not found.")

        if loan.status not in ("PENDING", "UNDER_REVIEW"):
            raise ValidationError("Only pending or under-review loans can be rejected.")

        loan.status = "REJECTED"
        update_fields = ["status"]

        if hasattr(loan, "rejection_reason"):
            loan.rejection_reason = rejection_reason
            update_fields.append("rejection_reason")

        if hasattr(loan, "reviewed_by"):
            loan.reviewed_by = request.user
            update_fields.append("reviewed_by")

        if hasattr(loan, "reviewed_at"):
            loan.reviewed_at = timezone.now()
            update_fields.append("reviewed_at")

        loan.save(update_fields=update_fields)

        create_notification(
            user=loan.borrower,
            created_by=request.user,
            title="Loan Rejected",
            message=f"Your loan request has been rejected. Reason: {rejection_reason}",
            notification_type="ERROR",
            action_url="/loans",
            loan_id=loan.id,
        )

        loan = _loan_detail_queryset().get(id=loan.id)

        return Response(
            {
                "message": "Loan rejected.",
                "note": "You can submit a new request after adjusting your amount or guarantors.",
                "loan": LoanDetailSerializer(loan).data,
            },
            status=status.HTTP_200_OK,
        )


# ==========================================================
# Payments: Pay loan
# ==========================================================
class PayLoanView(APIView):
    """
    Records a payment and applies it to installments.
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, loan_id: int):
        ser = LoanPaymentCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        amount = Decimal(str(ser.validated_data["amount"]))
        method = ser.validated_data.get("method", "MANUAL")
        reference = ser.validated_data.get("reference")

        try:
            loan = Loan.objects.select_for_update().get(id=loan_id)
        except Loan.DoesNotExist:
            raise ValidationError("Loan not found.")

        if loan.borrower_id != request.user.id:
            raise PermissionDenied("Only the borrower can pay this loan.")

        record_loan_payment(
            loan,
            amount,
            method=method,
            reference=reference,
        )
        loan = apply_payment_to_loan(loan, amount)

        create_notification(
            user=request.user,
            title="Loan Payment Received",
            message=f"Your payment of {amount} has been received successfully.",
            notification_type="SUCCESS",
            action_url="/loans",
            loan_id=loan.id,
        )

        loan = _loan_detail_queryset().get(id=loan.id)

        return Response(
            {
                "message": "Payment received successfully.",
                "loan_status": loan.status,
                "total_paid": str(loan.total_paid),
                "outstanding_balance": str(loan.outstanding_balance),
                "note": "Your loan will be marked complete automatically once fully paid.",
                "loan": LoanDetailSerializer(loan).data,
            },
            status=status.HTTP_200_OK,
        )