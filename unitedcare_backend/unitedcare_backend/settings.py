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
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    raise ValueError("DJANGO_SECRET_KEY is required in production.")

DEBUG = os.getenv("DJANGO_DEBUG", "False").lower() == "true"

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("DJANGO_ALLOWED_HOSTS", "").split(",")
    if host.strip()
]
if not ALLOWED_HOSTS:
    raise ValueError("DJANGO_ALLOWED_HOSTS is required in production.")

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
# DATABASE (RENDER POSTGRES)
# =========================
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise ValueError("DATABASE_URL is required in production.")

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
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "sslmode": os.getenv("DB_SSLMODE", "require"),
        },
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
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

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
    "DEFAULT_THROTTLE_RATES": {
        "login": os.getenv("THROTTLE_LOGIN", "10/min"),
        "otp": os.getenv("THROTTLE_OTP", "5/hour"),
    },
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
# STATIC
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
# CORS
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
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", EMAIL_HOST_USER)

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
# SMS
# =========================
ENABLE_SMS = os.getenv("ENABLE_SMS", "False").lower() == "true"
AFRICASTALKING_USERNAME = os.getenv("AFRICASTALKING_USERNAME", "")
AFRICASTALKING_API_KEY = os.getenv("AFRICASTALKING_API_KEY", "")
AFRICASTALKING_SENDER_ID = os.getenv("AFRICASTALKING_SENDER_ID", "")

# =========================
# SECURITY HARDENING (ENV-BASED)
# =========================
SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "True").lower() == "true"
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "True").lower() == "true"
CSRF_COOKIE_SECURE = os.getenv("CSRF_COOKIE_SECURE", "True").lower() == "true"

SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"

# =========================
# OPTIONAL UPLOAD PROTECTION
# =========================
DATA_UPLOAD_MAX_MEMORY_SIZE = 10485760
FILE_UPLOAD_MAX_MEMORY_SIZE = 10485760


# from pathlib import Path
# from datetime import timedelta
# import os
# from urllib.parse import urlparse
# from dotenv import load_dotenv

# # =========================
# # BASE
# # =========================
# BASE_DIR = Path(__file__).resolve().parent.parent
# load_dotenv()

# # =========================
# # SECURITY
# # =========================
# SECRET_KEY = os.getenv("DJANGO_SECRET_KEY")
# if not SECRET_KEY:
#     raise ValueError("DJANGO_SECRET_KEY is required in production.")

# DEBUG = False

# ALLOWED_HOSTS = [
#     host.strip()
#     for host in os.getenv("DJANGO_ALLOWED_HOSTS", "").split(",")
#     if host.strip()
# ]
# if not ALLOWED_HOSTS:
#     raise ValueError("DJANGO_ALLOWED_HOSTS is required in production.")

# CSRF_TRUSTED_ORIGINS = [
#     origin.strip()
#     for origin in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
#     if origin.strip()
# ]

# # =========================
# # APPLICATIONS
# # =========================
# INSTALLED_APPS = [
#     "corsheaders",
#     "django.contrib.admin",
#     "django.contrib.auth",
#     "django.contrib.contenttypes",
#     "django.contrib.sessions",
#     "django.contrib.messages",
#     "django.contrib.staticfiles",

#     "rest_framework",
#     "rest_framework.authtoken",
#     "rest_framework_simplejwt.token_blacklist",

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
#     "django.middleware.security.SecurityMiddleware",
#     "whitenoise.middleware.WhiteNoiseMiddleware",
#     "corsheaders.middleware.CorsMiddleware",
#     "django.contrib.sessions.middleware.SessionMiddleware",
#     "django.middleware.common.CommonMiddleware",
#     "django.middleware.csrf.CsrfViewMiddleware",
#     "django.contrib.auth.middleware.AuthenticationMiddleware",
#     "django.contrib.messages.middleware.MessageMiddleware",
#     "django.middleware.clickjacking.XFrameOptionsMiddleware",
# ]

# SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# USE_X_FORWARDED_HOST = True

# # =========================
# # URLS
# # =========================
# ROOT_URLCONF = "unitedcare_backend.urls"

# # =========================
# # TEMPLATES
# # =========================
# TEMPLATES = [
#     {
#         "BACKEND": "django.template.backends.django.DjangoTemplates",
#         "DIRS": [BASE_DIR / "templates"],
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
# # WSGI
# # =========================
# WSGI_APPLICATION = "unitedcare_backend.wsgi.application"

# # =========================
# # DATABASE (RENDER POSTGRES)
# # =========================
# DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
# if not DATABASE_URL:
#     raise ValueError("DATABASE_URL is required in production.")

# parsed = urlparse(DATABASE_URL)

# DATABASES = {
#     "default": {
#         "ENGINE": "django.db.backends.postgresql",
#         "NAME": parsed.path[1:],
#         "USER": parsed.username,
#         "PASSWORD": parsed.password,
#         "HOST": parsed.hostname,
#         "PORT": parsed.port or 5432,
#         "CONN_MAX_AGE": 600,
#         "CONN_HEALTH_CHECKS": True,
#         "OPTIONS": {
#             "sslmode": os.getenv("DB_SSLMODE", "require"),
#         },
#     }
# }

# # =========================
# # PASSWORD VALIDATION
# # =========================
# AUTH_PASSWORD_VALIDATORS = [
#     {
#         "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
#         "OPTIONS": {"min_length": 8},
#     },
#     {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
#     {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
# ]

# AUTH_USER_MODEL = "accounts.User"
# DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# # =========================
# # REST FRAMEWORK
# # =========================
# REST_FRAMEWORK = {
#     "DEFAULT_AUTHENTICATION_CLASSES": (
#         "rest_framework_simplejwt.authentication.JWTAuthentication",
#     ),
#     "DEFAULT_PERMISSION_CLASSES": (
#         "rest_framework.permissions.IsAuthenticated",
#     ),
# }

# # =========================
# # JWT
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
# # STATIC
# # =========================
# STATIC_URL = "/static/"
# STATIC_ROOT = BASE_DIR / "staticfiles"
# STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# # =========================
# # MEDIA
# # =========================
# MEDIA_URL = "/media/"
# MEDIA_ROOT = BASE_DIR / "media"

# # =========================
# # CORS
# # =========================
# CORS_ALLOW_ALL_ORIGINS = False
# CORS_ALLOWED_ORIGINS = [
#     origin.strip()
#     for origin in os.getenv("DJANGO_CORS_ALLOWED_ORIGINS", "").split(",")
#     if origin.strip()
# ]
# CORS_ALLOW_CREDENTIALS = True

# # =========================
# # EMAIL
# # =========================
# EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
# EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
# EMAIL_PORT = int(os.getenv("EMAIL_PORT", 587))
# EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
# EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
# EMAIL_USE_TLS = True
# DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", EMAIL_HOST_USER)

# # =========================
# # MPESA / DARAJA
# # =========================
# DARAJA_ENV = os.getenv("DARAJA_ENV", "sandbox")

# DARAJA_CONSUMER_KEY = os.getenv("DARAJA_CONSUMER_KEY", "")
# DARAJA_CONSUMER_SECRET = os.getenv("DARAJA_CONSUMER_SECRET", "")

# STK_SHORTCODE = os.getenv("STK_SHORTCODE", "")
# STK_PASSKEY = os.getenv("STK_PASSKEY", "")

# MPESA_CALLBACK_BASE_URL = os.getenv("MPESA_CALLBACK_BASE_URL", "")
# MPESA_CALLBACK_TOKEN = os.getenv("MPESA_CALLBACK_TOKEN", "")

# # =========================
# # SMS
# # =========================
# ENABLE_SMS = os.getenv("ENABLE_SMS", "False").lower() == "true"
# AFRICASTALKING_USERNAME = os.getenv("AFRICASTALKING_USERNAME", "")
# AFRICASTALKING_API_KEY = os.getenv("AFRICASTALKING_API_KEY", "")
# AFRICASTALKING_SENDER_ID = os.getenv("AFRICASTALKING_SENDER_ID", "")

# # =========================
# # SECURITY HARDENING
# # =========================
# SECURE_SSL_REDIRECT = True
# SESSION_COOKIE_SECURE = True
# CSRF_COOKIE_SECURE = True

# SESSION_COOKIE_SAMESITE = "Lax"
# CSRF_COOKIE_SAMESITE = "Lax"

# SECURE_HSTS_SECONDS = 31536000
# SECURE_HSTS_INCLUDE_SUBDOMAINS = True
# SECURE_HSTS_PRELOAD = True

# SECURE_CONTENT_TYPE_NOSNIFF = True
# SECURE_BROWSER_XSS_FILTER = True
# X_FRAME_OPTIONS = "DENY"
# SECURE_REFERRER_POLICY = "same-origin"

# # =========================
# # OPTIONAL UPLOAD PROTECTION
# # =========================
# DATA_UPLOAD_MAX_MEMORY_SIZE = 10485760
# FILE_UPLOAD_MAX_MEMORY_SIZE = 10485760

