from rest_framework.throttling import SimpleRateThrottle


class LoginThrottle(SimpleRateThrottle):
    scope = "login"

    def get_cache_key(self, request, view):
        return self.get_ident(request)


class OTPThrottle(SimpleRateThrottle):
    scope = "otp"

    def get_cache_key(self, request, view):
        return self.get_ident(request)
