from decimal import Decimal
from django.db import transaction
from django.utils import timezone

from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError

from .models import WithdrawalRequest, PaymentLedger, MpesaTransaction
from .serializers import (
    WithdrawalCreateSerializer,
    WithdrawalSerializer,
    WithdrawalApproveSerializer,
    WithdrawalRejectSerializer,
    PaymentLedgerSerializer,
    MpesaTransactionSerializer,
)

from .permissions import IsAdmin


# =========================================================
# Optional services wiring (safe import)
# =========================================================

def _svc(name: str):
    """
    Import a function from payments/services.py if it exists.
    If not found, return None (views will fallback gracefully).
    """
    try:
        from . import services
        return getattr(services, name, None)
    except Exception:
        return None


initiate_stk_push = _svc("initiate_stk_push")
handle_stk_callback = _svc("handle_stk_callback")

approve_withdrawal_and_start_payout = _svc("approve_withdrawal_and_start_payout")
handle_b2c_result = _svc("handle_b2c_result")
handle_b2c_timeout = _svc("handle_b2c_timeout")


# =========================================================
# Withdrawal (Member)
# =========================================================

class MyWithdrawalsView(generics.ListAPIView):
    """
    Member: list my withdrawal requests
    GET /payments/withdrawals/my/
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = WithdrawalSerializer

    def get_queryset(self):
        return WithdrawalRequest.objects.filter(user=self.request.user).order_by("-id")


class RequestWithdrawalView(generics.CreateAPIView):
    """
    Member: create withdrawal request
    POST /payments/withdrawals/request/
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = WithdrawalCreateSerializer

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)

        withdrawal = ser.save()  # serializer sets user

        return Response(
            {
                "message": "Withdrawal request submitted. Awaiting admin approval.",
                "withdrawal": WithdrawalSerializer(withdrawal).data,
            },
            status=status.HTTP_201_CREATED,
        )


# =========================================================
# Withdrawal (Admin)
# =========================================================

class AdminWithdrawalsView(generics.ListAPIView):
    """
    Admin: list all withdrawals
    GET /payments/withdrawals/admin/
    """
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    serializer_class = WithdrawalSerializer

    def get_queryset(self):
        qs = WithdrawalRequest.objects.select_related(
            "user", "approved_by", "rejected_by"
        ).order_by("-id")

        st = self.request.query_params.get("status")
        if st:
            qs = qs.filter(status=st.upper())

        return qs


class ApproveWithdrawalView(APIView):
    """
    Admin: approve a withdrawal request and start payout (B2C) via services
    PATCH /payments/withdrawals/<id>/approve/
    """
    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    @transaction.atomic
    def patch(self, request, pk: int):
        ser = WithdrawalApproveSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        try:
            w = WithdrawalRequest.objects.select_for_update().select_related("user").get(id=pk)
        except WithdrawalRequest.DoesNotExist:
            raise ValidationError("Withdrawal request not found.")

        if w.status != "PENDING":
            raise ValidationError("Only PENDING withdrawals can be approved.")

        # If services exist, let services own the full workflow:
        # - mark approved
        # - create OUT Mpesa tx
        # - call B2C
        # - set PROCESSING
        if approve_withdrawal_and_start_payout:
            w = approve_withdrawal_and_start_payout(withdrawal=w, admin_user=request.user, data=ser.validated_data)
            return Response(
                {"message": "Withdrawal approved. Payout initiated.", "withdrawal": WithdrawalSerializer(w).data},
                status=status.HTTP_200_OK,
            )

        # Fallback (keeps your previous behavior)
        w.status = "APPROVED"
        w.approved_by = request.user
        w.approved_at = timezone.now()
        w.save(update_fields=["status", "approved_by", "approved_at"])

        return Response(
            {"message": "Withdrawal approved. (B2C payout not wired yet)", "withdrawal": WithdrawalSerializer(w).data},
            status=status.HTTP_200_OK,
        )


class RejectWithdrawalView(APIView):
    """
    Admin: reject withdrawal
    PATCH /payments/withdrawals/<id>/reject/
    """
    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    @transaction.atomic
    def patch(self, request, pk: int):
        ser = WithdrawalRejectSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        try:
            w = WithdrawalRequest.objects.select_for_update().select_related("user").get(id=pk)
        except WithdrawalRequest.DoesNotExist:
            raise ValidationError("Withdrawal request not found.")

        if w.status != "PENDING":
            raise ValidationError("Only PENDING withdrawals can be rejected.")

        w.status = "REJECTED"
        w.rejected_by = request.user
        w.rejected_at = timezone.now()
        w.rejection_reason = ser.validated_data.get("rejection_reason", "") or ""
        w.save(update_fields=["status", "rejected_by", "rejected_at", "rejection_reason"])

        return Response(
            {"message": "Withdrawal rejected.", "withdrawal": WithdrawalSerializer(w).data},
            status=status.HTTP_200_OK,
        )


# =========================================================
# Ledger / History
# =========================================================

class MyLedgerHistoryView(generics.ListAPIView):
    """
    Member: list my ledger entries (history for savings, loans, merry, etc.)
    GET /payments/ledger/my/
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = PaymentLedgerSerializer

    def get_queryset(self):
        return (
            PaymentLedger.objects
            .filter(user=self.request.user)
            .select_related("mpesa_tx")
            .order_by("-id")
        )


class AdminLedgerHistoryView(generics.ListAPIView):
    """
    Admin: list all ledger entries
    GET /payments/ledger/admin/
    """
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    serializer_class = PaymentLedgerSerializer

    def get_queryset(self):
        qs = PaymentLedger.objects.select_related("user", "mpesa_tx").order_by("-id")

        user_id = self.request.query_params.get("user")
        if user_id:
            qs = qs.filter(user_id=user_id)

        category = self.request.query_params.get("category")
        if category:
            qs = qs.filter(category=category.upper())

        return qs


# =========================================================
# Mpesa Debug / Admin list
# =========================================================

class AdminMpesaTransactionsView(generics.ListAPIView):
    """
    Admin: view mpesa tx (optional)
    GET /payments/mpesa/admin/
    """
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    serializer_class = MpesaTransactionSerializer

    def get_queryset(self):
        qs = MpesaTransaction.objects.select_related("user").order_by("-id")

        st = self.request.query_params.get("status")
        if st:
            qs = qs.filter(status=st.upper())

        purpose = self.request.query_params.get("purpose")
        if purpose:
            qs = qs.filter(purpose=purpose.upper())

        return qs


# =========================================================
# Mpesa endpoints (hooks)
# =========================================================

class MpesaStkPushView(APIView):
    """
    Start STK push for deposits (savings, contributions, loan repayments, merry, etc.)

    POST /payments/mpesa/stk-push/
    body: { phone, amount, purpose, reference? }
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        phone = (request.data.get("phone") or "").strip()
        amount = request.data.get("amount")
        purpose = (request.data.get("purpose") or "SAVINGS_DEPOSIT").strip().upper()
        reference = (request.data.get("reference") or "").strip()

        if not phone:
            raise ValidationError({"phone": "Phone is required."})

        amt = Decimal(str(amount or "0"))
        if amt <= 0:
            raise ValidationError({"amount": "Amount must be greater than 0."})

        # ✅ If services exist: actually call Safaricom + store CheckoutRequestID etc.
        if initiate_stk_push:
            tx = initiate_stk_push(
                user=request.user,
                phone=phone,
                amount=amt,
                purpose=purpose,
                reference=reference,
                raw_request=request.data,
            )
            return Response(
                {"message": "STK push initiated.", "tx": MpesaTransactionSerializer(tx).data},
                status=status.HTTP_200_OK,
            )

        # Fallback: store as INITIATED only (your old behavior)
        tx = MpesaTransaction.objects.create(
            user=request.user,
            phone=phone,
            amount=amt,
            direction="IN",
            channel="STK",
            purpose=purpose,
            status="INITIATED",
            reference=reference,
            request_payload=request.data,
        )

        return Response(
            {"message": "STK push stored (Safaricom call not wired yet).", "tx": MpesaTransactionSerializer(tx).data},
            status=status.HTTP_200_OK,
        )


class MpesaStkCallbackView(APIView):
    """
    Callback from Safaricom for STK push
    POST /payments/mpesa/stk/callback/
    """
    permission_classes = [permissions.AllowAny]

    @transaction.atomic
    def post(self, request):
        data = request.data

        # ✅ If services exist: do full confirmation + ledger posting + idempotency
        if handle_stk_callback:
            handle_stk_callback(data)
            return Response({"ResultCode": 0, "ResultDesc": "Accepted"}, status=status.HTTP_200_OK)

        # Fallback: store payload only (your old behavior)
        checkout_id = None
        try:
            checkout_id = data["Body"]["stkCallback"]["CheckoutRequestID"]
        except Exception:
            checkout_id = None

        if checkout_id:
            tx = MpesaTransaction.objects.select_for_update().filter(checkout_request_id=checkout_id).first()
            if tx:
                tx.callback_payload = data
                tx.updated_at = timezone.now()
                tx.save(update_fields=["callback_payload", "updated_at"])

        return Response({"ResultCode": 0, "ResultDesc": "Accepted"}, status=status.HTTP_200_OK)


class MpesaB2CResultView(APIView):
    """
    B2C result callback (withdrawals payout)
    POST /payments/mpesa/b2c/result/
    """
    permission_classes = [permissions.AllowAny]

    @transaction.atomic
    def post(self, request):
        data = request.data

        if handle_b2c_result:
            handle_b2c_result(data)
            return Response({"ResultCode": 0, "ResultDesc": "Accepted"}, status=status.HTTP_200_OK)

        conversation_id = data.get("Result", {}).get("ConversationID")
        if conversation_id:
            tx = MpesaTransaction.objects.select_for_update().filter(conversation_id=conversation_id).first()
            if tx:
                tx.callback_payload = data
                tx.updated_at = timezone.now()
                tx.save(update_fields=["callback_payload", "updated_at"])

        return Response({"ResultCode": 0, "ResultDesc": "Accepted"}, status=status.HTTP_200_OK)


class MpesaB2CTimeoutView(APIView):
    """
    B2C timeout callback
    POST /payments/mpesa/b2c/timeout/
    """
    permission_classes = [permissions.AllowAny]

    @transaction.atomic
    def post(self, request):
        data = request.data

        if handle_b2c_timeout:
            handle_b2c_timeout(data)
            return Response({"ResultCode": 0, "ResultDesc": "Accepted"}, status=status.HTTP_200_OK)

        conversation_id = data.get("Result", {}).get("ConversationID") or data.get("ConversationID")
        if conversation_id:
            tx = MpesaTransaction.objects.select_for_update().filter(conversation_id=conversation_id).first()
            if tx:
                tx.callback_payload = data
                tx.status = "TIMEOUT"
                tx.updated_at = timezone.now()
                tx.save(update_fields=["callback_payload", "status", "updated_at"])

        return Response({"ResultCode": 0, "ResultDesc": "Accepted"}, status=status.HTTP_200_OK)