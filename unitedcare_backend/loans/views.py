# loans/views.py (COMPLETE + UPDATED)
# ----------------------------------
# ✅ Fixes your ImportError (no generate_schedule_for_loan import)
# ✅ Matches your updated services.py:
#    - LoanContext
#    - validate_loan_eligibility
#    - approve_loan_and_create_schedule
#    - record_loan_payment
#    - apply_payment_to_loan
#
# ✅ Works with your models:
#    - Loan has FK: merry, group (exactly one)
#    - LoanGuarantor has: loan, guarantor, accepted, accepted_at, reserved_amount
#
# Notes:
# - You must enforce admin/superadmin permission for approval (I added a safe check).
#   If you already have IsAdminOrSuperAdmin permission, plug it in.
# - Serializer names are assumed as you listed. If yours differ, rename the imports.

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError, PermissionDenied
from rest_framework.views import APIView

from .models import Loan, LoanGuarantor
from .serializers import (
    LoanSerializer,
    LoanCreateSerializer,
    LoanGuarantorSerializer,
    LoanPaymentSerializer,
)
from .services import (
    LoanContext,
    validate_loan_eligibility,
    approve_loan_and_create_schedule,
    record_loan_payment,
    apply_payment_to_loan,
)


# ==========================
# Loans: My Loans
# ==========================
class MyLoansView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = LoanSerializer

    def get_queryset(self):
        return (
            Loan.objects.filter(borrower=self.request.user)
            .select_related("product", "merry", "group")
            .order_by("-id")
        )


# ==========================
# Loans: Create (Request)
# ==========================
class RequestLoanView(generics.CreateAPIView):
    """
    Request a loan:
    - runs eligibility checks
    - creates Loan as PENDING
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = LoanCreateSerializer

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)

        user = request.user
        principal: Decimal = ser.validated_data["principal"]

        merry = ser.validated_data.get("merry")
        group = ser.validated_data.get("group")

        ctx = LoanContext(
            merry_id=merry.id if merry else None,
            group_id=group.id if group else None,
        )

        validate_loan_eligibility(user=user, ctx=ctx, principal=principal)

        loan = ser.save(borrower=user, status="PENDING")

        # Return full loan payload
        return Response(
            {"message": "Loan request submitted.", "loan": LoanSerializer(loan).data},
            status=status.HTTP_201_CREATED,
        )


# ==========================
# Loans: Detail (Borrower only)
# ==========================
class LoanDetailView(generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = LoanSerializer
    queryset = Loan.objects.select_related("product", "merry", "group", "borrower")

    def get_object(self):
        obj = super().get_object()
        if obj.borrower_id != self.request.user.id:
            raise PermissionDenied("You do not have permission to view this loan.")
        return obj


# ==========================
# Guarantors: Add guarantor (Borrower)
# ==========================
class AddGuarantorView(generics.CreateAPIView):
    """
    Borrower adds guarantor(s) to their loan.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = LoanGuarantorSerializer

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)

        loan: Loan = ser.validated_data["loan"]

        if loan.borrower_id != request.user.id:
            raise PermissionDenied("Only the borrower can add guarantors for this loan.")

        if loan.status not in ("PENDING", "UNDER_REVIEW"):
            raise ValidationError("You can only add guarantors to a pending/review loan.")

        g = ser.save()
        return Response(
            {"message": "Guarantor added. Waiting for acceptance.", "guarantor": LoanGuarantorSerializer(g).data},
            status=status.HTTP_201_CREATED,
        )


# ==========================
# Guarantors: My pending guarantee requests (Guarantor)
# ==========================
class MyGuaranteeRequestsView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = LoanGuarantorSerializer

    def get_queryset(self):
        return (
            LoanGuarantor.objects.filter(guarantor=self.request.user, accepted=False)
            .select_related("loan", "loan__merry", "loan__group", "loan__borrower")
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
            g = LoanGuarantor.objects.select_for_update().select_related("loan").get(id=guarantor_id)
        except LoanGuarantor.DoesNotExist:
            raise ValidationError("Guarantee request not found.")

        if g.guarantor_id != request.user.id:
            raise PermissionDenied("This guarantee request is not yours.")

        if g.accepted:
            return Response({"message": "Already accepted."}, status=status.HTTP_200_OK)

        if g.loan.status not in ("PENDING", "UNDER_REVIEW"):
            raise ValidationError("You can only accept guarantee for pending/review loans.")

        g.accepted = True
        g.accepted_at = timezone.now()
        g.full_clean()  # enforces guarantor rules in model.clean()
        g.save(update_fields=["accepted", "accepted_at"])

        return Response({"message": "Guarantee accepted."}, status=status.HTTP_200_OK)


# ==========================
# Guarantors: Reject (delete request)
# ==========================
class RejectGuaranteeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def patch(self, request, guarantor_id: int):
        try:
            g = LoanGuarantor.objects.select_for_update().select_related("loan").get(id=guarantor_id)
        except LoanGuarantor.DoesNotExist:
            raise ValidationError("Guarantee request not found.")

        if g.guarantor_id != request.user.id:
            raise PermissionDenied("This guarantee request is not yours.")

        if g.accepted:
            raise ValidationError("Cannot reject: already accepted.")

        g.delete()
        return Response({"message": "Guarantee rejected."}, status=status.HTTP_200_OK)


# ==========================
# Admin/Approver: Approve loan
# ==========================
class ApproveLoanView(APIView):
    """
    Approver marks loan as APPROVED, reserves security, and generates schedule.
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def patch(self, request, loan_id: int):
        # ✅ Simple safe permission gate:
        # Replace with your custom IsAdminOrSuperAdmin if you have it.
        if not (request.user.is_staff or request.user.is_superuser):
            raise PermissionDenied("Only admin can approve loans.")

        try:
            loan = (
                Loan.objects.select_for_update()
                .select_related("product", "borrower", "merry", "group")
                .get(id=loan_id)
            )
        except Loan.DoesNotExist:
            raise ValidationError("Loan not found.")

        loan = approve_loan_and_create_schedule(loan)

        return Response(
            {"message": "Loan approved and schedule generated.", "loan": LoanSerializer(loan).data},
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
        ser = LoanPaymentSerializer(data=request.data)
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