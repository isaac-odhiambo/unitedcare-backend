# loans/urls.py

from django.urls import path

from .views import (
    AcceptGuaranteeView,
    AddGuarantorView,
    AdminLoansView,
    ApproveLoanView,
    DisburseLoanView,
    LoanDetailView,
    LoanEligibilityPreviewView,
    LoanGuarantorCandidatesView,
    LoanReminderLogsView,
    LoanReminderPreviewView,
    LoanSecurityPreviewView,
    MyGuaranteeRequestsView,
    MyLoansView,
    PayLoanView,
    RejectGuaranteeView,
    RejectLoanView,
    RequestLoanView,
    SendLoanReminderView,
)

urlpatterns = [
    # ===============================
    # Borrower endpoints
    # ===============================

    # List my loans
    path("myloans/", MyLoansView.as_view(), name="my-loans"),

    # Basic eligibility preview
    path("eligibility/", LoanEligibilityPreviewView.as_view(), name="loan-eligibility"),

    # Full security preview for selected amount and guarantors
    path(
        "security-preview/",
        LoanSecurityPreviewView.as_view(),
        name="loan-security-preview",
    ),

    # Search guarantor candidates
    path(
        "guarantor-candidates/",
        LoanGuarantorCandidatesView.as_view(),
        name="guarantor-candidates",
    ),

    # Request loan
    path("request/", RequestLoanView.as_view(), name="request-loan"),

    # Loan detail
    path("loan/<int:pk>/", LoanDetailView.as_view(), name="loan-detail"),

    # Add guarantor
    path("loan/add-guarantor/", AddGuarantorView.as_view(), name="add-guarantor"),

    # Borrower and admin can view reminder history for a loan
    path(
        "loan/<int:loan_id>/reminders/",
        LoanReminderLogsView.as_view(),
        name="loan-reminder-logs",
    ),

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
    # Admin endpoints
    # ===============================

    # List all loans for admin
    path("admin/loans/", AdminLoansView.as_view(), name="admin-loans"),

    # Approve loan
    path(
        "loan/<int:loan_id>/approve/",
        ApproveLoanView.as_view(),
        name="approve-loan",
    ),

    # Disburse approved loan
    path(
        "loan/<int:loan_id>/disburse/",
        DisburseLoanView.as_view(),
        name="disburse-loan",
    ),

    # Reject loan
    path(
        "loan/<int:loan_id>/reject/",
        RejectLoanView.as_view(),
        name="reject-loan",
    ),

    # Preview loan reminder before sending
    path(
        "loan/<int:loan_id>/reminder-preview/",
        LoanReminderPreviewView.as_view(),
        name="loan-reminder-preview",
    ),

    # Send loan reminder
    path(
        "loan/<int:loan_id>/send-reminder/",
        SendLoanReminderView.as_view(),
        name="send-loan-reminder",
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


# # loans/urls.py

# from django.urls import path

# from .views import (
#     MyLoansView,
#     LoanEligibilityPreviewView,
#     LoanSecurityPreviewView,
#     LoanGuarantorCandidatesView,
#     RequestLoanView,
#     LoanDetailView,
#     AddGuarantorView,
#     MyGuaranteeRequestsView,
#     AcceptGuaranteeView,
#     RejectGuaranteeView,
#     ApproveLoanView,
#     RejectLoanView,
#     PayLoanView,
# )

# urlpatterns = [
#     # ===============================
#     # Borrower endpoints
#     # ===============================

#     # List my loans
#     path("myloans/", MyLoansView.as_view(), name="my-loans"),

#     # Basic eligibility preview
#     path("eligibility/", LoanEligibilityPreviewView.as_view(), name="loan-eligibility"),

#     # Full security preview for selected amount + guarantors
#     path("security-preview/", LoanSecurityPreviewView.as_view(), name="loan-security-preview"),

#     # Search guarantor candidates
#     path("guarantor-candidates/", LoanGuarantorCandidatesView.as_view(), name="guarantor-candidates"),

#     # Request loan
#     path("request/", RequestLoanView.as_view(), name="request-loan"),

#     # Loan detail
#     path("loan/<int:pk>/", LoanDetailView.as_view(), name="loan-detail"),

#     # Add guarantor
#     path("loan/add-guarantor/", AddGuarantorView.as_view(), name="add-guarantor"),

#     # ===============================
#     # Guarantor endpoints
#     # ===============================

#     # My pending guarantee requests
#     path(
#         "guarantee/my-requests/",
#         MyGuaranteeRequestsView.as_view(),
#         name="my-guarantee-requests",
#     ),

#     # Accept guarantee
#     path(
#         "guarantee/<int:guarantor_id>/accept/",
#         AcceptGuaranteeView.as_view(),
#         name="accept-guarantee",
#     ),

#     # Reject guarantee
#     path(
#         "guarantee/<int:guarantor_id>/reject/",
#         RejectGuaranteeView.as_view(),
#         name="reject-guarantee",
#     ),

#     # ===============================
#     # Admin / Approver endpoints
#     # ===============================

#     # Approve loan
#     path(
#         "loan/<int:loan_id>/approve/",
#         ApproveLoanView.as_view(),
#         name="approve-loan",
#     ),

#     # Reject loan
#     path(
#         "loan/<int:loan_id>/reject/",
#         RejectLoanView.as_view(),
#         name="reject-loan",
#     ),

#     # ===============================
#     # Payments
#     # ===============================

#     # Pay loan
#     path(
#         "loan/<int:loan_id>/pay/",
#         PayLoanView.as_view(),
#         name="pay-loan",
#     ),
# ]