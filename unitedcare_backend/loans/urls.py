# loans/urls.py

from django.urls import path

from .views import (
    MyLoansView,
    LoanEligibilityPreviewView,
    LoanSecurityPreviewView,
    LoanGuarantorCandidatesView,
    RequestLoanView,
    LoanDetailView,
    AddGuarantorView,
    MyGuaranteeRequestsView,
    AcceptGuaranteeView,
    RejectGuaranteeView,
    ApproveLoanView,
    RejectLoanView,
    PayLoanView,
)

urlpatterns = [
    # ===============================
    # Borrower endpoints
    # ===============================

    # List my loans
    path("myloans/", MyLoansView.as_view(), name="my-loans"),

    # Basic eligibility preview
    path("eligibility/", LoanEligibilityPreviewView.as_view(), name="loan-eligibility"),

    # Full security preview for selected amount + guarantors
    path("security-preview/", LoanSecurityPreviewView.as_view(), name="loan-security-preview"),

    # Search guarantor candidates
    path("guarantor-candidates/", LoanGuarantorCandidatesView.as_view(), name="guarantor-candidates"),

    # Request loan
    path("request/", RequestLoanView.as_view(), name="request-loan"),

    # Loan detail
    path("loan/<int:pk>/", LoanDetailView.as_view(), name="loan-detail"),

    # Add guarantor
    path("loan/add-guarantor/", AddGuarantorView.as_view(), name="add-guarantor"),

    # ===============================
    # Guarantor endpoints
    # ===============================

    # My pending guarantee requests
    path(
        "guarantee/my-requests/",
        MyGuaranteeRequestsView.as_view(),
        name="my-guarantee-requests",
    ),

    # Accept guarantee
    path(
        "guarantee/<int:guarantor_id>/accept/",
        AcceptGuaranteeView.as_view(),
        name="accept-guarantee",
    ),

    # Reject guarantee
    path(
        "guarantee/<int:guarantor_id>/reject/",
        RejectGuaranteeView.as_view(),
        name="reject-guarantee",
    ),

    # ===============================
    # Admin / Approver endpoints
    # ===============================

    # Approve loan
    path(
        "loan/<int:loan_id>/approve/",
        ApproveLoanView.as_view(),
        name="approve-loan",
    ),

    # Reject loan
    path(
        "loan/<int:loan_id>/reject/",
        RejectLoanView.as_view(),
        name="reject-loan",
    ),

    # ===============================
    # Payments
    # ===============================

    # Pay loan
    path(
        "loan/<int:loan_id>/pay/",
        PayLoanView.as_view(),
        name="pay-loan",
    ),
]