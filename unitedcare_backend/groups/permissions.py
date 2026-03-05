# groups/permissions.py
from rest_framework.permissions import BasePermission


class IsActiveApprovedUser(BasePermission):
    """
    User must be authenticated, active, and not blocked.
    """
    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and getattr(user, "status", None) != "blocked"
        )


class IsAdmin(BasePermission):
    """
    Admin = superuser OR staff OR app role=admin.
    Uses your custom User.is_admin property.
    """
    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and getattr(user, "status", None) != "blocked"
            and getattr(user, "is_admin", False)
        )


class IsSuperAdmin(BasePermission):
    """
    ✅ Only Django superuser can pass (true super admin).
    """
    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and getattr(user, "status", None) != "blocked"
            and getattr(user, "is_superuser", False)
        )