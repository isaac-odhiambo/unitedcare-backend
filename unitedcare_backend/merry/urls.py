from django.urls import path

from .views import (
    AvailableMerriesView,
    MyMerriesView,
    CreateMerryView,
    MerryDetailView,
    MerryMobileDetailView,
    MerryMobileReadinessRowsView,
    MerryMembersView,
    MerrySeatsView,
    RequestToJoinMerryView,
    CancelJoinRequestView,
    MyJoinRequestsView,
    AdminListJoinRequestsView,
    AdminApproveJoinRequestView,
    AdminRejectJoinRequestView,
    MyAllMerryDueSummaryView,
    MerryPaymentBreakdownView,
    MyMerryWalletView,
    MyMerryWalletTransactionsView,
    AdminUserMerryWalletView,
    EnsureDuesForCurrentPeriodView,
    MyMerryDuesView,
    AdminDuesView,
    CreatePaymentIntentView,
    MyPaymentsView,
    AdminMarkPaymentConfirmedView,
    MerryPayoutScheduleView,
    NextPayoutTurnView,
    CreatePayoutView,
    CreateNextPayoutView,
    MarkPayoutPaidView,
    MerryMemberDashboardView,
    PayoutReadinessView,
)

urlpatterns = [
    # =========================
    # Available / My
    # =========================
    path("available/", AvailableMerriesView.as_view(), name="merry-available"),
    path("my/", MyMerriesView.as_view(), name="merry-my"),

    # =========================
    # Merry management
    # =========================
    path("create/", CreateMerryView.as_view(), name="merry-create"),

    # Mobile optimized detail endpoints
    path("<int:merry_id>/mobile-detail/", MerryMobileDetailView.as_view(), name="merry-mobile-detail"),
    path("<int:merry_id>/mobile-readiness-rows/", MerryMobileReadinessRowsView.as_view(), name="merry-mobile-readiness-rows"),

    path("<int:merry_id>/", MerryDetailView.as_view(), name="merry-detail"),

    # =========================
    # Members / Seats
    # =========================
    path("<int:merry_id>/members/", MerryMembersView.as_view(), name="merry-members"),
    path("<int:merry_id>/seats/", MerrySeatsView.as_view(), name="merry-seats"),

    # =========================
    # Join requests
    # =========================
    path("<int:merry_id>/join/request/", RequestToJoinMerryView.as_view(), name="merry-join-request"),
    path("join/requests/my/", MyJoinRequestsView.as_view(), name="merry-join-requests-my"),
    path("join/requests/<int:request_id>/cancel/", CancelJoinRequestView.as_view(), name="merry-join-request-cancel"),

    # =========================
    # Admin join moderation
    # =========================
    path("<int:merry_id>/join/requests/", AdminListJoinRequestsView.as_view(), name="merry-join-requests-admin"),
    path("join/requests/<int:request_id>/approve/", AdminApproveJoinRequestView.as_view(), name="merry-join-request-approve"),
    path("join/requests/<int:request_id>/reject/", AdminRejectJoinRequestView.as_view(), name="merry-join-request-reject"),

    # =========================
    # Dues / Dashboard
    # =========================
    path("dues/summary/", MyAllMerryDueSummaryView.as_view(), name="merry-dues-summary"),
    path("<int:merry_id>/dues/ensure/", EnsureDuesForCurrentPeriodView.as_view(), name="merry-dues-ensure"),
    path("<int:merry_id>/dues/my/", MyMerryDuesView.as_view(), name="merry-dues-my"),
    path("<int:merry_id>/dues/", AdminDuesView.as_view(), name="merry-dues-admin"),
    path("<int:merry_id>/dashboard/", MerryMemberDashboardView.as_view(), name="merry-member-dashboard"),

    # =========================
    # Payments
    # =========================
    path("<int:merry_id>/payments/breakdown/", MerryPaymentBreakdownView.as_view(), name="merry-payment-breakdown"),
    path("<int:merry_id>/payments/intent/", CreatePaymentIntentView.as_view(), name="merry-payment-intent"),
    path("payments/my/", MyPaymentsView.as_view(), name="merry-payments-my"),
    path("payments/<int:payment_id>/confirm/", AdminMarkPaymentConfirmedView.as_view(), name="merry-payment-confirm"),

    # =========================
    # Merry wallet
    # =========================
    path("wallet/my/", MyMerryWalletView.as_view(), name="merry-wallet-my"),
    path("wallet/my/transactions/", MyMerryWalletTransactionsView.as_view(), name="merry-wallet-my-transactions"),
    path("admin/users/<int:user_id>/wallet/", AdminUserMerryWalletView.as_view(), name="merry-wallet-admin-user"),

    # =========================
    # Payouts
    # =========================
    path("<int:merry_id>/payouts/schedule/", MerryPayoutScheduleView.as_view(), name="merry-payout-schedule"),
    path("<int:merry_id>/payouts/next-turn/", NextPayoutTurnView.as_view(), name="merry-payout-next-turn"),
    path("<int:merry_id>/payouts/readiness/", PayoutReadinessView.as_view(), name="merry-payout-readiness"),
    path("<int:merry_id>/payouts/create/", CreatePayoutView.as_view(), name="merry-payout-create"),
    path("<int:merry_id>/payouts/create-next/", CreateNextPayoutView.as_view(), name="merry-payout-create-next"),
    path("payouts/<int:payout_id>/paid/", MarkPayoutPaidView.as_view(), name="merry-payout-paid"),
]
# from django.urls import path

# from .views import (
#     AvailableMerriesView,
#     MyMerriesView,
#     CreateMerryView,
#     MerryDetailView,
#     MerryMembersView,
#     MerrySeatsView,
#     RequestToJoinMerryView,
#     CancelJoinRequestView,
#     MyJoinRequestsView,
#     AdminListJoinRequestsView,
#     AdminApproveJoinRequestView,
#     AdminRejectJoinRequestView,
#     MyAllMerryDueSummaryView,
#     MerryPaymentBreakdownView,
#     MyMerryWalletView,
#     MyMerryWalletTransactionsView,
#     AdminUserMerryWalletView,
#     EnsureDuesForCurrentPeriodView,
#     MyMerryDuesView,
#     AdminDuesView,
#     CreatePaymentIntentView,
#     MyPaymentsView,
#     AdminMarkPaymentConfirmedView,
#     MerryPayoutScheduleView,
#     NextPayoutTurnView,
#     CreatePayoutView,
#     CreateNextPayoutView,
#     MarkPayoutPaidView,
#     MerryMemberDashboardView,
#     PayoutReadinessView,
# )

# urlpatterns = [
#     # =========================
#     # Available / My
#     # =========================
#     path("available/", AvailableMerriesView.as_view(), name="merry-available"),
#     path("my/", MyMerriesView.as_view(), name="merry-my"),

#     # =========================
#     # Merry management
#     # =========================
#     path("create/", CreateMerryView.as_view(), name="merry-create"),
#     path("<int:merry_id>/", MerryDetailView.as_view(), name="merry-detail"),

#     # =========================
#     # Members / Seats
#     # =========================
#     path("<int:merry_id>/members/", MerryMembersView.as_view(), name="merry-members"),
#     path("<int:merry_id>/seats/", MerrySeatsView.as_view(), name="merry-seats"),

#     # =========================
#     # Join requests
#     # =========================
#     path("<int:merry_id>/join/request/", RequestToJoinMerryView.as_view(), name="merry-join-request"),
#     path("join/requests/my/", MyJoinRequestsView.as_view(), name="merry-join-requests-my"),
#     path("join/requests/<int:request_id>/cancel/", CancelJoinRequestView.as_view(), name="merry-join-request-cancel"),

#     # =========================
#     # Admin join moderation
#     # =========================
#     path("<int:merry_id>/join/requests/", AdminListJoinRequestsView.as_view(), name="merry-join-requests-admin"),
#     path("join/requests/<int:request_id>/approve/", AdminApproveJoinRequestView.as_view(), name="merry-join-request-approve"),
#     path("join/requests/<int:request_id>/reject/", AdminRejectJoinRequestView.as_view(), name="merry-join-request-reject"),

#     # =========================
#     # Dues / Dashboard
#     # =========================
#     path("dues/summary/", MyAllMerryDueSummaryView.as_view(), name="merry-dues-summary"),
#     path("<int:merry_id>/dues/ensure/", EnsureDuesForCurrentPeriodView.as_view(), name="merry-dues-ensure"),
#     path("<int:merry_id>/dues/my/", MyMerryDuesView.as_view(), name="merry-dues-my"),
#     path("<int:merry_id>/dues/", AdminDuesView.as_view(), name="merry-dues-admin"),
#     path("<int:merry_id>/dashboard/", MerryMemberDashboardView.as_view(), name="merry-member-dashboard"),

#     # =========================
#     # Payments
#     # =========================
#     path("<int:merry_id>/payments/breakdown/", MerryPaymentBreakdownView.as_view(), name="merry-payment-breakdown"),
#     path("<int:merry_id>/payments/intent/", CreatePaymentIntentView.as_view(), name="merry-payment-intent"),
#     path("payments/my/", MyPaymentsView.as_view(), name="merry-payments-my"),
#     path("payments/<int:payment_id>/confirm/", AdminMarkPaymentConfirmedView.as_view(), name="merry-payment-confirm"),

#     # =========================
#     # Merry wallet
#     # =========================
#     path("wallet/my/", MyMerryWalletView.as_view(), name="merry-wallet-my"),
#     path("wallet/my/transactions/", MyMerryWalletTransactionsView.as_view(), name="merry-wallet-my-transactions"),
#     path("admin/users/<int:user_id>/wallet/", AdminUserMerryWalletView.as_view(), name="merry-wallet-admin-user"),

#     # =========================
#     # Payouts
#     # =========================
#     path("<int:merry_id>/payouts/schedule/", MerryPayoutScheduleView.as_view(), name="merry-payout-schedule"),
#     path("<int:merry_id>/payouts/next-turn/", NextPayoutTurnView.as_view(), name="merry-payout-next-turn"),
#     path("<int:merry_id>/payouts/readiness/", PayoutReadinessView.as_view(), name="merry-payout-readiness"),
#     path("<int:merry_id>/payouts/create/", CreatePayoutView.as_view(), name="merry-payout-create"),
#     path("<int:merry_id>/payouts/create-next/", CreateNextPayoutView.as_view(), name="merry-payout-create-next"),
#     path("payouts/<int:payout_id>/paid/", MarkPayoutPaidView.as_view(), name="merry-payout-paid"),
# ]
