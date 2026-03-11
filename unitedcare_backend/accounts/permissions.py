from rest_framework.permissions import BasePermission


class IsActiveUser(BasePermission):
    """
    User must have verified OTP and be able to log in.
    """

    message = "Your account is not active. Verify OTP first."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and not user.is_blocked
        )


class IsFullyApprovedUser(BasePermission):
    """
    Full access users only.
    Requires:
    - OTP verified
    - KYC approved
    - Admin approval
    """

    message = "Complete KYC and wait for admin approval to access this feature."

    def has_permission(self, request, view):
        user = request.user

        if not user or not user.is_authenticated:
            return False

        if not user.is_active:
            return False

        if user.is_blocked:
            return False

        if user.status != "approved":
            return False

        if not hasattr(user, "kycprofile"):
            return False

        if user.kycprofile.status != "approved":
            return False

        return True


class IsAdminUserRole(BasePermission):
    """
    Allows access only to admin users inside the app.
    """

    message = "Admin access required."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and user.is_admin
        )