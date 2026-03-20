from decimal import Decimal
import logging
import re

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from rest_framework import generics, permissions, status
from rest_framework.exceptions import PermissionDenied, ValidationError, NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    MpesaConfig,
    MpesaTransaction,
    PaymentLedger,
    TransactionFeeConfig,
    WithdrawalRequest,
)
from .permissions import IsAdmin
from .serializers import (
    MpesaConfigSerializer,
    MpesaTransactionSerializer,
    PaymentLedgerSerializer,
    TransactionFeeConfigSerializer,
    WithdrawalApproveSerializer,
    WithdrawalCreateSerializer,
    WithdrawalRejectSerializer,
    WithdrawalSerializer,
)
from .throttles import StkPushPhoneThrottle, StkPushUserThrottle

logger = logging.getLogger(__name__)


# =========================================================
# Reference helpers
# =========================================================
def _extract_id(reference: str, prefix: str):
    ref = (reference or "").strip()
    if not ref.startswith(prefix):
        return None
    try:
        return int(ref.replace(prefix, "").strip())
    except Exception:
        return None


def _normalize_reference_token(reference: str) -> str:
    ref = (reference or "").strip().upper()
    ref = ref.replace(" ", "").replace("-", "").replace("_", "")
    return ref


def _parse_reference(reference: str):
    """
    Supports simple references:
      - mus12        => merry id 12
      - saving23     => savings
      - sav23        => savings
      - loan35       => loan
      - grp9         => group
      - group9       => group

    Also supports legacy references:
      - MERRY-PAYMENT-99
      - LOAN-12
      - GROUP-7
    """
    raw = (reference or "").strip()
    norm = _normalize_reference_token(raw)

    if not raw:
        return {
            "raw": raw,
            "normalized": norm,
            "kind": "EMPTY",
            "entity_id": None,
            "purpose": "SAVINGS_DEPOSIT",
            "valid": False,
        }

    m = re.match(r"^MUS(\d+)$", norm)
    if m:
        return {
            "raw": raw,
            "normalized": norm,
            "kind": "MERRY",
            "entity_id": int(m.group(1)),
            "purpose": "MERRY_CONTRIBUTION",
            "valid": True,
        }

    m = re.match(r"^SAVING(\d+)$", norm)
    if m:
        return {
            "raw": raw,
            "normalized": norm,
            "kind": "SAVINGS_ACCOUNT",
            "entity_id": int(m.group(1)),
            "purpose": "SAVINGS_DEPOSIT",
            "valid": True,
        }

    m = re.match(r"^SAV(\d+)$", norm)
    if m:
        return {
            "raw": raw,
            "normalized": norm,
            "kind": "SAVINGS_ACCOUNT",
            "entity_id": int(m.group(1)),
            "purpose": "SAVINGS_DEPOSIT",
            "valid": True,
        }

    m = re.match(r"^LOAN(\d+)$", norm)
    if m:
        return {
            "raw": raw,
            "normalized": norm,
            "kind": "LOAN",
            "entity_id": int(m.group(1)),
            "purpose": "LOAN_REPAYMENT",
            "valid": True,
        }

    m = re.match(r"^GRP(\d+)$", norm)
    if m:
        return {
            "raw": raw,
            "normalized": norm,
            "kind": "GROUP",
            "entity_id": int(m.group(1)),
            "purpose": "GROUP_CONTRIBUTION",
            "valid": True,
        }

    m = re.match(r"^GROUP(\d+)$", norm)
    if m:
        return {
            "raw": raw,
            "normalized": norm,
            "kind": "GROUP",
            "entity_id": int(m.group(1)),
            "purpose": "GROUP_CONTRIBUTION",
            "valid": True,
        }

    # Legacy
    m = re.match(r"^MERRYPAYMENT(\d+)$", norm)
    if m:
        return {
            "raw": raw,
            "normalized": norm,
            "kind": "MERRY_PAYMENT",
            "entity_id": int(m.group(1)),
            "purpose": "MERRY_CONTRIBUTION",
            "valid": True,
        }

    m = re.match(r"^LOAN(\d+)$", norm)
    if m:
        return {
            "raw": raw,
            "normalized": norm,
            "kind": "LOAN",
            "entity_id": int(m.group(1)),
            "purpose": "LOAN_REPAYMENT",
            "valid": True,
        }

    m = re.match(r"^GROUP(\d+)$", norm)
    if m:
        return {
            "raw": raw,
            "normalized": norm,
            "kind": "GROUP",
            "entity_id": int(m.group(1)),
            "purpose": "GROUP_CONTRIBUTION",
            "valid": True,
        }

    return {
        "raw": raw,
        "normalized": norm,
        "kind": "UNKNOWN",
        "entity_id": None,
        "purpose": "OTHER",
        "valid": False,
    }


def _require_reference_format(purpose: str, reference: str) -> None:
    """
    Validate reference format early before calling services.

    Allowed now:
      - MERRY_CONTRIBUTION => mus12 OR MERRY-PAYMENT-99
      - LOAN_REPAYMENT     => loan35 OR LOAN-35
      - GROUP_CONTRIBUTION => grp9 OR GROUP-9
      - SAVINGS_DEPOSIT    => optional; can be blank, saving23, sav23
    """
    p = (purpose or "").upper()
    ref = (reference or "").strip()

    if p == "SAVINGS_DEPOSIT":
        if not ref:
            return
        parsed = _parse_reference(ref)
        if not parsed["valid"] or parsed["purpose"] != "SAVINGS_DEPOSIT":
            raise ValidationError(
                {"reference": "For SAVINGS_DEPOSIT, reference can be blank or like 'saving23' / 'sav23'."}
            )
        return

    if p == "MERRY_CONTRIBUTION":
        parsed = _parse_reference(ref)
        if parsed["valid"] and parsed["purpose"] == "MERRY_CONTRIBUTION":
            return
        if _extract_id(ref, "MERRY-PAYMENT-") is None:
            raise ValidationError(
                {"reference": "For MERRY_CONTRIBUTION, use reference like 'mus12'."}
            )

    if p == "LOAN_REPAYMENT":
        parsed = _parse_reference(ref)
        if parsed["valid"] and parsed["purpose"] == "LOAN_REPAYMENT":
            return
        if _extract_id(ref, "LOAN-") is None:
            raise ValidationError(
                {"reference": "For LOAN_REPAYMENT, use reference like 'loan35'."}
            )

    if p == "GROUP_CONTRIBUTION":
        parsed = _parse_reference(ref)
        if parsed["valid"] and parsed["purpose"] == "GROUP_CONTRIBUTION":
            return
        if _extract_id(ref, "GROUP-") is None:
            raise ValidationError(
                {"reference": "For GROUP_CONTRIBUTION, use reference like 'grp9'."}
            )


# =========================================================
# Callback security helpers
# =========================================================
def _require_callback_token(request) -> None:
    token = getattr(settings, "MPESA_CALLBACK_TOKEN", "")
    if not token:
        return

    provided = (request.query_params.get("token") or "").strip()
    if provided != token:
        raise PermissionDenied("Invalid callback token")


def _get_client_ip(request) -> str:
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return (request.META.get("REMOTE_ADDR") or "").strip()


def _require_safaricom_ip(request) -> None:
    allowlist = getattr(settings, "MPESA_CALLBACK_IP_ALLOWLIST", None)
    if not allowlist:
        return

    ip = _get_client_ip(request)
    if ip not in set(allowlist):
        raise PermissionDenied("Callback IP not allowed")


def _accepted_callback_response():
    return Response({"ResultCode": 0, "ResultDesc": "Accepted"}, status=status.HTTP_200_OK)


# =========================================================
# Optional services wiring
# =========================================================
def _svc(name: str):
    try:
        from . import services
        return getattr(services, name, None)
    except Exception:
        return None


# MPESA / withdrawal services
initiate_stk_push = _svc("initiate_stk_push")
handle_stk_callback = _svc("handle_stk_callback")

# C2B services
handle_c2b_validation_callback = _svc("handle_c2b_validation_callback")
handle_c2b_confirmation_callback = _svc("handle_c2b_confirmation_callback")

approve_withdrawal_request = _svc("approve_withdrawal_request")
initiate_b2c_payout_for_withdrawal = _svc("initiate_b2c_payout_for_withdrawal")

handle_b2c_result_callback = _svc("handle_b2c_result_callback")
handle_b2c_timeout_callback = _svc("handle_b2c_timeout_callback")


# =========================================================
# Mpesa Config
# =========================================================
class ActiveMpesaConfigView(APIView):
    """
    Authenticated users: get active Mpesa config for frontend display
    GET /payments/mpesa-config/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        obj = (
            MpesaConfig.objects.filter(is_active=True)
            .order_by("-updated_at", "-id")
            .first()
        )
        if not obj:
            raise NotFound("No active Mpesa config found.")
        return Response(MpesaConfigSerializer(obj).data, status=status.HTTP_200_OK)


class AdminMpesaConfigListCreateView(generics.ListCreateAPIView):
    """
    Admin: list/create mpesa configs
    GET  /payments/mpesa-config/admin/
    POST /payments/mpesa-config/admin/
    """
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    serializer_class = MpesaConfigSerializer
    queryset = MpesaConfig.objects.all().order_by("-updated_at", "-id")


class AdminMpesaConfigDetailView(generics.RetrieveUpdateAPIView):
    """
    Admin: retrieve/update a single mpesa config
    GET   /payments/mpesa-config/admin/<pk>/
    PATCH /payments/mpesa-config/admin/<pk>/
    PUT   /payments/mpesa-config/admin/<pk>/
    """
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    serializer_class = MpesaConfigSerializer
    queryset = MpesaConfig.objects.all()


# =========================================================
# Fee config (Admin)
# =========================================================
class AdminFeeConfigListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    serializer_class = TransactionFeeConfigSerializer
    queryset = TransactionFeeConfig.objects.all().order_by("purpose")


class AdminFeeConfigDetailView(generics.RetrieveUpdateAPIView):
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    serializer_class = TransactionFeeConfigSerializer
    queryset = TransactionFeeConfig.objects.all()


# =========================================================
# Withdrawal (Member)
# =========================================================
class MyWithdrawalsView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = WithdrawalSerializer

    def get_queryset(self):
        return (
            WithdrawalRequest.objects.filter(user=self.request.user)
            .select_related("mpesa_tx")
            .order_by("-id")
        )


class RequestWithdrawalView(generics.CreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = WithdrawalCreateSerializer

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)

        withdrawal = ser.save()

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
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    serializer_class = WithdrawalSerializer

    def get_queryset(self):
        qs = (
            WithdrawalRequest.objects.select_related(
                "user", "approved_by", "rejected_by", "mpesa_tx"
            )
            .order_by("-id")
        )

        st = self.request.query_params.get("status")
        if st:
            qs = qs.filter(status=st.upper())

        return qs


class ApproveWithdrawalView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    @transaction.atomic
    def patch(self, request, pk: int):
        ser = WithdrawalApproveSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        try:
            w = (
                WithdrawalRequest.objects.select_for_update()
                .select_related("user")
                .get(id=pk)
            )
        except WithdrawalRequest.DoesNotExist:
            raise ValidationError("Withdrawal request not found.")

        if w.status != "PENDING":
            raise ValidationError("Only PENDING withdrawals can be approved.")

        if approve_withdrawal_request and initiate_b2c_payout_for_withdrawal:
            approve_withdrawal_request(withdrawal_id=w.id, approved_by=request.user)
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

        w.status = "APPROVED"
        w.approved_by = request.user
        w.approved_at = timezone.now()
        w.save(update_fields=["status", "approved_by", "approved_at"])

        return Response(
            {
                "message": "Withdrawal approved. (B2C payout not wired yet)",
                "withdrawal": WithdrawalSerializer(w).data,
            },
            status=status.HTTP_200_OK,
        )


class RejectWithdrawalView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    @transaction.atomic
    def patch(self, request, pk: int):
        ser = WithdrawalRejectSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        try:
            w = (
                WithdrawalRequest.objects.select_for_update()
                .select_related("user")
                .get(id=pk)
            )
        except WithdrawalRequest.DoesNotExist:
            raise ValidationError("Withdrawal request not found.")

        if w.status != "PENDING":
            raise ValidationError("Only PENDING withdrawals can be rejected.")

        w.status = "REJECTED"
        w.rejected_by = request.user
        w.rejected_at = timezone.now()
        w.rejection_reason = ser.validated_data.get("rejection_reason", "") or ""
        w.save(
            update_fields=[
                "status",
                "rejected_by",
                "rejected_at",
                "rejection_reason",
            ]
        )

        return Response(
            {
                "message": "Withdrawal rejected.",
                "withdrawal": WithdrawalSerializer(w).data,
            },
            status=status.HTTP_200_OK,
        )


# =========================================================
# Ledger / History
# =========================================================
class MyLedgerHistoryView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = PaymentLedgerSerializer

    def get_queryset(self):
        return (
            PaymentLedger.objects.filter(user=self.request.user)
            .select_related("mpesa_tx")
            .order_by("-id")
        )


class AdminLedgerHistoryView(generics.ListAPIView):
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

        allocation_status = self.request.query_params.get("allocation_status")
        if allocation_status and hasattr(MpesaTransaction, "allocation_status"):
            qs = qs.filter(allocation_status=allocation_status.upper())

        channel = self.request.query_params.get("channel")
        if channel:
            qs = qs.filter(channel=channel.upper())

        return qs


# =========================================================
# Mpesa endpoints
# =========================================================
class MpesaStkPushView(APIView):
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
            raise ValidationError(
                {"purpose": f"Invalid purpose. Use one of: {sorted(self.ALLOWED_PURPOSES)}"}
            )

        _require_reference_format(purpose, reference)

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
                {
                    "message": "STK push initiated.",
                    "tx": MpesaTransactionSerializer(tx).data,
                },
                status=status.HTTP_200_OK,
            )

        tx = MpesaTransaction.objects.create(
            user=request.user,
            phone=phone,
            amount=amt,
            base_amount=amt,
            transaction_fee=Decimal("0.00"),
            direction="IN",
            channel="STK",
            purpose=purpose,
            status="INITIATED",
            reference=reference,
            request_payload=request.data,
        )

        return Response(
            {
                "message": "STK push stored (Safaricom call not wired yet).",
                "tx": MpesaTransactionSerializer(tx).data,
            },
            status=status.HTTP_200_OK,
        )


class MpesaStkCallbackView(APIView):
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


class MpesaC2BValidationView(APIView):
    permission_classes = [permissions.AllowAny]

    @transaction.atomic
    def post(self, request):
        try:
            _require_callback_token(request)
            _require_safaricom_ip(request)

            if handle_c2b_validation_callback:
                result = handle_c2b_validation_callback(callback_payload=request.data)
                if isinstance(result, dict):
                    return Response(result, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception("C2B validation handling error: %s", str(e))
            return Response(
                {"ResultCode": "C2B00011", "ResultDesc": "Rejected"},
                status=status.HTTP_200_OK,
            )

        return Response(
            {"ResultCode": "0", "ResultDesc": "Accepted"},
            status=status.HTTP_200_OK,
        )


class MpesaC2BConfirmationView(APIView):
    permission_classes = [permissions.AllowAny]

    @transaction.atomic
    def post(self, request):
        try:
            _require_callback_token(request)
            _require_safaricom_ip(request)

            if handle_c2b_confirmation_callback:
                handle_c2b_confirmation_callback(callback_payload=request.data)

        except Exception as e:
            logger.exception("C2B confirmation handling error: %s", str(e))

        return _accepted_callback_response()


class MpesaB2CResultView(APIView):
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