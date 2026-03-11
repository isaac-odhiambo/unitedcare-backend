# payments/urls.py
from django.urls import path

from .views import (
    AdminFeeConfigDetailView,
    AdminFeeConfigListCreateView,
    AdminLedgerHistoryView,
    AdminMpesaTransactionsView,
    AdminWithdrawalsView,
    ApproveWithdrawalView,
    MpesaB2CResultView,
    MpesaB2CTimeoutView,
    MpesaC2BConfirmationView,
    MpesaC2BValidationView,
    MpesaStkCallbackView,
    MpesaStkPushView,
    MyLedgerHistoryView,
    MyWithdrawalsView,
    RejectWithdrawalView,
    RequestWithdrawalView,
)

urlpatterns = [
    # =========================================================
    # Fee Config (Admin)
    # =========================================================
    path("fees/admin/", AdminFeeConfigListCreateView.as_view(), name="admin-fee-config-list-create"),
    path("fees/admin/<int:pk>/", AdminFeeConfigDetailView.as_view(), name="admin-fee-config-detail"),

    # =========================================================
    # Withdrawals
    # =========================================================
    path("withdrawals/my/", MyWithdrawalsView.as_view(), name="my-withdrawals"),
    path("withdrawals/request/", RequestWithdrawalView.as_view(), name="request-withdrawal"),
    path("withdrawals/admin/", AdminWithdrawalsView.as_view(), name="admin-withdrawals"),
    path("withdrawals/<int:pk>/approve/", ApproveWithdrawalView.as_view(), name="approve-withdrawal"),
    path("withdrawals/<int:pk>/reject/", RejectWithdrawalView.as_view(), name="reject-withdrawal"),

    # =========================================================
    # Ledger
    # =========================================================
    path("ledger/my/", MyLedgerHistoryView.as_view(), name="my-ledger"),
    path("ledger/admin/", AdminLedgerHistoryView.as_view(), name="admin-ledger"),

    # =========================================================
    # Mpesa / STK / C2B / B2C
    # =========================================================
    path("mpesa/admin/", AdminMpesaTransactionsView.as_view(), name="admin-mpesa-transactions"),
    path("mpesa/stk-push/", MpesaStkPushView.as_view(), name="mpesa-stk-push"),
    path("mpesa/stk/callback/", MpesaStkCallbackView.as_view(), name="mpesa-stk-callback"),

    path("mpesa/c2b/validation/", MpesaC2BValidationView.as_view(), name="mpesa-c2b-validation"),
    path("mpesa/c2b/confirmation/", MpesaC2BConfirmationView.as_view(), name="mpesa-c2b-confirmation"),

    path("mpesa/b2c/result/", MpesaB2CResultView.as_view(), name="mpesa-b2c-result"),
    path("mpesa/b2c/timeout/", MpesaB2CTimeoutView.as_view(), name="mpesa-b2c-timeout"),
]