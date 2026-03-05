# merry/urls.py
from django.urls import path

from .views import (
    # merry
    MyMerriesView,
    CreateMerryView,
    MerryDetailView,

    # members / seats
    MerryMembersView,
    MerrySeatsView,

    # slots
    SlotConfigView,

    # join requests
    RequestToJoinMerryView,
    CancelJoinRequestView,
    MyJoinRequestsView,
    AdminListJoinRequestsView,
    AdminApproveJoinRequestView,
    AdminRejectJoinRequestView,

    # dues
    EnsureDuesForCurrentPeriodView,
    MyMerryDuesView,
    AdminDuesView,

    # payments
    CreatePaymentIntentView,
    MyPaymentsView,
    AdminMarkPaymentConfirmedView,

    # payouts
    MerryPayoutScheduleView,
    CreatePayoutView,
    MarkPayoutPaidView,
)

urlpatterns = [
    # =========================
    # My (created + memberships)
    # =========================
    path("my/", MyMerriesView.as_view(), name="merry-my"),

    # =========================
    # Merry management
    # =========================
    path("create/", CreateMerryView.as_view(), name="merry-create"),
    path("<int:merry_id>/", MerryDetailView.as_view(), name="merry-detail"),

    # =========================
    # Members / Seats (admin or member)
    # =========================
    path("<int:merry_id>/members/", MerryMembersView.as_view(), name="merry-members"),
    path("<int:merry_id>/seats/", MerrySeatsView.as_view(), name="merry-seats"),

    # =========================
    # Slot config (GET for member/admin, POST admin)
    # =========================
    path("<int:merry_id>/slots/", SlotConfigView.as_view(), name="merry-slots"),

    # =========================
    # Join requests
    # =========================
    path("<int:merry_id>/join/request/", RequestToJoinMerryView.as_view(), name="merry-join-request"),
    path("join/requests/my/", MyJoinRequestsView.as_view(), name="merry-join-requests-my"),
    path("join/requests/<int:request_id>/cancel/", CancelJoinRequestView.as_view(), name="merry-join-request-cancel"),

    # Admin moderation
    path("<int:merry_id>/join/requests/", AdminListJoinRequestsView.as_view(), name="merry-join-requests-admin"),
    path("join/requests/<int:request_id>/approve/", AdminApproveJoinRequestView.as_view(), name="merry-join-request-approve"),
    path("join/requests/<int:request_id>/reject/", AdminRejectJoinRequestView.as_view(), name="merry-join-request-reject"),

    # =========================
    # Dues
    # =========================
    path("<int:merry_id>/dues/ensure/", EnsureDuesForCurrentPeriodView.as_view(), name="merry-dues-ensure"),
    path("<int:merry_id>/dues/my/", MyMerryDuesView.as_view(), name="merry-dues-my"),
    path("<int:merry_id>/dues/", AdminDuesView.as_view(), name="merry-dues-admin"),

    # =========================
    # Payments
    # =========================
    path("<int:merry_id>/payments/intent/", CreatePaymentIntentView.as_view(), name="merry-payment-intent"),
    path("payments/my/", MyPaymentsView.as_view(), name="merry-payments-my"),
    path("payments/<int:payment_id>/confirm/", AdminMarkPaymentConfirmedView.as_view(), name="merry-payment-confirm"),

    # =========================
    # Payouts
    # =========================
    path("<int:merry_id>/payouts/schedule/", MerryPayoutScheduleView.as_view(), name="merry-payout-schedule"),
    path("<int:merry_id>/payouts/create/", CreatePayoutView.as_view(), name="merry-payout-create"),
    path("payouts/<int:payout_id>/paid/", MarkPayoutPaidView.as_view(), name="merry-payout-paid"),
]