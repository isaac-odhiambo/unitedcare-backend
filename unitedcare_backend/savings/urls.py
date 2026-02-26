from django.urls import path
from .views import (
    MySavingsAccountsView,
    DepositToSavingsView,
    SavingsAccountHistoryView,
    CreateWithdrawRequestView,
    AdminApproveWithdrawRequestView,
    AdminRejectWithdrawRequestView,
    AdminPayWithdrawRequestView,
)
from .views_statement import DownloadSavingsStatementPDF

urlpatterns = [
    # accounts
    path("accounts/", MySavingsAccountsView.as_view(), name="my-savings-accounts"),

    # deposit
    path("deposit/", DepositToSavingsView.as_view(), name="deposit-to-savings"),

    # history
    path("accounts/<int:account_id>/history/", SavingsAccountHistoryView.as_view(), name="savings-history"),

    # withdrawals
    path("withdraw/request/", CreateWithdrawRequestView.as_view(), name="create-withdraw-request"),
    path("withdraw/<int:withdraw_id>/approve/", AdminApproveWithdrawRequestView.as_view(), name="admin-approve-withdraw"),
    path("withdraw/<int:withdraw_id>/reject/", AdminRejectWithdrawRequestView.as_view(), name="admin-reject-withdraw"),
    path("withdraw/<int:withdraw_id>/pay/", AdminPayWithdrawRequestView.as_view(), name="admin-pay-withdraw"),

    # statement pdf
    path("accounts/<int:account_id>/statement.pdf", DownloadSavingsStatementPDF.as_view(), name="download-savings-statement"),
]