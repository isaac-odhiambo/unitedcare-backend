# merry/urls.py

from django.urls import path
from . import views

urlpatterns = [

    # =====================================================
    # MERRY GROUPS
    # =====================================================

    # GET: list my merries (created + memberships)
    path(
        "",
        views.MyMerriesView.as_view(),
        name="merry-list"
    ),

    # POST: admin creates a merry
    path(
        "create/",
        views.CreateMerryView.as_view(),
        name="merry-create"
    ),

    # GET: merry detail
    path(
        "<int:merry_id>/",
        views.MerryDetailView.as_view(),
        name="merry-detail"
    ),

    # GET: members of a merry
    path(
        "<int:merry_id>/members/",
        views.MerryMembersView.as_view(),
        name="merry-members"
    ),


    # =====================================================
    # JOIN REQUEST FLOW (Member -> Admin Approval)
    # =====================================================

    # POST: member requests to join
    path(
        "<int:merry_id>/join/request/",
        views.RequestToJoinMerryView.as_view(),
        name="merry-join-request"
    ),

    # GET: member sees their own join requests
    path(
        "join/requests/",
        views.MyJoinRequestsView.as_view(),
        name="merry-my-join-requests"
    ),

    # POST: member cancels their pending join request
    path(
        "join/requests/<int:request_id>/cancel/",
        views.CancelJoinRequestView.as_view(),
        name="merry-cancel-join-request"
    ),

    # GET: admin lists join requests for a merry
    path(
        "<int:merry_id>/join/requests/",
        views.AdminListJoinRequestsView.as_view(),
        name="merry-admin-list-join-requests"
    ),

    # POST: admin approves join request
    path(
        "join/requests/<int:request_id>/approve/",
        views.AdminApproveJoinRequestView.as_view(),
        name="merry-admin-approve-join"
    ),

    # POST: admin rejects join request
    path(
        "join/requests/<int:request_id>/reject/",
        views.AdminRejectJoinRequestView.as_view(),
        name="merry-admin-reject-join"
    ),


    # =====================================================
    # CONTRIBUTIONS (STK via Payments App)
    # =====================================================

    # GET: member sees their contributions
    path(
        "contributions/",
        views.MyMerryContributionsView.as_view(),
        name="merry-my-contributions"
    ),

    # POST: create contribution intent (PENDING)
    path(
        "<int:merry_id>/contribute/",
        views.CreateContributionIntentView.as_view(),
        name="merry-contribute"
    ),

    # OPTIONAL (normally payments callback handles this)
    path(
        "contributions/<int:contribution_id>/mark-paid/",
        views.MarkContributionPaidView.as_view(),
        name="merry-mark-contribution-paid"
    ),


    # =====================================================
    # PAYOUTS (Money sent via Payments App)
    # =====================================================

    # GET: payout schedule overview
    path(
        "<int:merry_id>/payouts/schedule/",
        views.MerryPayoutScheduleView.as_view(),
        name="merry-payout-schedule"
    ),

    # POST: admin creates payout record (SCHEDULED)
    path(
        "<int:merry_id>/payouts/create/",
        views.CreatePayoutView.as_view(),
        name="merry-create-payout"
    ),

    # POST: mark payout paid (normally payments callback)
    path(
        "payouts/<int:payout_id>/mark-paid/",
        views.MarkPayoutPaidView.as_view(),
        name="merry-mark-payout-paid"
    ),
]