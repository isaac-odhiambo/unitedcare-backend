from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    GroupContributionsHistoryView,
    GroupDependantViewSet,
    GroupJoinRequestViewSet,
    GroupMembershipViewSet,
    GroupViewSet,
    MyGroupSavingsSummaryView,
    PostGroupContributionView,
)

router = DefaultRouter()

# Group management
router.register(r"groups", GroupViewSet, basename="groups")

# Membership management
router.register(r"memberships", GroupMembershipViewSet, basename="memberships")

# Join requests
router.register(r"join-requests", GroupJoinRequestViewSet, basename="group-join-requests")

# Dependants
router.register(r"dependants", GroupDependantViewSet, basename="group-dependants")

urlpatterns = [
    # Router endpoints
    path("", include(router.urls)),

    # ---------------------------------------
    # Group Savings
    # ---------------------------------------

    # List groups I belong to + my share
    path(
        "my-savings/",
        MyGroupSavingsSummaryView.as_view(),
        name="my-group-savings",
    ),

    # Member contribution into group fund
    path(
        "contribute/",
        PostGroupContributionView.as_view(),
        name="group-contribute",
    ),

    # Member contribution history
    path(
        "<int:group_id>/contributions/my/",
        GroupContributionsHistoryView.as_view(),
        {"scope": "my"},
        name="group-contributions-my",
    ),

    # Admin contribution history
    path(
        "<int:group_id>/contributions/all/",
        GroupContributionsHistoryView.as_view(),
        {"scope": "all"},
        name="group-contributions-admin",
    ),
]