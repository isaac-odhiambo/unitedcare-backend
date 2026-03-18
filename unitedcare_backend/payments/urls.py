from django.urls import path

from .views import (
    AdminFeeConfigDetailView,
    AdminFeeConfigListCreateView,
    AdminLedgerHistoryView,
    AdminMpesaTransactionsView,
    AdminWithdrawalsView,
    ApproveWithdrawalView,
    RejectWithdrawalView,
    RequestWithdrawalView,
    MyWithdrawalsView,
    MyLedgerHistoryView,
    MpesaStkPushView,
    MpesaStkCallbackView,
    MpesaC2BValidationView,
    MpesaC2BConfirmationView,
    MpesaB2CResultView,
    MpesaB2CTimeoutView,
)

urlpatterns = [
    # =========================================================
    # Fee Config (Admin)
    # =========================================================
    path(
        "fees/admin/",
        AdminFeeConfigListCreateView.as_view(),
        name="payments-admin-fee-config-list-create",
    ),
    path(
        "fees/admin/<int:pk>/",
        AdminFeeConfigDetailView.as_view(),
        name="payments-admin-fee-config-detail",
    ),

    # =========================================================
    # Withdrawals
    # =========================================================
    path(
        "withdrawals/my/",
        MyWithdrawalsView.as_view(),
        name="payments-my-withdrawals",
    ),
    path(
        "withdrawals/request/",
        RequestWithdrawalView.as_view(),
        name="payments-request-withdrawal",
    ),
    path(
        "withdrawals/admin/",
        AdminWithdrawalsView.as_view(),
        name="payments-admin-withdrawals",
    ),
    path(
        "withdrawals/<int:pk>/approve/",
        ApproveWithdrawalView.as_view(),
        name="payments-approve-withdrawal",
    ),
    path(
        "withdrawals/<int:pk>/reject/",
        RejectWithdrawalView.as_view(),
        name="payments-reject-withdrawal",
    ),

    # =========================================================
    # Ledger
    # =========================================================
    path(
        "ledger/my/",
        MyLedgerHistoryView.as_view(),
        name="payments-my-ledger",
    ),
    path(
        "ledger/admin/",
        AdminLedgerHistoryView.as_view(),
        name="payments-admin-ledger",
    ),

    # =========================================================
    # M-Pesa / STK / C2B / B2C
    # =========================================================
    path(
        "mpesa/admin/",
        AdminMpesaTransactionsView.as_view(),
        name="payments-admin-mpesa-transactions",
    ),
    path(
        "mpesa/stk-push/",
        MpesaStkPushView.as_view(),
        name="payments-mpesa-stk-push",
    ),
    path(
        "mpesa/stk/callback/",
        MpesaStkCallbackView.as_view(),
        name="payments-mpesa-stk-callback",
    ),
    path(
        "mpesa/c2b/validation/",
        MpesaC2BValidationView.as_view(),
        name="payments-mpesa-c2b-validation",
    ),
    path(
        "mpesa/c2b/confirmation/",
        MpesaC2BConfirmationView.as_view(),
        name="payments-mpesa-c2b-confirmation",
    ),
    path(
        "mpesa/b2c/result/",
        MpesaB2CResultView.as_view(),
        name="payments-mpesa-b2c-result",
    ),
    path(
        "mpesa/b2c/timeout/",
        MpesaB2CTimeoutView.as_view(),
        name="payments-mpesa-b2c-timeout",
    ),
]