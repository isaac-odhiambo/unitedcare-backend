# savings/urls.py
from django.urls import path

from .views import (
    MySavingsAccountsView,
    CreateSavingsAccountView,
    DepositToSavingsView,
    SavingsAccountHistoryView,
    CreateWithdrawRequestView,
    MyWithdrawRequestsView,
    AdminApproveWithdrawRequestView,
    AdminRejectWithdrawRequestView,
    AdminPayWithdrawRequestView,
)

from .views_statement import DownloadSavingsStatementPDF

urlpatterns = [
    # -----------------------------
    # Accounts
    # -----------------------------
    path("accounts/", MySavingsAccountsView.as_view(), name="my-savings-accounts"),
    path("accounts/create/", CreateSavingsAccountView.as_view(), name="create-savings-account"),

    # -----------------------------
    # Deposits
    # -----------------------------
    path("deposit/", DepositToSavingsView.as_view(), name="deposit-to-savings"),

    # -----------------------------
    # History / Statement
    # -----------------------------
    path("accounts/<int:account_id>/history/", SavingsAccountHistoryView.as_view(), name="savings-history"),
    path("accounts/<int:account_id>/statement.pdf", DownloadSavingsStatementPDF.as_view(), name="download-savings-statement"),

    # -----------------------------
    # Withdrawals (member)
    # -----------------------------
    path("withdraw/request/", CreateWithdrawRequestView.as_view(), name="create-withdraw-request"),
    path("withdraw/requests/", MyWithdrawRequestsView.as_view(), name="my-withdraw-requests"),

    # -----------------------------
    # Withdrawals (admin workflow)
    # -----------------------------
    path("admin/withdraw/<int:withdraw_id>/approve/", AdminApproveWithdrawRequestView.as_view(), name="admin-approve-withdraw"),
    path("admin/withdraw/<int:withdraw_id>/reject/", AdminRejectWithdrawRequestView.as_view(), name="admin-reject-withdraw"),
    path("admin/withdraw/<int:withdraw_id>/pay/", AdminPayWithdrawRequestView.as_view(), name="admin-pay-withdraw"),
]
