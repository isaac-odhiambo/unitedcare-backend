from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from rest_framework import generics, permissions, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

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
)
from .services import (
    approve_loan_and_create_schedule,
    apply_payment_to_loan,
    get_loan_eligibility_preview,
    record_loan_payment,
    request_global_loan,
)

User = get_user_model()


# ==========================
# Loans: My Loans
# ==========================
class MyLoansView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = LoanListSerializer

    def get_queryset(self):
        return (
            Loan.objects.filter(borrower=self.request.user)
            .select_related("product", "borrower")
            .order_by("-id")
        )


# ==========================
# Loans: Eligibility Preview
# ==========================
class LoanEligibilityPreviewView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        preview = get_loan_eligibility_preview(user=request.user)
        data = LoanEligibilityPreviewSerializer(preview).data
        return Response(data, status=status.HTTP_200_OK)


# ==========================
# Loans: Guarantor Candidates
# ==========================
class LoanGuarantorCandidatesView(generics.ListAPIView):
    """
    Platform-level guarantor list.
    Excludes the current user.
    Supports simple search.
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

        if hasattr(User, "is_active"):
            qs = qs.filter(is_active=True)

        return qs.order_by("id")[:50]


# ==========================
# Loans: Create (Request)
# ==========================
class RequestLoanView(APIView):
    """
    Member requests a global loan.

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

        loan = (
            Loan.objects.select_related("product", "borrower")
            .prefetch_related(
                "guarantors",
                "guarantors__guarantor",
                "security_allocations",
                "installments",
                "payments",
            )
            .get(id=loan.id)
        )

        return Response(
            {
                "message": "Loan request submitted.",
                "loan": LoanDetailSerializer(loan).data,
            },
            status=status.HTTP_201_CREATED,
        )


# ==========================
# Loans: Detail
# ==========================
class LoanDetailView(generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = LoanDetailSerializer
    queryset = (
        Loan.objects.select_related("product", "borrower")
        .prefetch_related(
            "guarantors",
            "guarantors__guarantor",
            "security_allocations",
            "installments",
            "payments",
        )
    )

    def get_object(self):
        obj = super().get_object()

        if obj.borrower_id == self.request.user.id:
            return obj

        if LoanGuarantor.objects.filter(loan=obj, guarantor=self.request.user).exists():
            return obj

        if self.request.user.is_staff or self.request.user.is_superuser:
            return obj

        raise PermissionDenied("You do not have permission to view this loan.")


# ==========================
# Guarantors: Add guarantor
# ==========================
class AddGuarantorView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        ser = AddLoanGuarantorSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)
        guarantor = ser.save()

        return Response(
            {
                "message": "Guarantor added. Waiting for acceptance.",
                "guarantor": LoanGuarantorSerializer(guarantor).data,
            },
            status=status.HTTP_201_CREATED,
        )


# ==========================
# Guarantors: My pending guarantee requests
# ==========================
class MyGuaranteeRequestsView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = LoanGuarantorSerializer

    def get_queryset(self):
        return (
            LoanGuarantor.objects.filter(guarantor=self.request.user, accepted=False)
            .select_related("loan", "loan__borrower", "loan__product", "guarantor")
            .order_by("-id")
        )


# ==========================
# Guarantors: Accept
# ==========================
class AcceptGuaranteeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def patch(self, request, guarantor_id: int):
        try:
            guarantor_link = (
                LoanGuarantor.objects.select_for_update()
                .select_related("loan", "guarantor")
                .get(id=guarantor_id)
            )
        except LoanGuarantor.DoesNotExist:
            raise ValidationError("Guarantee request not found.")

        if guarantor_link.guarantor_id != request.user.id:
            raise PermissionDenied("This guarantee request is not yours.")

        if guarantor_link.accepted:
            return Response({"message": "Already accepted."}, status=status.HTTP_200_OK)

        if guarantor_link.loan.status not in ("PENDING", "UNDER_REVIEW"):
            raise ValidationError("You can only accept guarantee for pending/review loans.")

        guarantor_link.accepted = True
        guarantor_link.accepted_at = timezone.now()
        guarantor_link.full_clean()
        guarantor_link.save(update_fields=["accepted", "accepted_at"])

        return Response({"message": "Guarantee accepted."}, status=status.HTTP_200_OK)


# ==========================
# Guarantors: Reject
# ==========================
class RejectGuaranteeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def patch(self, request, guarantor_id: int):
        try:
            guarantor_link = (
                LoanGuarantor.objects.select_for_update()
                .select_related("loan", "guarantor")
                .get(id=guarantor_id)
            )
        except LoanGuarantor.DoesNotExist:
            raise ValidationError("Guarantee request not found.")

        if guarantor_link.guarantor_id != request.user.id:
            raise PermissionDenied("This guarantee request is not yours.")

        if guarantor_link.accepted:
            raise ValidationError("Cannot reject: already accepted.")

        guarantor_link.delete()
        return Response({"message": "Guarantee rejected."}, status=status.HTTP_200_OK)


# ==========================
# Admin: Approve loan
# ==========================
class ApproveLoanView(APIView):
    """
    Admin marks loan as APPROVED, reserves security, and generates schedule.
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def patch(self, request, loan_id: int):
        if not (request.user.is_staff or request.user.is_superuser):
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

        loan = (
            Loan.objects.select_related("product", "borrower")
            .prefetch_related(
                "guarantors",
                "guarantors__guarantor",
                "security_allocations",
                "installments",
                "payments",
            )
            .get(id=loan.id)
        )

        return Response(
            {
                "message": "Loan approved and schedule generated.",
                "loan": LoanDetailSerializer(loan).data,
            },
            status=status.HTTP_200_OK,
        )


# ==========================
# Payments: Pay loan
# ==========================
class PayLoanView(APIView):
    """
    Records a payment and applies it to installments.
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, loan_id: int):
        ser = LoanPaymentCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        amount = Decimal(ser.validated_data["amount"])
        method = ser.validated_data.get("method", "MANUAL")
        reference = ser.validated_data.get("reference")

        try:
            loan = Loan.objects.select_for_update().get(id=loan_id)
        except Loan.DoesNotExist:
            raise ValidationError("Loan not found.")

        if loan.borrower_id != request.user.id:
            raise PermissionDenied("Only the borrower can pay this loan.")

        record_loan_payment(loan, amount, method=method, reference=reference)
        loan = apply_payment_to_loan(loan, amount)

        return Response(
            {
                "message": "Payment applied.",
                "loan_status": loan.status,
                "total_paid": str(loan.total_paid),
                "outstanding_balance": str(loan.outstanding_balance),
            },
            status=status.HTTP_200_OK,
        )