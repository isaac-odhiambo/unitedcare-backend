from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError

from .models import SavingsAccount, SavingsTransaction
from .serializers import (
    SavingsAccountSerializer,
    CreateSavingsAccountSerializer,
    SavingsTransactionSerializer,
    ManualDepositSerializer,
)
from .services import create_account, manual_deposit, get_account_or_404_for_user


class MySavingsAccountsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        qs = SavingsAccount.objects.filter(user=request.user).order_by("id")
        return Response(SavingsAccountSerializer(qs, many=True).data, status=status.HTTP_200_OK)


class CreateSavingsAccountView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        ser = CreateSavingsAccountSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        acct = create_account(
            user=request.user,
            name=ser.validated_data["name"],
            account_type=ser.validated_data["account_type"],
            locked_until=ser.validated_data.get("locked_until"),
            target_amount=ser.validated_data.get("target_amount"),
            target_deadline=ser.validated_data.get("target_deadline"),
        )
        return Response(SavingsAccountSerializer(acct).data, status=status.HTTP_201_CREATED)


class DepositToSavingsView(APIView):
    """
    Manual deposits only.
    MPesa deposits are applied via payments callback -> savings.services.apply_mpesa_deposit()
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        ser = ManualDepositSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        acct = manual_deposit(
            user=request.user,
            account_id=int(ser.validated_data["account_id"]),
            amount=ser.validated_data["amount"],
            reference=(ser.validated_data.get("reference") or "").strip() or None,
            note=(ser.validated_data.get("note") or "").strip() or None,
        )

        return Response(
            {"message": "Deposit successful.", "account": SavingsAccountSerializer(acct).data},
            status=status.HTTP_200_OK,
        )


class SavingsAccountHistoryView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, account_id: int):
        acct = get_account_or_404_for_user(int(account_id), request.user)
        txns = SavingsTransaction.objects.filter(account=acct).order_by("-created_at")[:200]

        return Response(
            {
                "account": SavingsAccountSerializer(acct).data,
                "transactions": SavingsTransactionSerializer(txns, many=True).data,
            },
            status=status.HTTP_200_OK,
        )