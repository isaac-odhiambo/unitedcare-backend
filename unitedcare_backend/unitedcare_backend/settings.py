from pathlib import Path
from datetime import timedelta
import os
from urllib.parse import urlparse
from dotenv import load_dotenv

# =========================
# BASE
# =========================
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv()

# =========================
# SECURITY
# =========================
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "change-this-in-production")

DEBUG = os.getenv("DJANGO_DEBUG", "False").lower() == "true"

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("DJANGO_ALLOWED_HOSTS", "").split(",")
    if host.strip()
]

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

# =========================
# APPLICATIONS
# =========================
INSTALLED_APPS = [
    "corsheaders",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    "rest_framework",
    "rest_framework.authtoken",
    "rest_framework_simplejwt.token_blacklist",
    "django_extensions",

    "accounts",
    "merry",
    "loans",
    "savings",
    "groups",
    "payments",
    "notifications",
]

# =========================
# MIDDLEWARE
# =========================
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

# =========================
# URLS
# =========================
ROOT_URLCONF = "unitedcare_backend.urls"

# =========================
# TEMPLATES
# =========================
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# =========================
# WSGI
# =========================
WSGI_APPLICATION = "unitedcare_backend.wsgi.application"

# =========================
# DATABASE (POSTGRES READY)
# =========================
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if DATABASE_URL:
    parsed = urlparse(DATABASE_URL)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": parsed.path[1:],
            "USER": parsed.username,
            "PASSWORD": parsed.password,
            "HOST": parsed.hostname,
            "PORT": parsed.port or 5432,
            "CONN_MAX_AGE": 600,
            "OPTIONS": {"sslmode": "require"},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("DB_NAME", "unitedcare"),
            "USER": os.getenv("DB_USER", "unitedcare_user"),
            "PASSWORD": os.getenv("DB_PASSWORD", ""),
            "HOST": os.getenv("DB_HOST", "127.0.0.1"),
            "PORT": os.getenv("DB_PORT", "5432"),
        }
    }

# =========================
# PASSWORD VALIDATION
# =========================
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

AUTH_USER_MODEL = "accounts.User"

# =========================
# REST FRAMEWORK
# =========================
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
}

# =========================
# JWT
# =========================
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# =========================
# INTERNATIONALIZATION
# =========================
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Nairobi"
USE_I18N = True
USE_TZ = True

# =========================
# STATIC (RENDER READY)
# =========================
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# =========================
# MEDIA
# =========================
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# =========================
# CORS (IMPORTANT)
# =========================
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("DJANGO_CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
CORS_ALLOW_CREDENTIALS = True

# =========================
# EMAIL
# =========================
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", 587))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = True

# =========================
# MPESA / DARAJA
# =========================
DARAJA_ENV = os.getenv("DARAJA_ENV", "sandbox")

DARAJA_CONSUMER_KEY = os.getenv("DARAJA_CONSUMER_KEY", "")
DARAJA_CONSUMER_SECRET = os.getenv("DARAJA_CONSUMER_SECRET", "")

STK_SHORTCODE = os.getenv("STK_SHORTCODE", "")
STK_PASSKEY = os.getenv("STK_PASSKEY", "")

MPESA_CALLBACK_BASE_URL = os.getenv("MPESA_CALLBACK_BASE_URL", "")
MPESA_CALLBACK_TOKEN = os.getenv("MPESA_CALLBACK_TOKEN", "")

# =========================
# SMS (OPTIONAL)
# =========================
ENABLE_SMS = os.getenv("ENABLE_SMS", "False").lower() == "true"
AFRICASTALKING_USERNAME = os.getenv("AFRICASTALKING_USERNAME", "")
AFRICASTALKING_API_KEY = os.getenv("AFRICASTALKING_API_KEY", "")
AFRICASTALKING_SENDER_ID = os.getenv("AFRICASTALKING_SENDER_ID", "")

# =========================
# SECURITY HARDENING
# =========================
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"

# # unitedcare_backend/settings.py

# from pathlib import Path
# from datetime import timedelta
# import os
# from dotenv import load_dotenv

# # =========================
# # BASE DIRECTORY
# # =========================
# BASE_DIR = Path(__file__).resolve().parent.parent
# load_dotenv()

# # =========================
# # SECURITY
# # =========================
# SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "django-insecure-change-this-in-production")
# DEBUG = os.getenv("DJANGO_DEBUG", "True").lower() == "true"

# # Comma-separated env value, e.g.:
# # DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost,192.168.100.34
# ALLOWED_HOSTS = [
#     host.strip()
#     for host in os.getenv("DJANGO_ALLOWED_HOSTS", "*").split(",")
#     if host.strip()
# ]

# # =========================
# # EMAIL SETTINGS (GMAIL SMTP)
# # =========================
# EMAIL_BACKEND = os.getenv(
#     "EMAIL_BACKEND",
#     "django.core.mail.backends.smtp.EmailBackend",
# )

# EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
# EMAIL_PORT = int(os.getenv("EMAIL_PORT", 587))

# EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "").strip()
# EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "").strip()

# EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True").lower() == "true"
# EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "False").lower() == "true"

# DEFAULT_FROM_EMAIL = os.getenv(
#     "DEFAULT_FROM_EMAIL",
#     f"United Care <{EMAIL_HOST_USER}>",
# )

# SERVER_EMAIL = os.getenv("SERVER_EMAIL", DEFAULT_FROM_EMAIL)

# # =========================
# # APPLICATION DEFINITION
# # =========================
# INSTALLED_APPS = [
#     # Third-party / cross-origin
#     "corsheaders",

#     # Django core
#     "django.contrib.admin",
#     "django.contrib.auth",
#     "django.contrib.contenttypes",
#     "django.contrib.sessions",
#     "django.contrib.messages",
#     "django.contrib.staticfiles",

#     # Third-party
#     "rest_framework",
#     "rest_framework.authtoken",
#     "rest_framework_simplejwt.token_blacklist",  # important when BLACKLIST_AFTER_ROTATION=True
#     "django_extensions",

#     # Local apps
#     "accounts",
#     "merry",
#     "loans",
#     "savings",
#     "groups",
#     "payments",
#     "notifications",
# ]

# # =========================
# # MIDDLEWARE
# # =========================
# MIDDLEWARE = [
#     "corsheaders.middleware.CorsMiddleware",
#     "django.middleware.security.SecurityMiddleware",
#     "django.contrib.sessions.middleware.SessionMiddleware",
#     "django.middleware.common.CommonMiddleware",
#     "django.middleware.csrf.CsrfViewMiddleware",
#     "django.contrib.auth.middleware.AuthenticationMiddleware",
#     "django.contrib.messages.middleware.MessageMiddleware",
#     "django.middleware.clickjacking.XFrameOptionsMiddleware",
# ]

# # If later behind nginx / reverse proxy, you can enable:
# # SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# # USE_X_FORWARDED_HOST = True

# # =========================
# # URL CONFIGURATION
# # =========================
# ROOT_URLCONF = "unitedcare_backend.urls"

# # =========================
# # TEMPLATES
# # =========================
# TEMPLATES = [
#     {
#         "BACKEND": "django.template.backends.django.DjangoTemplates",
#         "DIRS": [BASE_DIR / "templates"],  # helpful for future custom admin templates
#         "APP_DIRS": True,
#         "OPTIONS": {
#             "context_processors": [
#                 "django.template.context_processors.request",
#                 "django.contrib.auth.context_processors.auth",
#                 "django.contrib.messages.context_processors.messages",
#             ],
#         },
#     },
# ]

# # =========================
# # WSGI / ASGI
# # =========================
# WSGI_APPLICATION = "unitedcare_backend.wsgi.application"
# # ASGI_APPLICATION = "unitedcare_backend.asgi.application"  # enable later if needed

# # =========================
# # DATABASE
# # =========================
# DATABASES = {
#     "default": {
#         "ENGINE": os.getenv("DB_ENGINE", "django.db.backends.sqlite3"),
#         "NAME": os.getenv("DB_NAME", BASE_DIR / "db.sqlite3"),
#     }
# }

# # =========================
# # PASSWORD VALIDATION
# # =========================
# AUTH_PASSWORD_VALIDATORS = [
#     {
#         "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
#         "OPTIONS": {"min_length": 4},
#     },
#     {
#         "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
#     },
#     {
#         "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
#     },
# ]

# # =========================
# # CUSTOM USER MODEL
# # =========================
# AUTH_USER_MODEL = "accounts.User"

# # =========================
# # DJANGO REST FRAMEWORK
# # =========================
# REST_FRAMEWORK = {
#     "DEFAULT_AUTHENTICATION_CLASSES": (
#         "rest_framework_simplejwt.authentication.JWTAuthentication",
#     ),
#     "DEFAULT_PERMISSION_CLASSES": (
#         "rest_framework.permissions.IsAuthenticated",
#     ),
#     "DEFAULT_THROTTLE_CLASSES": [
#         "rest_framework.throttling.AnonRateThrottle",
#         "rest_framework.throttling.UserRateThrottle",
#     ],
#     "DEFAULT_THROTTLE_RATES": {
#         "anon": "20/minute",
#         "user": "100/minute",
#         "login": "5/minute",
#         "otp": "3/minute",

#         # Payments / STK spam protection
#         "stk_push": os.getenv("THROTTLE_STK_PUSH_USER", "6/min"),
#         "stk_push_phone": os.getenv("THROTTLE_STK_PUSH_PHONE", "3/min"),
#     },
# }

# # =========================
# # JWT SETTINGS
# # =========================
# SIMPLE_JWT = {
#     "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
#     "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
#     "ROTATE_REFRESH_TOKENS": True,
#     "BLACKLIST_AFTER_ROTATION": True,
#     "AUTH_HEADER_TYPES": ("Bearer",),
# }

# # =========================
# # INTERNATIONALIZATION
# # =========================
# LANGUAGE_CODE = "en-us"
# TIME_ZONE = "Africa/Nairobi"
# USE_I18N = True
# USE_TZ = True

# # =========================
# # STATIC FILES
# # =========================
# STATIC_URL = "/static/"
# STATIC_ROOT = BASE_DIR / "staticfiles"

# # =========================
# # MEDIA FILES
# # =========================
# MEDIA_URL = "/media/"
# MEDIA_ROOT = BASE_DIR / "media"

# # =========================
# # DEFAULT PRIMARY KEY
# # =========================
# DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# # =========================
# # CORS SETTINGS (DEV)
# # =========================
# CORS_ALLOW_ALL_ORIGINS = True
# CORS_ALLOW_CREDENTIALS = True

# # For production, prefer this instead of allowing all origins:
# # CORS_ALLOWED_ORIGINS = [
# #     "http://localhost:8081",
# #     "http://127.0.0.1:8081",
# # ]

# # =========================
# # DJANGO ADMIN / AUTH REDIRECTS
# # =========================
# LOGIN_URL = "/admin/login/"
# LOGIN_REDIRECT_URL = "/admin/"
# LOGOUT_REDIRECT_URL = "/admin/login/"

# # =========================
# # AFRICA'S TALKING (SMS OTP)
# # =========================
# AFRICASTALKING_USERNAME = os.getenv("AFRICASTALKING_USERNAME", "sandbox")
# AFRICASTALKING_API_KEY = os.getenv("AFRICASTALKING_API_KEY", "your_api_key_here")
# AFRICASTALKING_SENDER_ID = os.getenv("AFRICASTALKING_SENDER_ID", "UNITEDCARE")

# # =========================
# # CELERY
# # =========================
# CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
# CELERY_ACCEPT_CONTENT = ["json"]
# CELERY_TASK_SERIALIZER = "json"
# CELERY_RESULT_SERIALIZER = "json"
# CELERY_TIMEZONE = "Africa/Nairobi"

# CELERY_BEAT_SCHEDULE = {
#     "apply-late-fees-nightly": {
#         "task": "loans.tasks.apply_late_fees_and_tag_defaulters",
#         "schedule": 60 * 60 * 24,  # daily
#     },
# }

# # =========================
# # PAYMENTS + MPESA
# # =========================
# MPESA_CALLBACK_BASE_URL = os.getenv("MPESA_CALLBACK_BASE_URL", "").rstrip("/")
# MPESA_CALLBACK_TOKEN = os.getenv("MPESA_CALLBACK_TOKEN", "")

# MPESA_ENABLE_STK_QUERY_VERIFICATION = os.getenv(
#     "MPESA_ENABLE_STK_QUERY_VERIFICATION", "True"
# ).lower() == "true"

# MPESA_STRICT_AMOUNT_MATCH = os.getenv(
#     "MPESA_STRICT_AMOUNT_MATCH", "True"
# ).lower() == "true"

# # =========================
# # DARAJA SETTINGS
# # =========================
# DARAJA_ENV = os.getenv("DARAJA_ENV", "sandbox").strip().lower()
# DARAJA_CONSUMER_KEY = os.getenv("DARAJA_CONSUMER_KEY", "").strip()
# DARAJA_CONSUMER_SECRET = os.getenv("DARAJA_CONSUMER_SECRET", "").strip()

# STK_SHORTCODE = os.getenv("STK_SHORTCODE", "").strip()
# STK_PASSKEY = os.getenv("STK_PASSKEY", "").strip()

# B2C_SHORTCODE = os.getenv("B2C_SHORTCODE", "").strip()
# B2C_INITIATOR_NAME = os.getenv("B2C_INITIATOR_NAME", "").strip()
# B2C_SECURITY_CREDENTIAL = os.getenv("B2C_SECURITY_CREDENTIAL", "").strip()
# B2C_COMMAND_ID = os.getenv("B2C_COMMAND_ID", "BusinessPayment").strip()

# # =========================
# # CACHE
# # =========================
# USE_REDIS_CACHE = os.getenv("USE_REDIS_CACHE", "False").lower() == "true"

# if USE_REDIS_CACHE:
#     CACHES = {
#         "default": {
#             "BACKEND": "django.core.cache.backends.redis.RedisCache",
#             "LOCATION": os.getenv("REDIS_CACHE_URL", "redis://127.0.0.1:6379/1"),
#         }
#     }
# else:
#     CACHES = {
#         "default": {
#             "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
#             "LOCATION": "unitedcare-cache",
#         }
#     }

# # =========================
# # SECURITY HARDENING
# # =========================
# if not DEBUG:
#     SECURE_SSL_REDIRECT = True
#     SESSION_COOKIE_SECURE = True
#     CSRF_COOKIE_SECURE = True

#     SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))
#     SECURE_HSTS_INCLUDE_SUBDOMAINS = True
#     SECURE_HSTS_PRELOAD = True

#     SECURE_CONTENT_TYPE_NOSNIFF = True
#     X_FRAME_OPTIONS = "DENY"

# # =========================
# # OPTIONAL LOGGING (good for admin/backend debugging)
# # =========================
# LOGGING = {
#     "version": 1,
#     "disable_existing_loggers": False,
#     "handlers": {
#         "console": {
#             "class": "logging.StreamHandler",
#         },
#     },
#     "root": {
#         "handlers": ["console"],
#         "level": "INFO",
#     },
# }