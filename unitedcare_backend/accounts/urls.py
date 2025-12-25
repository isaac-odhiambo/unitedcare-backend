from django.urls import path
from .views import (
    MemberRegisterView,
    VerifyOTPView,
    KYCUploadView,
)

urlpatterns = [
    path("register/", MemberRegisterView.as_view()),
    path("verify-otp/", VerifyOTPView.as_view()),
    path("kyc-upload/", KYCUploadView.as_view()),
]
