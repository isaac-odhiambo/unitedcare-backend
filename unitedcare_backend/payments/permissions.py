from rest_framework.permissions import BasePermission


class IsAdmin(BasePermission):
    """
    Allows access only to system admins.

    Conditions:
    - user must be authenticated
    - user must be active
    - user must not be blocked
    - user.is_admin must be True
    """

    message = "Admin privileges are required."

    def has_permission(self, request, view):
        user = request.user

        if not user or not user.is_authenticated:
            return False

        if not getattr(user, "is_active", False):
            return False

        if getattr(user, "status", None) == "blocked":
            return False

        if not getattr(user, "is_admin", False):
            return False

        return True