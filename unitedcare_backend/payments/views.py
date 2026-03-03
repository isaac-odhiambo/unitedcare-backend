# payments/views.py
from decimal import Decimal
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError, PermissionDenied

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

# ✅ Throttling (STK spam protection)
from .throttles import StkPushUserThrottle, StkPushPhoneThrottle

logger = logging.getLogger(__name__)

# =========================================================
# Security helpers
# =========================================================
def _require_callback_token(request) -> None:
    """
    If MPESA_CALLBACK_TOKEN is set, require it on callback URLs as ?token=...
    """
    token = getattr(settings, "MPESA_CALLBACK_TOKEN", "")
    if not token:
        return  # dev: allow

    provided = (request.query_params.get("token") or "").strip()
    if provided != token:
        raise PermissionDenied("Invalid callback token")


def _get_client_ip(request) -> str:
    """
    Prefer X-Forwarded-For when behind proxy/load balancer.
    """
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        # first IP in the chain is the original client
        return xff.split(",")[0].strip()
    return (request.META.get("REMOTE_ADDR") or "").strip()


def _require_safaricom_ip(request) -> None:
    """
    Strong protection: block fake callbacks.
    In production: set MPESA_CALLBACK_IP_ALLOWLIST = ["1.2.3.4", ...]
    If not set, we allow (dev mode).
    """
    allowlist = getattr(settings, "MPESA_CALLBACK_IP_ALLOWLIST", None)
    if not allowlist:
        return  # dev: allow

    ip = _get_client_ip(request)
    if ip not in set(allowlist):
        raise PermissionDenied("Callback IP not allowed")


def _accepted_callback_response():
    """
    Safer callback behavior:
    - Always respond Accepted to reduce retries and avoid leaking errors.
    """
    return Response({"ResultCode": 0, "ResultDesc": "Accepted"}, status=status.HTTP_200_OK)


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


# ✅ Service names
initiate_stk_push = _svc("initiate_stk_push")
handle_stk_callback = _svc("handle_stk_callback")

approve_withdrawal_request = _svc("approve_withdrawal_request")
initiate_b2c_payout_for_withdrawal = _svc("initiate_b2c_payout_for_withdrawal")

handle_b2c_result_callback = _svc("handle_b2c_result_callback")
handle_b2c_timeout_callback = _svc("handle_b2c_timeout_callback")


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
        return (
            WithdrawalRequest.objects
            .filter(user=self.request.user)
            .select_related("mpesa_tx")
            .order_by("-id")
        )


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
        qs = (
            WithdrawalRequest.objects
            .select_related("user", "approved_by", "rejected_by", "mpesa_tx")
            .order_by("-id")
        )

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

        if approve_withdrawal_request and initiate_b2c_payout_for_withdrawal:
            # 1) approve
            approve_withdrawal_request(withdrawal_id=w.id, approved_by=request.user)
            # 2) payout (services will balance-check again before payout)
            tx = initiate_b2c_payout_for_withdrawal(withdrawal_id=w.id)

            w.refresh_from_db()
            return Response(
                {
                    "message": "Withdrawal approved. Payout initiated.",
                    "withdrawal": WithdrawalSerializer(w).data,
                    "mpesa_tx": MpesaTransactionSerializer(tx).data,
                },
                status=status.HTTP_200_OK,
            )

        # Fallback (dev only)
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
    Member: list my ledger entries
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
    Admin: view mpesa tx
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
    body: { phone, amount, purpose, reference?, narration? }

    ✅ Security:
    - Throttling per user + per phone to prevent STK spam
    - Allow only known purposes
    """
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [StkPushUserThrottle, StkPushPhoneThrottle]

    ALLOWED_PURPOSES = {
        "SAVINGS_DEPOSIT",
        "MERRY_CONTRIBUTION",
        "GROUP_CONTRIBUTION",
        "LOAN_REPAYMENT",
        "OTHER",
    }

    @transaction.atomic
    def post(self, request):
        phone = (request.data.get("phone") or "").strip()
        amount = request.data.get("amount")
        purpose = (request.data.get("purpose") or "SAVINGS_DEPOSIT").strip().upper()
        reference = (request.data.get("reference") or "").strip()
        narration = (request.data.get("narration") or "").strip()

        if not phone:
            raise ValidationError({"phone": "Phone is required."})

        try:
            amt = Decimal(str(amount or "0"))
        except Exception:
            amt = Decimal("0")

        if amt <= 0:
            raise ValidationError({"amount": "Amount must be greater than 0."})

        if purpose not in self.ALLOWED_PURPOSES:
            raise ValidationError({"purpose": f"Invalid purpose. Use one of: {sorted(self.ALLOWED_PURPOSES)}"})

        if initiate_stk_push:
            tx = initiate_stk_push(
                user=request.user,
                phone=phone,
                amount=amt,
                purpose=purpose,
                reference=reference,
                narration=narration,
                target_object=None,
            )
            return Response(
                {"message": "STK push initiated.", "tx": MpesaTransactionSerializer(tx).data},
                status=status.HTTP_200_OK,
            )

        # Fallback (dev only)
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
    POST /payments/mpesa/stk/callback/?token=...

    ✅ Security:
    - Requires callback token (if set)
    - Requires callback IP allowlist (if set)
    - Calls service which does STK Query verification before credit
    - Always returns Accepted (no info leak)
    """
    permission_classes = [permissions.AllowAny]

    @transaction.atomic
    def post(self, request):
        try:
            _require_callback_token(request)
            _require_safaricom_ip(request)
            if handle_stk_callback:
                handle_stk_callback(callback_payload=request.data)
        except Exception as e:
            logger.exception("STK callback handling error: %s", str(e))
        return _accepted_callback_response()


class MpesaB2CResultView(APIView):
    """
    B2C result callback (withdrawals payout)
    POST /payments/mpesa/b2c/result/?token=...

    ✅ Security:
    - Requires callback token (if set)
    - Requires callback IP allowlist (if set)
    - Always returns Accepted (no info leak)
    """
    permission_classes = [permissions.AllowAny]

    @transaction.atomic
    def post(self, request):
        try:
            _require_callback_token(request)
            _require_safaricom_ip(request)
            if handle_b2c_result_callback:
                handle_b2c_result_callback(callback_payload=request.data)
        except Exception as e:
            logger.exception("B2C result callback handling error: %s", str(e))
        return _accepted_callback_response()


class MpesaB2CTimeoutView(APIView):
    """
    B2C timeout callback
    POST /payments/mpesa/b2c/timeout/?token=...

    ✅ Security:
    - Requires callback token (if set)
    - Requires callback IP allowlist (if set)
    - Always returns Accepted (no info leak)
    """
    permission_classes = [permissions.AllowAny]

    @transaction.atomic
    def post(self, request):
        try:
            _require_callback_token(request)
            _require_safaricom_ip(request)
            if handle_b2c_timeout_callback:
                handle_b2c_timeout_callback(callback_payload=request.data)
        except Exception as e:
            logger.exception("B2C timeout callback handling error: %s", str(e))
        return _accepted_callback_response()