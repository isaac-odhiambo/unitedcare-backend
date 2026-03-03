# payments/urls.py
from django.urls import path
from .views import (
    MyWithdrawalsView, RequestWithdrawalView,
    AdminWithdrawalsView, ApproveWithdrawalView, RejectWithdrawalView,
    MyLedgerHistoryView, AdminLedgerHistoryView,
    AdminMpesaTransactionsView,
    MpesaStkPushView, MpesaStkCallbackView,
    MpesaB2CResultView, MpesaB2CTimeoutView,
)

urlpatterns = [
    # withdrawals
    path("withdrawals/my/", MyWithdrawalsView.as_view()),
    path("withdrawals/request/", RequestWithdrawalView.as_view()),
    path("withdrawals/admin/", AdminWithdrawalsView.as_view()),
    path("withdrawals/<int:pk>/approve/", ApproveWithdrawalView.as_view()),
    path("withdrawals/<int:pk>/reject/", RejectWithdrawalView.as_view()),

    # ledger
    path("ledger/my/", MyLedgerHistoryView.as_view()),
    path("ledger/admin/", AdminLedgerHistoryView.as_view()),

    # mpesa tx admin
    path("mpesa/admin/", AdminMpesaTransactionsView.as_view()),

    # mpesa initiation + callbacks
    path("mpesa/stk-push/", MpesaStkPushView.as_view()),
    path("mpesa/stk/callback/", MpesaStkCallbackView.as_view()),
    path("mpesa/b2c/result/", MpesaB2CResultView.as_view()),
    path("mpesa/b2c/timeout/", MpesaB2CTimeoutView.as_view()),
]