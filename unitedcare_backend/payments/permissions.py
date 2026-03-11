from rest_framework.permissions import BasePermission


class IsActiveUser(BasePermission):
    """
    Allows access to users who verified OTP and are active.

    Conditions:
    - authenticated
    - is_active = True
    - user not blocked
    """

    message = "Your account is not active. Please verify OTP."

    def has_permission(self, request, view):
        user = request.user

        if not user or not user.is_authenticated:
            return False

        if not getattr(user, "is_active", False):
            return False

        if getattr(user, "status", None) == "blocked":
            return False

        return True


class IsFullyApprovedUser(BasePermission):
    """
    Allows access only to fully approved users.

    Conditions:
    - authenticated
    - is_active = True
    - user not blocked
    - user.status = approved
    - KYC approved
    """

    message = "Complete KYC and wait for admin approval to access this feature."

    def has_permission(self, request, view):
        user = request.user

        if not user or not user.is_authenticated:
            return False

        if not getattr(user, "is_active", False):
            return False

        if getattr(user, "status", None) == "blocked":
            return False

        if getattr(user, "status", None) != "approved":
            return False

        # check KYC
        if not hasattr(user, "kycprofile"):
            return False

        if user.kycprofile.status != "approved":
            return False

        return True


class IsAdmin(BasePermission):
    """
    Allows access only to system admins.

    Conditions:
    - authenticated
    - is_active = True
    - user not blocked
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