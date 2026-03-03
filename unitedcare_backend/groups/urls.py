from rest_framework.routers import DefaultRouter
from .views import GroupViewSet, GroupMembershipViewSet

router = DefaultRouter()
router.register(r"groups", GroupViewSet, basename="groups")
router.register(r"memberships", GroupMembershipViewSet, basename="memberships")

urlpatterns = router.urls
