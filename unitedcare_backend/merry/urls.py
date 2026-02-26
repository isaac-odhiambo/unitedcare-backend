from django.urls import path
from .views import pay_contribution, mpesa_callback

urlpatterns = [
    path("pay/", pay_contribution),
    path("mpesa/callback/", mpesa_callback),
]

# from django.urls import path
# from .views import pay_contribution

# urlpatterns = [
#     path("pay/", pay_contribution),
# ]

# from django.urls import path
# from .views import mpesa_callback

# urlpatterns = [
#     path("mpesa/callback/", mpesa_callback),
# ]