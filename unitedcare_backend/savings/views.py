# savings/views.py
# Updated for PERSONAL savings (no merry/group on SavingsAccount)
# - Supports: deposit, list accounts, account history, withdraw request workflow
# - Withdrawal uses available_balance (balance - reserved_amount)
# - Uses DRF + JWT default auth (your project settings)

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError, PermissionDenied

from .models import SavingsAccount, SavingsTransaction, WithdrawRequest


# -----------------------------
# Helpers
# -----------------------------

def q2(x: Decimal) -> Decimal:
    return Decimal(x).quantize(Decimal("0.01"))


def get_account_or_404_for_user(account_id: int, user) -> SavingsAccount:
    acct = SavingsAccount.objects.filter(id=account_id, user=user, is_active=True).first()
    if not acct:
        raise ValidationError("Savings account not found.")
    return acct


# -----------------------------
# Accounts
# -----------------------------

class MySavingsAccountsView(generics.ListAPIView):
    """
    GET /api/savings/accounts/
    List the logged-in user's savings accounts
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        accounts = SavingsAccount.objects.filter(user=request.user).order_by("id")
        data = []
        for a in accounts:
            data.append(
                {
                    "id": a.id,
                    "name": a.name,
                    "account_type": a.account_type,
                    "balance": str(a.balance),
                    "reserved_amount": str(a.reserved_amount),
                    "available_balance": str(a.available_balance),
                    "locked_until": a.locked_until,
                    "target_amount": str(a.target_amount) if a.target_amount is not None else None,
                    "target_deadline": a.target_deadline,
                    "is_active": a.is_active,
                    "created_at": a.created_at,
                }
            )
        return Response(data, status=status.HTTP_200_OK)


class CreateSavingsAccountView(APIView):
    """
    POST /api/savings/accounts/create/
    Body: { "name": "...", "account_type": "FLEXIBLE|FIXED|TARGET", "locked_until": "...", ... }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        name = (request.data.get("name") or "").strip()
        account_type = request.data.get("account_type")

        if not name:
            raise ValidationError("Account name is required.")
        if account_type not in ("FLEXIBLE", "FIXED", "TARGET"):
            raise ValidationError("Invalid account_type.")

        locked_until = request.data.get("locked_until")
        target_amount = request.data.get("target_amount")
        target_deadline = request.data.get("target_deadline")

        acct = SavingsAccount.objects.create(
            user=request.user,
            name=name,
            account_type=account_type,
            locked_until=locked_until or None,
            target_amount=target_amount or None,
            target_deadline=target_deadline or None,
            balance=Decimal("0.00"),
            reserved_amount=Decimal("0.00"),
            is_active=True,
        )

        return Response(
            {
                "id": acct.id,
                "name": acct.name,
                "account_type": acct.account_type,
                "balance": str(acct.balance),
                "reserved_amount": str(acct.reserved_amount),
                "available_balance": str(acct.available_balance),
            },
            status=status.HTTP_201_CREATED,
        )


# -----------------------------
# Deposits / Withdrawals
# -----------------------------

class DepositToSavingsView(APIView):
    """
    POST /api/savings/deposit/
    Body: { "account_id": 1, "amount": 1000, "reference": "...", "note": "..." }

    Creates a DEPOSIT transaction and increases balance.
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        account_id = request.data.get("account_id")
        amount = request.data.get("amount")
        reference = request.data.get("reference")
        note = request.data.get("note")

        if not account_id:
            raise ValidationError("account_id is required.")
        if amount is None:
            raise ValidationError("amount is required.")

        amount = q2(Decimal(str(amount)))
        if amount <= 0:
            raise ValidationError("amount must be greater than 0.")

        acct = SavingsAccount.objects.select_for_update().filter(id=account_id, user=request.user, is_active=True).first()
        if not acct:
            raise ValidationError("Savings account not found.")

        acct.balance = q2(Decimal(acct.balance) + amount)
        acct.full_clean()
        acct.save(update_fields=["balance"])

        SavingsTransaction.objects.create(
            account=acct,
            txn_type="DEPOSIT",
            amount=amount,
            reference=reference,
            note=note,
        )

        return Response(
            {
                "message": "Deposit successful.",
                "account_id": acct.id,
                "balance": str(acct.balance),
                "reserved_amount": str(acct.reserved_amount),
                "available_balance": str(acct.available_balance),
            },
            status=status.HTTP_200_OK,
        )


class CreateWithdrawRequestView(APIView):
    """
    POST /api/savings/withdraw/request/
    Body: { "account_id": 1, "amount": 500, "reason": "..." }

    Creates a WithdrawRequest (PENDING).
    Does NOT reduce balance.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        account_id = request.data.get("account_id")
        amount = request.data.get("amount")
        reason = request.data.get("reason")

        if not account_id:
            raise ValidationError("account_id is required.")
        if amount is None:
            raise ValidationError("amount is required.")

        amount = q2(Decimal(str(amount)))
        if amount <= 0:
            raise ValidationError("amount must be greater than 0.")

        acct = get_account_or_404_for_user(int(account_id), request.user)

        # enforce model-level checks too
        wr = WithdrawRequest(
            account=acct,
            requested_by=request.user,
            amount=amount,
            reason=reason,
            status="PENDING",
        )
        wr.full_clean()
        wr.save()

        return Response(
            {
                "message": "Withdraw request created.",
                "withdraw_request_id": wr.id,
                "status": wr.status,
            },
            status=status.HTTP_201_CREATED,
        )


class MyWithdrawRequestsView(generics.ListAPIView):
    """
    GET /api/savings/withdraw/requests/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, *args, **kwargs):
        qs = WithdrawRequest.objects.filter(requested_by=request.user).order_by("-created_at")
        data = []
        for w in qs:
            data.append(
                {
                    "id": w.id,
                    "account_id": w.account_id,
                    "amount": str(w.amount),
                    "status": w.status,
                    "reason": w.reason,
                    "created_at": w.created_at,
                    "reviewed_at": w.reviewed_at,
                }
            )
        return Response(data, status=status.HTTP_200_OK)


# -----------------------------
# Admin workflow
# -----------------------------

class AdminApproveWithdrawRequestView(APIView):
    """
    POST /api/savings/admin/withdraw/<id>/approve/
    Admin approves a pending withdrawal request.

    (Adjust the admin check to your role system.)
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, withdraw_id: int):
        if not request.user.is_staff:
            raise PermissionDenied("Admin only.")

        wr = WithdrawRequest.objects.select_related("account").filter(id=withdraw_id).first()
        if not wr:
            raise ValidationError("Withdraw request not found.")
        if wr.status != "PENDING":
            raise ValidationError("Only PENDING requests can be approved.")

        # Revalidate at approval time
        wr.full_clean()

        wr.status = "APPROVED"
        wr.reviewed_by = request.user
        wr.reviewed_at = timezone.now()
        wr.save(update_fields=["status", "reviewed_by", "reviewed_at"])

        return Response({"message": "Withdrawal approved."}, status=status.HTTP_200_OK)


class AdminRejectWithdrawRequestView(APIView):
    """
    POST /api/savings/admin/withdraw/<id>/reject/
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, withdraw_id: int):
        if not request.user.is_staff:
            raise PermissionDenied("Admin only.")

        wr = WithdrawRequest.objects.filter(id=withdraw_id).first()
        if not wr:
            raise ValidationError("Withdraw request not found.")
        if wr.status != "PENDING":
            raise ValidationError("Only PENDING requests can be rejected.")

        wr.status = "REJECTED"
        wr.reviewed_by = request.user
        wr.reviewed_at = timezone.now()
        wr.save(update_fields=["status", "reviewed_by", "reviewed_at"])

        return Response({"message": "Withdrawal rejected."}, status=status.HTTP_200_OK)


class AdminPayWithdrawRequestView(APIView):
    """
    POST /api/savings/admin/withdraw/<id>/pay/
    Actually pays out:
    - deduct from account.balance
    - create SavingsTransaction(WITHDRAWAL)
    - mark WithdrawRequest PAID

    NOTE: This should only happen after APPROVED.
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, withdraw_id: int):
        if not request.user.is_staff:
            raise PermissionDenied("Admin only.")

        wr = WithdrawRequest.objects.select_for_update().select_related("account").filter(id=withdraw_id).first()
        if not wr:
            raise ValidationError("Withdraw request not found.")
        if wr.status != "APPROVED":
            raise ValidationError("Only APPROVED requests can be paid.")

        acct = SavingsAccount.objects.select_for_update().get(id=wr.account_id)

        # Validate again right now (balances/reserves can change)
        if not acct.is_active:
            raise ValidationError("Savings account is not active.")
        if not acct.can_withdraw_now():
            raise ValidationError("This savings account is currently locked.")
        if wr.amount > acct.available_balance:
            raise ValidationError("Insufficient available balance (some funds may be reserved).")

        acct.balance = q2(Decimal(acct.balance) - Decimal(wr.amount))
        acct.full_clean()
        acct.save(update_fields=["balance"])

        SavingsTransaction.objects.create(
            account=acct,
            txn_type="WITHDRAWAL",
            amount=wr.amount,
            reference=f"WITHDRAW#{wr.id}",
            note=wr.reason,
        )

        wr.status = "PAID"
        wr.reviewed_by = request.user
        wr.reviewed_at = timezone.now()
        wr.save(update_fields=["status", "reviewed_by", "reviewed_at"])

        return Response(
            {
                "message": "Withdrawal paid successfully.",
                "account_id": acct.id,
                "new_balance": str(acct.balance),
                "available_balance": str(acct.available_balance),
            },
            status=status.HTTP_200_OK,
        )


# -----------------------------
# History / Statement
# -----------------------------

class SavingsAccountHistoryView(APIView):
    """
    GET /api/savings/accounts/<id>/history/
    Returns transaction history for the given account (owner only).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, account_id: int):
        acct = SavingsAccount.objects.filter(id=account_id, user=request.user).first()
        if not acct:
            raise ValidationError("Savings account not found.")

        txns = SavingsTransaction.objects.filter(account=acct).order_by("-created_at")[:200]

        data = {
            "account": {
                "id": acct.id,
                "name": acct.name,
                "account_type": acct.account_type,
                "balance": str(acct.balance),
                "reserved_amount": str(acct.reserved_amount),
                "available_balance": str(acct.available_balance),
            },
            "transactions": [
                {
                    "id": t.id,
                    "txn_type": t.txn_type,
                    "amount": str(t.amount),
                    "reference": t.reference,
                    "note": t.note,
                    "created_at": t.created_at,
                }
                for t in txns
            ],
        }
        return Response(data, status=status.HTTP_200_OK)