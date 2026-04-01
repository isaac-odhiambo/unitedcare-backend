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
from .services import (
    get_active_mpesa_config,
    initiate_stk_push,
    handle_stk_callback,
    handle_c2b_validation_callback,
    handle_c2b_confirmation_callback,
    create_withdrawal_request,
    approve_withdrawal_request,
    initiate_b2c_payout_for_withdrawal,
    handle_b2c_result_callback,
    handle_b2c_timeout_callback,
)

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
    Supported primary references:
      - mus11        => merry for USER id 11
      - saving23     => savings for USER id 23
      - sav23        => savings for USER id 23
      - loan35       => loan for USER/loan ref 35
      - grp9         => group 9
      - group9       => group 9

    Legacy supported:
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
            "purpose": "OTHER",
            "valid": False,
            "matched_reference_type": "UNKNOWN",
        }

    m = re.match(r"^MUS(\d+)$", norm)
    if m:
        return {
            "raw": raw,
            "normalized": norm,
            "kind": "MERRY_USER",
            "entity_id": int(m.group(1)),
            "purpose": "MERRY_CONTRIBUTION",
            "valid": True,
            "matched_reference_type": "MERRY",
        }

    m = re.match(r"^SAVING(\d+)$", norm)
    if m:
        return {
            "raw": raw,
            "normalized": norm,
            "kind": "SAVINGS_USER",
            "entity_id": int(m.group(1)),
            "purpose": "SAVINGS_DEPOSIT",
            "valid": True,
            "matched_reference_type": "SAVINGS",
        }

    m = re.match(r"^SAV(\d+)$", norm)
    if m:
        return {
            "raw": raw,
            "normalized": norm,
            "kind": "SAVINGS_USER",
            "entity_id": int(m.group(1)),
            "purpose": "SAVINGS_DEPOSIT",
            "valid": True,
            "matched_reference_type": "SAVINGS",
        }

    m = re.match(r"^LOAN(\d+)$", norm)
    if m:
        return {
            "raw": raw,
            "normalized": norm,
            "kind": "LOAN_USER",
            "entity_id": int(m.group(1)),
            "purpose": "LOAN_REPAYMENT",
            "valid": True,
            "matched_reference_type": "LOAN",
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
            "matched_reference_type": "GROUP",
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
            "matched_reference_type": "GROUP",
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
            "matched_reference_type": "MERRY",
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
            "matched_reference_type": "LOAN",
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
            "matched_reference_type": "GROUP",
        }

    return {
        "raw": raw,
        "normalized": norm,
        "kind": "UNKNOWN",
        "entity_id": None,
        "purpose": "OTHER",
        "valid": False,
        "matched_reference_type": "UNKNOWN",
    }


def _require_reference_format(purpose: str, reference: str) -> None:
    """
    Validate reference format before calling services.
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
                {"reference": "For MERRY_CONTRIBUTION, use reference like 'mus11'."}
            )
        return

    if p == "LOAN_REPAYMENT":
        parsed = _parse_reference(ref)
        if parsed["valid"] and parsed["purpose"] == "LOAN_REPAYMENT":
            return
        if _extract_id(ref, "LOAN-") is None:
            raise ValidationError(
                {"reference": "For LOAN_REPAYMENT, use reference like 'loan35'."}
            )
        return

    if p == "GROUP_CONTRIBUTION":
        parsed = _parse_reference(ref)
        if parsed["valid"] and parsed["purpose"] == "GROUP_CONTRIBUTION":
            return
        if _extract_id(ref, "GROUP-") is None:
            raise ValidationError(
                {"reference": "For GROUP_CONTRIBUTION, use reference like 'grp9'."}
            )
        return


# =========================================================
# Callback security helpers
# =========================================================
def _require_callback_token(request) -> None:
    token = getattr(settings, "MPESA_CALLBACK_TOKEN", "")
    if not token:
        return

    provided = (
        request.query_params.get("token")
        or request.headers.get("X-Callback-Token")
        or request.headers.get("X-MPESA-CALLBACK-TOKEN")
        or ""
    )
    if provided != token:
        raise PermissionDenied("Invalid callback token")


def _require_safaricom_ip(request) -> None:
    allowed_ips = getattr(settings, "MPESA_ALLOWED_IPS", None)
    if not allowed_ips:
        return

    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    remote_addr = request.META.get("REMOTE_ADDR", "")

    client_ip = forwarded.split(",")[0].strip() if forwarded else remote_addr
    if client_ip not in allowed_ips:
        raise PermissionDenied("Callback IP not allowed")


def _accepted_callback_response():
    return Response({"ResultCode": 0, "ResultDesc": "Accepted"})


# =========================================================
# Mpesa Config Views
# =========================================================
class ActiveMpesaConfigView(generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = MpesaConfigSerializer

    def get_object(self):
        obj = get_active_mpesa_config()
        if not obj:
            raise NotFound("No active M-Pesa configuration found.")
        return obj


class AdminMpesaConfigListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    queryset = MpesaConfig.objects.all().order_by("-updated_at", "-id")
    serializer_class = MpesaConfigSerializer


class AdminMpesaConfigDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    queryset = MpesaConfig.objects.all()
    serializer_class = MpesaConfigSerializer


# =========================================================
# Fee Config Views
# =========================================================
class AdminFeeConfigListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    queryset = TransactionFeeConfig.objects.all().order_by("-updated_at", "-id")
    serializer_class = TransactionFeeConfigSerializer


class AdminFeeConfigDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    queryset = TransactionFeeConfig.objects.all()
    serializer_class = TransactionFeeConfigSerializer


# =========================================================
# Ledger Views
# =========================================================
class MyLedgerHistoryView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = PaymentLedgerSerializer

    def get_queryset(self):
        return (
            PaymentLedger.objects.select_related("user", "mpesa_tx")
            .filter(user=self.request.user)
            .order_by("-created_at", "-id")
        )


class AdminLedgerHistoryView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    serializer_class = PaymentLedgerSerializer
    queryset = PaymentLedger.objects.select_related("user", "mpesa_tx").order_by("-created_at", "-id")


# =========================================================
# Mpesa Transaction Views
# =========================================================
class AdminMpesaTransactionsView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    serializer_class = MpesaTransactionSerializer

    def get_queryset(self):
        qs = MpesaTransaction.objects.select_related("user").order_by("-id")

        status_q = self.request.query_params.get("status")
        purpose_q = self.request.query_params.get("purpose")
        payment_method_q = self.request.query_params.get("payment_method")
        channel_q = self.request.query_params.get("channel")
        phone_q = self.request.query_params.get("phone")
        reference_q = self.request.query_params.get("reference")
        receipt_q = self.request.query_params.get("mpesa_receipt_number")

        if status_q:
            qs = qs.filter(status__iexact=status_q)
        if purpose_q:
            qs = qs.filter(purpose__iexact=purpose_q)
        if payment_method_q:
            qs = qs.filter(payment_method__iexact=payment_method_q)
        if channel_q:
            qs = qs.filter(channel__iexact=channel_q)
        if phone_q:
            qs = qs.filter(phone__icontains=phone_q)
        if reference_q:
            qs = qs.filter(reference__icontains=reference_q)
        if receipt_q:
            qs = qs.filter(mpesa_receipt_number__icontains=receipt_q)

        return qs


class MyMpesaTransactionsView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = MpesaTransactionSerializer

    def get_queryset(self):
        qs = (
            MpesaTransaction.objects.select_related("user")
            .filter(user=self.request.user)
            .order_by("-id")
        )

        status_q = self.request.query_params.get("status")
        purpose_q = self.request.query_params.get("purpose")
        payment_method_q = self.request.query_params.get("payment_method")
        channel_q = self.request.query_params.get("channel")
        phone_q = self.request.query_params.get("phone")
        reference_q = self.request.query_params.get("reference")
        amount_q = self.request.query_params.get("amount")
        allocation_status_q = self.request.query_params.get("allocation_status")

        if status_q:
            qs = qs.filter(status__iexact=status_q)
        if purpose_q:
            qs = qs.filter(purpose__iexact=purpose_q)
        if payment_method_q:
            qs = qs.filter(payment_method__iexact=payment_method_q)
        if channel_q:
            qs = qs.filter(channel__iexact=channel_q)
        if phone_q:
            qs = qs.filter(phone__icontains=phone_q)
        if reference_q:
            qs = qs.filter(reference__iexact=reference_q)
        if allocation_status_q:
            qs = qs.filter(allocation_status__iexact=allocation_status_q)

        if amount_q:
            try:
                qs = qs.filter(amount=Decimal(str(amount_q)))
            except Exception:
                pass

        return qs


class MyMpesaTransactionDetailView(generics.RetrieveAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = MpesaTransactionSerializer

    def get_queryset(self):
        return MpesaTransaction.objects.select_related("user").filter(user=self.request.user)


# =========================================================
# STK Push
# =========================================================
class MpesaStkPushView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [StkPushUserThrottle, StkPushPhoneThrottle]

    @transaction.atomic
    def post(self, request):
        phone = (request.data.get("phone") or "").strip()
        amount = request.data.get("amount")
        purpose = (request.data.get("purpose") or "").strip().upper()
        reference = (request.data.get("reference") or "").strip()
        narration = (request.data.get("narration") or "").strip()

        if not phone:
            raise ValidationError({"phone": "Phone is required."})
        if amount in (None, ""):
            raise ValidationError({"amount": "Amount is required."})
        if not purpose:
            raise ValidationError({"purpose": "Purpose is required."})

        _require_reference_format(purpose, reference)

        tx = initiate_stk_push(
            user=request.user,
            phone=phone,
            amount=Decimal(str(amount)),
            purpose=purpose,
            reference=reference,
            narration=narration,
        )

        logger.info(
            "STK push initiated | tx_id=%s | user_id=%s | phone=%s | amount=%s | purpose=%s | reference=%s | status=%s",
            tx.id,
            request.user.id,
            phone,
            tx.amount,
            purpose,
            tx.reference,
            tx.status,
        )

        return Response(
            {
                "message": "STK push sent successfully.",
                "tx": MpesaTransactionSerializer(tx).data,
            },
            status=status.HTTP_200_OK,
        )


# =========================================================
# STK Callback
# =========================================================
class MpesaStkCallbackView(APIView):
    permission_classes = [permissions.AllowAny]

    @transaction.atomic
    def post(self, request):
        try:
            _require_callback_token(request)
            _require_safaricom_ip(request)

            tx = handle_stk_callback(callback_payload=request.data)

            logger.info(
                "STK callback processed | tx_id=%s | checkout_request_id=%s | status=%s | result_code=%s | allocation_status=%s | receipt=%s | reference=%s",
                getattr(tx, "id", None),
                getattr(tx, "checkout_request_id", None),
                getattr(tx, "status", None),
                getattr(tx, "result_code", None),
                getattr(tx, "allocation_status", None),
                getattr(tx, "mpesa_receipt_number", None),
                getattr(tx, "reference", None),
            )
        except Exception as e:
            logger.exception("STK callback handling error: %s", str(e))

        return _accepted_callback_response()


# =========================================================
# C2B Validation
# =========================================================
class MpesaC2BValidationView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        try:
            _require_callback_token(request)
            _require_safaricom_ip(request)
            return Response(
                handle_c2b_validation_callback(callback_payload=request.data),
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            logger.exception("C2B validation handling error: %s", str(e))
            return Response(
                {"ResultCode": 1, "ResultDesc": "Rejected"},
                status=status.HTTP_200_OK,
            )


# =========================================================
# C2B Confirmation
# =========================================================
# class MpesaC2BConfirmationView(APIView):
#     permission_classes = [permissions.AllowAny]

#     @transaction.atomic
#     def post(self, request):
#         try:
#             _require_callback_token(request)
#             _require_safaricom_ip(request)

#             tx = handle_c2b_confirmation_callback(callback_payload=request.data)

#             logger.info(
#                 "C2B confirmation processed successfully | tx_id=%s | receipt=%s | status=%s | purpose=%s | allocation_status=%s | reference=%s | user_id=%s",
#                 getattr(tx, "id", None),
#                 getattr(tx, "mpesa_receipt_number", None),
#                 getattr(tx, "status", None),
#                 getattr(tx, "purpose", None),
#                 getattr(tx, "allocation_status", None),
#                 getattr(tx, "reference", None),
#                 getattr(tx, "user_id", None),
#             )
#         except Exception as e:
#             logger.exception("C2B confirmation handling error: %s", str(e))

#         return _accepted_callback_response()
class MpesaC2BConfirmationView(APIView):
    permission_classes = [permissions.AllowAny]

    @transaction.atomic
    def post(self, request):
        try:
            _require_callback_token(request)
            _require_safaricom_ip(request)

            print("🔥 CALLBACK DATA:", request.data)

            tx = handle_c2b_confirmation_callback(callback_payload=request.data)

            print("✅ TX CREATED:", tx)

            logger.info(
                "C2B confirmation processed successfully | tx_id=%s",
                getattr(tx, "id", None),
            )

        except Exception as e:
            print("❌ ERROR:", str(e))
            logger.exception("C2B confirmation handling error: %s", str(e))

        return _accepted_callback_response()

# =========================================================
# Withdrawals
# =========================================================
class MyWithdrawalsView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = WithdrawalSerializer

    def get_queryset(self):
        return (
            WithdrawalRequest.objects.select_related("user", "approved_by", "rejected_by", "mpesa_tx")
            .filter(user=self.request.user)
            .order_by("-created_at", "-id")
        )


class RequestWithdrawalView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        serializer = WithdrawalCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        wd = create_withdrawal_request(
            user=request.user,
            phone=serializer.validated_data["phone"],
            amount=serializer.validated_data["amount"],
            source=serializer.validated_data.get("source", "SAVINGS"),
        )

        return Response(
            WithdrawalSerializer(wd, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class AdminWithdrawalsView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    serializer_class = WithdrawalSerializer
    queryset = (
        WithdrawalRequest.objects.select_related("user", "approved_by", "rejected_by", "mpesa_tx")
        .order_by("-created_at", "-id")
    )


class ApproveWithdrawalView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    @transaction.atomic
    def post(self, request, pk: int):
        serializer = WithdrawalApproveSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        wd = approve_withdrawal_request(
            withdrawal_id=pk,
            approved_by=request.user,
        )

        auto_initiate = serializer.validated_data.get("auto_initiate_b2c", True)
        tx_data = None

        if auto_initiate:
            tx = initiate_b2c_payout_for_withdrawal(withdrawal_id=wd.id)
            tx_data = MpesaTransactionSerializer(tx, context={"request": request}).data

        return Response(
            {
                "message": "Withdrawal approved successfully.",
                "withdrawal": WithdrawalSerializer(wd, context={"request": request}).data,
                "mpesa_tx": tx_data,
            },
            status=status.HTTP_200_OK,
        )


class RejectWithdrawalView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    @transaction.atomic
    def post(self, request, pk: int):
        serializer = WithdrawalRejectSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        reason = serializer.validated_data.get("reason", "")

        wd = WithdrawalRequest.objects.select_for_update().filter(pk=pk).first()
        if not wd:
            raise NotFound("Withdrawal request not found.")

        if wd.status not in ("PENDING", "APPROVED"):
            raise ValidationError("Only pending or approved withdrawals can be rejected.")

        wd.status = "REJECTED"
        wd.rejected_by = request.user
        wd.rejected_at = timezone.now()
        if hasattr(wd, "rejection_reason"):
            wd.rejection_reason = reason
            wd.save(update_fields=["status", "rejected_by", "rejected_at", "rejection_reason"])
        else:
            wd.save(update_fields=["status", "rejected_by", "rejected_at"])

        return Response(
            {
                "message": "Withdrawal rejected successfully.",
                "withdrawal": WithdrawalSerializer(wd, context={"request": request}).data,
            },
            status=status.HTTP_200_OK,
        )


# =========================================================
# B2C Result / Timeout
# =========================================================
class MpesaB2CResultView(APIView):
    permission_classes = [permissions.AllowAny]

    @transaction.atomic
    def post(self, request):
        try:
            _require_callback_token(request)
            _require_safaricom_ip(request)

            tx = handle_b2c_result_callback(callback_payload=request.data)

            logger.info(
                "B2C result processed | tx_id=%s | conversation_id=%s | status=%s | result_code=%s | reference=%s",
                getattr(tx, "id", None),
                getattr(tx, "conversation_id", None),
                getattr(tx, "status", None),
                getattr(tx, "result_code", None),
                getattr(tx, "reference", None),
            )
        except Exception as e:
            logger.exception("B2C result handling error: %s", str(e))

        return _accepted_callback_response()


class MpesaB2CTimeoutView(APIView):
    permission_classes = [permissions.AllowAny]

    @transaction.atomic
    def post(self, request):
        try:
            _require_callback_token(request)
            _require_safaricom_ip(request)

            tx = handle_b2c_timeout_callback(callback_payload=request.data)

            logger.info(
                "B2C timeout processed | tx_id=%s | conversation_id=%s | status=%s | reference=%s",
                getattr(tx, "id", None),
                getattr(tx, "conversation_id", None),
                getattr(tx, "status", None),
                getattr(tx, "reference", None),
            )
        except Exception as e:
            logger.exception("B2C timeout handling error: %s", str(e))

        return _accepted_callback_response()