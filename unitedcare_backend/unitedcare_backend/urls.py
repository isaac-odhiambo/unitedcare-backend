from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path("admin/", admin.site.urls),

    path("api/accounts/", include("accounts.urls")),
    path("api/merry/", include("merry.urls")),
    path("api/loans/", include("loans.urls")),
    path("api/savings/", include("savings.urls")),

    # ✅ ADD GROUPS HERE (router urls)
    path("api/groups/", include("groups.urls")),

    # payments is NOT under /api/ in your project
    path("payments/", include("payments.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)


