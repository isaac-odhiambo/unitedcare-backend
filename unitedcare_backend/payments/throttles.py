# payments/throttles.py
from rest_framework.throttling import UserRateThrottle, SimpleRateThrottle


class StkPushUserThrottle(UserRateThrottle):
    """
    Limit STK push requests per authenticated user.
    Uses REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']['stk_push'].
    """
    scope = "stk_push"


class StkPushPhoneThrottle(SimpleRateThrottle):
    """
    Optional: limit STK push per phone number, even if user changes accounts.
    Uses REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']['stk_push_phone'].
    """
    scope = "stk_push_phone"

    def get_cache_key(self, request, view):
        phone = (request.data.get("phone") or "").strip()
        if not phone:
            return None
        ident = phone.replace(" ", "")
        return self.cache_format % {"scope": self.scope, "ident": ident}