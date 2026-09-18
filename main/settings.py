import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env' / '.env.dev')

SECRET_KEY = os.getenv('SECRET_KEY', 'django-insecure-m1n*+lfy!bx87v)=6y98=-!yvfhzq0^q-^2l8k8l79#e545*1@')

DEBUG = False

ALLOWED_HOSTS = [
    'levelupapp.ru',
    '.levelupapp.ru',
    'levonework.ru',
    'loyalupp.ru',
    'vk.com',
    '.vk.com'
]

CSRF_TRUSTED_ORIGINS = [
    'https://levelupapp.ru',
    'https://*.levelupapp.ru',
    'https://levonework.ru',
    'https://loyalupp.ru',
    'https://vk.com',
    'https://*.vk.com',
    # Платформа CheckUp (волна 0 переезда, 16.09.2026): кабинет CheckUp
    # ходит в API LoyalUP из браузера. Только https и только их домены.
    'https://checkupapp.ru',
    'https://*.checkupapp.ru',
]

# Django стоит за TLS-терминирующим nginx (прод + белые прокси-«двери»). Доверяем
# заголовку X-Forwarded-Proto (его проставляет nginx), чтобы request.is_secure()=True
# и build_absolute_uri() отдавал https-URL для картинок (/media), а не http (mixed-content).
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

CORS_ALLOWED_ORIGINS = [
    'https://levelupapp.ru',
    'https://levonework.ru',
    'https://loyalupp.ru',
    'https://vk.com',
    'https://*.vk.com',
    # Платформа CheckUp (волна 0, 16.09.2026) — см. CSRF_TRUSTED_ORIGINS.
    'https://checkupapp.ru',
]

CORS_ALLOWED_ORIGIN_REGEXES = [
    r'^https://.*\.levelupapp\.ru$',
    r'^https://.*\.levonework\.ru$',
    r'^https://.*\.loyalupp\.ru$',
    r'^https://.*\.vk\.com$',
    r'^https://vk\.com$',
    r'^https://.*\.checkupapp\.ru$',
]

# Кастомный заголовок подписи запуска мини-аппа (X-VK-Launch-Params) делает
# кросс-доменные запросы «непростыми» → preflight OPTIONS. Без явного разрешения
# заголовка django-cors-headers его отвергнет и ляжет ВСЁ гостевое API.
from corsheaders.defaults import default_headers  # noqa: E402
CORS_ALLOW_HEADERS = (*default_headers, 'x-vk-launch-params', 'x-web-session')

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

SHARED_APPS = [
    'django_tenants',

    # Shared apps
    'apps.shared.config.apps.ConfigConfig',
    'apps.shared.clients.apps.ClientsConfig',
    'apps.shared.guest.apps.GuestConfig',
    'apps.shared.users.apps.UsersConfig',
    'apps.shared.leads.apps.LeadsConfig',
    'apps.shared.audit.apps.AuditConfig',
    'apps.shared.discovery.apps.DiscoveryConfig',
    'apps.shared.monitoring.apps.MonitoringConfig',
    'apps.shared.checkup.apps.CheckupConfig',  # обмен токена CheckUp → LoyalUP (контракт платформы)

    # Django built-ins
    'django.contrib.admin',
    'django.contrib.humanize',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third-party
    'rest_framework',
    'corsheaders',
    'django_filters',
    'colorfield',
    'drf_spectacular',
]

TENANT_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'django.contrib.admin',

    'apps.tenant.branch.apps.BranchAppConfig',
    'apps.tenant.catalog.apps.CatalogConfig',
    'apps.tenant.game.apps.GameConfig',
    'apps.tenant.inventory.apps.InventoryConfig',
    'apps.tenant.quest.apps.QuestConfig',
    'apps.tenant.analytics.apps.AnalyticsConfig',
    'apps.tenant.senler.apps.SenlerConfig',
    'apps.tenant.delivery.apps.DeliveryConfig',
    'apps.tenant.telegram.apps.TelegramConfig',
    'apps.tenant.mobile.apps.MobileConfig',
    'apps.tenant.loyalty.apps.LoyaltyConfig',
    'apps.tenant.marketer.apps.MarketerAppConfig',
]

INSTALLED_APPS = list(SHARED_APPS) + [
    app for app in TENANT_APPS if app not in SHARED_APPS
]

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

MIDDLEWARE = [
    'django_tenants.middleware.main.TenantMainMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    # Подпись запуска мини-аппа ВК: после TenantMainMiddleware (тенант уже
    # определён) и обязательно ПОСЛЕ CorsMiddleware — иначе на 403 из middleware
    # не навесятся CORS-заголовки и фронт увидит сетевую ошибку вместо кода.
    'apps.shared.guest.middleware.VKLaunchParamsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Журнал действий — последним, чтобы request.user уже был проставлен.
    'apps.shared.audit.middleware.AuditMiddleware',
]

# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

ROOT_URLCONF = 'main.urls'                  # tenant schemas
PUBLIC_SCHEMA_URLCONF = 'main.public_urls'  # public schema (superadmin)

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'main.wsgi.application'

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DATABASES = {
    'default': {
        'ENGINE': 'django_tenants.postgresql_backend',
        'NAME': os.getenv('POSTGRES_DB'),
        'USER': os.getenv('POSTGRES_USER'),
        'PASSWORD': os.getenv('POSTGRES_PASSWORD'),
        'HOST': os.getenv('POSTGRES_HOST'),
        'PORT': os.getenv('POSTGRES_PORT'),
    },
}

DATABASE_ROUTERS = (
    'django_tenants.routers.TenantSyncRouter',
)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

AUTH_USER_MODEL = 'users.User'

AUTHENTICATION_BACKENDS = [
    'apps.shared.users.backends.RoleBasedBackend',
]

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ---------------------------------------------------------------------------
# Internationalisation
# ---------------------------------------------------------------------------

LANGUAGE_CODE = os.getenv('LANGUAGE_CODE', 'ru')
TIME_ZONE = os.getenv('TZ', 'Europe/Moscow')
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static & Media
# ---------------------------------------------------------------------------

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']

DEFAULT_FILE_STORAGE = 'django_tenants.files.storage.TenantFileSystemStorage'
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# ---------------------------------------------------------------------------
# DRF
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': (
        'rest_framework.renderers.JSONRenderer',
    ),
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'apps.shared.users.auth.JWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ),
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
}

# ---------------------------------------------------------------------------
# drf-spectacular (Swagger / ReDoc)
# ---------------------------------------------------------------------------

SPECTACULAR_SETTINGS = {
    'TITLE': 'Levone API',
    'DESCRIPTION': 'REST API для платформы Levone',
    'VERSION': 'v1',
    'SERVE_INCLUDE_SCHEMA': False,
}

# ---------------------------------------------------------------------------
# django-tenants
# ---------------------------------------------------------------------------

TENANT_MODEL = 'clients.Company'
TENANT_DOMAIN_MODEL = 'clients.Domain'

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

VK_SECRET = os.getenv('VK_SECRET', '')
# Кандидат «Защищённого ключа» мини-аппа: пока задан — при несовпадении подписи с VK_SECRET
# в лог пишется 'vk_sign candidate=ok|mismatch'. Ничего не решает (guest/vk_sign.candidate_check).
VK_SECRET_CANDIDATE = os.getenv('VK_SECRET_CANDIDATE', '')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')

# Сервис-API лояльности для внешнего ordering-BFF (приложение заказа).
# Ключ — только в окружении, не в гите. Без него loyalty-эндпоинты fail closed.
LOYALTY_SERVICE_API_KEY = os.getenv('LOYALTY_SERVICE_API_KEY')
# Кэшбэк начисления (% от суммы заказа). BFF может прислать явный points-override.
LOYALTY_ACCRUAL_PERCENT = int(os.getenv('LOYALTY_ACCRUAL_PERCENT', 10))
VK_MINI_APP_ID=os.getenv('VK_MINI_APP_ID', 53418653)
VK_WEB_APP_ID=os.getenv('VK_WEB_APP_ID', 54473505)

# ── Подпись запуска мини-аппа ВК (apps/shared/guest/middleware.py) ────────────
# 'off' (дефолт) — только считаем нарушения в лог (`grep -c 'vk_sign '`),
# 'on' — 403 {"code": "vk_sign_invalid"}. Включать спустя несколько дней после
# деплоя фронта с заголовком X-VK-Launch-Params, когда лог покажет ~0 легитимных
# запросов без подписи (у гостей висит кэш старого бандла).
VK_SIGN_ENFORCE = os.getenv('VK_SIGN_ENFORCE', 'off')
# Домены Телеграм-мини-аппа: тот же фронт-бандл, но подписи ВК там нет —
# такие запуски enforce не трогает (см. TODO про initData в middleware).
TELEGRAM_MINI_APP_HOSTS = tuple(
    h.strip().lower()
    for h in os.getenv('TELEGRAM_MINI_APP_HOSTS', 'loyalupp.ru,www.loyalupp.ru').split(',')
    if h.strip()
)
# Аварийный клапан: префиксы гостевых путей, которые enforce не проверяет вовсе
# (например если после включения 'on' всплывёт легитимный флоу без подписи).
VK_SIGN_EXEMPT_PATHS = tuple(
    p.strip() for p in os.getenv('VK_SIGN_EXEMPT_PATHS', '').split(',') if p.strip()
)

# ── Веб-сессия гостя вне ВК (apps/shared/guest/web_session.py) ────────────────
# Сколько дней живёт токен, который выдаёт POST /api/v1/vk/auth/ и который гость
# шлёт заголовком X-Web-Session. Уменьшение = более частый повторный вход через
# VK ID; смена SECRET_KEY = разлогин всех веб-гостей разом.
WEB_SESSION_TTL_DAYS = int(os.getenv('WEB_SESSION_TTL_DAYS', 30))

# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------

CELERY_BROKER_URL            = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND        = os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
CELERY_ACCEPT_CONTENT        = ['json']
CELERY_TASK_SERIALIZER       = 'json'
CELERY_RESULT_SERIALIZER     = 'json'
CELERY_TASK_TRACK_STARTED    = True
CELERY_TASK_TIME_LIMIT       = 300          # 5 min hard limit per task
CELERY_TASK_SOFT_TIME_LIMIT  = 240          # 4 min soft limit
CELERY_WORKER_PREFETCH_MULTIPLIER = 1       # one task at a time per worker slot


# ---------------------------------------------------------------------------
# CORS — для мобильного web-превью и нативных сборок.
# Только cookie-credential и DEBUG-override. Сам whitelist origins лежит выше,
# в начале файла (CORS_ALLOWED_ORIGINS + CORS_ALLOWED_ORIGIN_REGEXES), и
# покрывает levonework.ru / loyalupp.ru / vk.com / *.levelupapp.ru — НЕ
# перезаписывать здесь, иначе VK мини-апс ложится на CORS preflight.
# ---------------------------------------------------------------------------
CORS_ALLOW_CREDENTIALS = True
if DEBUG:
    CORS_ALLOW_ALL_ORIGINS = True

# ---------------------------------------------------------------------------
# Email — для рассылки логин/пароль новым клиентам после онбординга.
# ---------------------------------------------------------------------------
EMAIL_HOST          = os.getenv('EMAIL_HOST',          'smtp.yandex.ru')
EMAIL_PORT          = int(os.getenv('EMAIL_PORT',      '465'))
EMAIL_USE_SSL       = os.getenv('EMAIL_USE_SSL',       'True') == 'True'
EMAIL_USE_TLS       = os.getenv('EMAIL_USE_TLS',       'False') == 'True'
EMAIL_HOST_USER     = os.getenv('EMAIL_HOST_USER',     '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL  = os.getenv('DEFAULT_FROM_EMAIL',  EMAIL_HOST_USER or 'noreply@levelupapp.ru')
if EMAIL_HOST_USER:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

TENANT_DOMAIN_ROOT = os.getenv('TENANT_DOMAIN_ROOT', 'levelupapp.ru')
SUPER_ADMIN_EMAILS = [
    e.strip() for e in os.getenv('SUPER_ADMIN_EMAILS', '').split(',') if e.strip()
]


# ── LoyalUP ↔ CheckUp relay (Stage 2 support chat integration) ─────────────────
# Shared secret to authenticate inbound replies from CheckUp side.
# Same value must be set on CheckUp .env. Loopback-only check enforced
# in apps.shared.relay.views.InboundReplyView.
LOYALUP_RELAY_SECRET = os.getenv("LOYALUP_RELAY_SECRET", "")
# CheckUp inbound URL (where outbound _safe_relay_to_checkup POSTs).
CHECKUP_RELAY_URL = os.getenv("CHECKUP_RELAY_URL", "http://localhost:8000/api/v1/loyalup/inbound/")

# ── Жалобы гостей → CheckUp (реестр «Отзывы/Жалобы») ────────────────
# Приёмник на стороне CheckUp: POST /api/v1/loyalup/complaints/inbound/,
# авторизация тем же LOYALUP_RELAY_SECRET. Логика сбора и отправки —
# apps/shared/relay/checkup_complaints.py.
CHECKUP_COMPLAINTS_URL = os.getenv(
    "CHECKUP_COMPLAINTS_URL",
    "https://checkupapp.ru/api/v1/loyalup/complaints/inbound/",
)
# Рубильник на нашей стороне. Второй рубильник — отдельно по каждой
# организации — живёт на стороне CheckUp (ClientConfig.loyalup_complaints_enabled).
CHECKUP_COMPLAINTS_ENABLED = os.getenv("CHECKUP_COMPLAINTS_ENABLED", "1") != "0"
# Слать ли жалобы из ВК-группы, у которых точки нет вовсе. По умолчанию НЕТ:
# у «Автосуши» точки в двух городах сразу, и жалоба без точки уехала бы наугад.
# Включить = такие жалобы лягут в CheckUp в «Без филиала», точку привяжут руками.
CHECKUP_COMPLAINTS_RELAY_UNPOINTED = os.getenv(
    "CHECKUP_COMPLAINTS_RELAY_UNPOINTED", "0") == "1"
# №78 карты переезда: телефон гостя с согласия через ВК (VKWebAppGetPhoneNumber).
# Выкл (по умолчанию) = ручка POST /api/v1/client/phone/ отвечает 404, поля
# guest.Client.phone* не заполняются, прод неотличим от эталона.
GUEST_PHONE_ENABLED = os.getenv("GUEST_PHONE_ENABLED", "0") == "1"
# Подпись телефона: 'off' — наблюдение (лог ok/mismatch, телефон сохраняется
# как 'vk_unverified' при несовпадении), 'on' — несовпадение = 403.
# Включать после нуля mismatch на живом трафике, как с VK_SIGN_ENFORCE.
GUEST_PHONE_SIGN_ENFORCE = os.getenv("GUEST_PHONE_SIGN_ENFORCE", "off")
# База для абсолютных ссылок на медиа: фото гостя уходят в CheckUp URL'ами,
# а в базе лежат относительными путями (/media/...).
CHECKUP_RELAY_MEDIA_BASE = os.getenv("CHECKUP_RELAY_MEDIA_BASE", "https://levelupapp.ru")




# ── Волна 0 переезда LoyalUP → CheckUp (16.09.2026) ─────────────────────────
# Всё ниже — с безопасными значениями по умолчанию: пока переменные окружения
# не заданы, прод ведёт себя ровно как раньше.

# Фоновые задачи beat обходят все сети подряд, даже выключенные и неоплаченные.
# Режим гарда: 'off' — как раньше; 'log' — обходим всех, но пишем в лог, кого
# бы пропустили; 'on' — пропускаем is_active=False и paid_until старше
# BEAT_PAID_UNTIL_GRACE_DAYS дней. Гость сеть с истёкшей оплатой не видит и так
# (CompanyExpired в clients/api/services.py) — гард лишь догоняет beat.
BEAT_TENANT_GUARD = os.getenv("BEAT_TENANT_GUARD", "log").strip().lower()
BEAT_PAID_UNTIL_GRACE_DAYS = int(os.getenv("BEAT_PAID_UNTIL_GRACE_DAYS", "7") or 7)

# Вебхук доставки (Dooglys/iiko → /api/v1/delivery/webhook/). Секрет
# DELIVERY_WEBHOOK_SECRET проверяется в delivery/api/services.py; при ПУСТОМ
# секрете вебхук пускает всех (так сейчас на проде). Чтобы включить секрет, не
# уронив доставку по 4 городам, сначала кладём значение в
# DELIVERY_WEBHOOK_SECRET_CANDIDATE — оно ничего не решает, только логирует,
# шлёт ли POS уже верный X-Webhook-Secret. Когда в логе всё 'ok' — переносим
# значение в DELIVERY_WEBHOOK_SECRET. Обе переменные читаются из окружения
# прямо в services.py, здесь — только памятка.

# Обмен токена CheckUp → LoyalUP: СВОЙ секрет, не LOYALUP_RELAY_SECRET (тот
# уже открывает жалобы и чат). Ручка появится с контрактом платформы; пусто =
# обмен выключен.
CHECKUP_TOKEN_EXCHANGE_SECRET = os.getenv("CHECKUP_TOKEN_EXCHANGE_SECRET", "")
# Белый список сетей для обмена (через запятую). Пусто = любая живая сеть.
# На проде сначала только dev (песочница), живые сети — по решению владельца.
CHECKUP_TOKEN_EXCHANGE_TENANTS = [
    s.strip().lower() for s in os.getenv("CHECKUP_TOKEN_EXCHANGE_TENANTS", "").split(",") if s.strip()
]
# Сколько живёт JWT из обмена (refresh не выдаётся — BFF меняет заново).
CHECKUP_TOKEN_EXCHANGE_MINUTES = int(os.getenv("CHECKUP_TOKEN_EXCHANGE_MINUTES", "60") or 60)
# Платформенный доступ (контракт 3в.1): checkup_user_id через запятую (владелец = 1).
# Для них обмен принимает ЛЮБУЮ живую сеть независимо от CHECKUP_TOKEN_EXCHANGE_TENANTS
# и ставит platform: true в JWT (сводная по всем клиентам). Меняет только владелец.
CHECKUP_PLATFORM_ADMINS = os.getenv("CHECKUP_PLATFORM_ADMINS", "")

# Мониторинг платформы (apps.shared.monitoring): сертификаты, домены, оплата
# сетей, callback ВК, доступность входа. Выключен, пока не задано
# PLATFORM_MONITOR_ENABLED=1 — задача beat тикает вхолостую.
PLATFORM_MONITOR_ENABLED = os.getenv("PLATFORM_MONITOR_ENABLED", "0") == "1"
# За сколько дней предупреждать о сроке (сертификат, домен, оплата сети).
PLATFORM_MONITOR_WARN_DAYS = int(os.getenv("PLATFORM_MONITOR_WARN_DAYS", "14") or 14)
# Хосты, у которых проверяем TLS-сертификат (через запятую). api-ya.levelupapp.ru
# («белая дверь» через Yandex Cloud) сюда не входит: 16.09.2026 мониторинг
# показал, что имени нет в сертификате и дверью никто не пользуется —
# владелец решил её снять, а не чинить.
PLATFORM_MONITOR_TLS_HOSTS = [
    h.strip() for h in os.getenv(
        "PLATFORM_MONITOR_TLS_HOSTS",
        "levelupapp.ru,vkapp.levelupapp.ru,levone.levelupapp.ru,levonework.ru",
    ).split(",") if h.strip()
]
# Домены: срок продления. Сначала живой whois (whois.tcinet.ru:43, поле
# paid-till), при молчании — эти даты как запасные. Формат: домен=ГГГГ-ММ-ДД.
PLATFORM_MONITOR_DOMAIN_EXPIRY = dict(
    kv.split("=", 1) for kv in os.getenv(
        "PLATFORM_MONITOR_DOMAIN_EXPIRY",
        "levelupapp.ru=2026-12-19,levonework.ru=2027-06-25,loyalupp.ru=2027-03-04",
    ).split(",") if "=" in kv
)
# Адреса, которые должны отвечать 200 (вход мини-аппа и API).
PLATFORM_MONITOR_PROBE_URLS = [
    u.strip() for u in os.getenv(
        "PLATFORM_MONITOR_PROBE_URLS",
        "https://levonework.ru/,https://levone.levelupapp.ru/api/v1/branches/1/",
    ).split(",") if u.strip()
]
# Один и тот же сигнал повторяем пушем не чаще, чем раз в столько часов.
PLATFORM_MONITOR_REPEAT_HOURS = int(os.getenv("PLATFORM_MONITOR_REPEAT_HOURS", "24") or 24)


# ── Логирование ────────────────────────────────────────────────────────────────
# До 23.08.2026 блока LOGGING не было вовсе: при DEBUG=False traceback'и 500-ок
# выбрасывались (дефолтный console-хендлер закрыт фильтром require_debug_true),
# и «integer out of range» на vk_id > int4 месяцами ловился только по логам
# Postgres. Пишем ошибки запросов в stderr — их видно в `docker logs web`.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {'format': '{levelname} {asctime} {name} {message}', 'style': '{'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'verbose'},
    },
    'root': {'handlers': ['console'], 'level': 'WARNING'},
    'loggers': {
        'django.request': {'handlers': ['console'], 'level': 'ERROR', 'propagate': False},
        # №78: строка наблюдения за согласиями на телефон (`guest phone: …`) —
        # несколько событий в день, нужны в логе, а корень стоит на WARNING.
        'apps.tenant.branch.api.client_phone': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'apps.tenant.branch.api.vk_message_event': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
    },
}
