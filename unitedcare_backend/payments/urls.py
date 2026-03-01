from django.urls import path

from .views import (
    # Withdrawals (member)
    MyWithdrawalsView,
    RequestWithdrawalView,

    # Withdrawals (admin)
    AdminWithdrawalsView,
    ApproveWithdrawalView,
    RejectWithdrawalView,

    # Ledger / history
    MyLedgerHistoryView,
    AdminLedgerHistoryView,

    # Mpesa
    MpesaStkPushView,
    MpesaStkCallbackView,
    MpesaB2CResultView,
    MpesaB2CTimeoutView,

    # Optional admin mpesa list
    AdminMpesaTransactionsView,
)

urlpatterns = [
    # ==========================
    # Withdrawals (Member)
    # ==========================
    path("withdrawals/my/", MyWithdrawalsView.as_view(), name="my-withdrawals"),
    path("withdrawals/request/", RequestWithdrawalView.as_view(), name="request-withdrawal"),

    # ==========================
    # Withdrawals (Admin)
    # ==========================
    path("withdrawals/admin/", AdminWithdrawalsView.as_view(), name="admin-withdrawals"),
    path("withdrawals/<int:pk>/approve/", ApproveWithdrawalView.as_view(), name="approve-withdrawal"),
    path("withdrawals/<int:pk>/reject/", RejectWithdrawalView.as_view(), name="reject-withdrawal"),

    # ==========================
    # Ledger / History
    # ==========================
    path("ledger/my/", MyLedgerHistoryView.as_view(), name="my-ledger"),
    path("ledger/admin/", AdminLedgerHistoryView.as_view(), name="admin-ledger"),

    # ==========================
    # Mpesa
    # ==========================
    path("mpesa/stk-push/", MpesaStkPushView.as_view(), name="mpesa-stk-push"),
    path("mpesa/stk/callback/", MpesaStkCallbackView.as_view(), name="mpesa-stk-callback"),

    path("mpesa/b2c/result/", MpesaB2CResultView.as_view(), name="mpesa-b2c-result"),
    path("mpesa/b2c/timeout/", MpesaB2CTimeoutView.as_view(), name="mpesa-b2c-timeout"),

    # ==========================
    # Mpesa (Admin optional)
    # ==========================
    path("mpesa/admin/", AdminMpesaTransactionsView.as_view(), name="admin-mpesa-transactions"),
]