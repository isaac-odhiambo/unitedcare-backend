# loans/urls.py
from django.urls import path
from .views import (
    MyLoansView,
    RequestLoanView,
    LoanDetailView,
    AddGuarantorView,
    MyGuaranteeRequestsView,
    AcceptGuaranteeView,
    RejectGuaranteeView,
    ApproveLoanView,
    PayLoanView,
)

urlpatterns = [
    # Borrower endpoints
    path("myloans/", MyLoansView.as_view(), name="my-loans"),
    path("request/", RequestLoanView.as_view(), name="request-loan"),
    path("loan/<int:pk>/", LoanDetailView.as_view(), name="loan-detail"),
    path("loan/add-guarantor/", AddGuarantorView.as_view(), name="add-guarantor"),

    # Guarantor endpoints
    path("guarantee/my-requests/", MyGuaranteeRequestsView.as_view(), name="my-guarantee-requests"),
    path("guarantee/<int:guarantor_id>/accept/", AcceptGuaranteeView.as_view(), name="accept-guarantee"),
    path("guarantee/<int:guarantor_id>/reject/", RejectGuaranteeView.as_view(), name="reject-guarantee"),

    # Admin/Approver endpoints
    path("loan/<int:loan_id>/approve/", ApproveLoanView.as_view(), name="approve-loan"),

    # Payment endpoints
    path("loan/<int:loan_id>/pay/", PayLoanView.as_view(), name="pay-loan"),
]