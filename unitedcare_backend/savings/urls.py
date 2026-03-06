from django.urls import path

from .views import (
    MySavingsAccountsView,
    CreateSavingsAccountView,
    DepositToSavingsView,
    SavingsAccountHistoryView,
)

urlpatterns = [
    path("accounts/", MySavingsAccountsView.as_view(), name="my-savings-accounts"),
    path("accounts/create/", CreateSavingsAccountView.as_view(), name="create-savings-account"),
    path("accounts/<int:account_id>/history/", SavingsAccountHistoryView.as_view(), name="savings-history"),
    path("deposit/", DepositToSavingsView.as_view(), name="deposit-to-savings"),
]