from django.urls import path
from .views import (
    RegisterView,
    VerifyOTPView,
    LoginView,
    ForgotPasswordView,
    ResetPasswordView,
    ResendOTPView,
    MeView,
    KYCSubmitView,
    DeleteAccountView,
    child_safety_page,   # ✅ FIXED
    account_deletion_page,
)

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("verify-otp/", VerifyOTPView.as_view(), name="verify-otp"),
    path("login/", LoginView.as_view(), name="login"),
    path("forgot-password/", ForgotPasswordView.as_view(), name="forgot-password"),
    path("reset-password/", ResetPasswordView.as_view(), name="reset-password"),
    path("resend-otp/", ResendOTPView.as_view(), name="resend-otp"),

    path("me/", MeView.as_view(), name="me"),
    path("kyc/", KYCSubmitView.as_view(), name="kyc-submit"),

    # Account deletion
    path("delete-account/", DeleteAccountView.as_view(), name="delete_account"),
    path("account-deletion/", account_deletion_page, name="account_deletion_page"),

    # Child safety
    path("child-safety/", child_safety_page, name="child_safety_page"),
]