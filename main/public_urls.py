from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

from apps.shared.checkup.views import InternalTenantsView
from apps.shared.checkup.scans_export import CardOrdersExportView, ScansExportView
from apps.shared.checkup.guest_card import GuestCardView
from django.views.generic import TemplateView

from drf_spectacular.views import SpectacularAPIView
from drf_spectacular.views import SpectacularSwaggerView as _SwaggerView
from drf_spectacular.views import SpectacularRedocView as _RedocView


class SpectacularSwaggerView(_SwaggerView):
    schema = None


class SpectacularRedocView(_RedocView):
    schema = None

from apps.shared.config.admin_sites import public_admin
from apps.tenant.delivery.api.public_views import PublicDeliveryWebhook
from apps.tenant.analytics.api.orders_public import PublicDailyOrdersIngest
from main.views import health
from apps.shared.guest.request_log import ClientLogView
from apps.tenant.marketer.studio_worker import (
    StudioClaimView, StudioDoneView, StudioFileView, StudioOutputView, StudioProgressView,
    StudioSeeClaimView, StudioSeeFileView, StudioSeeResultView,
)

urlpatterns = [
    path('api/v1/health/', health, name='health'),
    path('api/v1/client-log/', ClientLogView.as_view(), name='public-client-log'),  # ошибки браузера гостя (15.10.2026)
    # Очередь сборщика роликов студии (19.10.2026): токен X-Studio-Worker-Token.
    path('api/v1/studio-worker/claim/', StudioClaimView.as_view(), name='studio-worker-claim'),
    path('api/v1/studio-worker/see/claim/', StudioSeeClaimView.as_view(), name='studio-worker-see-claim'),
    path('api/v1/studio-worker/see/<str:schema>/<int:aid>/file/', StudioSeeFileView.as_view(),
         name='studio-worker-see-file'),
    path('api/v1/studio-worker/see/<str:schema>/<int:aid>/', StudioSeeResultView.as_view(),
         name='studio-worker-see-result'),
    path('api/v1/studio-worker/<str:schema>/<int:vid>/file/<str:key>/', StudioFileView.as_view(),
         name='studio-worker-file'),
    path('api/v1/studio-worker/<str:schema>/<int:vid>/progress/', StudioProgressView.as_view(),
         name='studio-worker-progress'),
    path('api/v1/studio-worker/<str:schema>/<int:vid>/output/', StudioOutputView.as_view(),
         name='studio-worker-output'),
    path('api/v1/studio-worker/<str:schema>/<int:vid>/done/', StudioDoneView.as_view(),
         name='studio-worker-done'),
    path('admin/', public_admin.urls),
    path('api/v1/', include('apps.shared.clients.api.urls')),
    path('api/v1/delivery/webhook/', PublicDeliveryWebhook.as_view(), name='public-delivery-webhook'),
    path('api/v1/orders/daily/', PublicDailyOrdersIngest.as_view(), name='public-daily-orders-ingest'),
    path('api/v1/internal/support/', include('apps.shared.relay.urls')),
    # Обмен токена CheckUp → LoyalUP (контракт платформы 2.1): тот же контур, что релей.
    path('api/v1/internal/auth/', include('apps.shared.checkup.urls')),
    path('api/v1/internal/tenants/', InternalTenantsView.as_view(), name='checkup-internal-tenants'),  # сети платформы (контракт 3в.2)
    # Сканы QR для ночной сверки CheckUp «скан со стола → официант» (24.09.2026):
    # свой секрет X-LoyalUP-Export-Secret, сеть — в ?tenant_schema, белый список сетей.
    path('api/v1/internal/checkup/scans/', ScansExportView.as_view(), name='checkup-internal-scans'),
    path('api/v1/internal/checkup/card-orders/', CardOrdersExportView.as_view(), name='checkup-internal-card-orders'),
    # Карточка звонящего CheckUp (08.11.2026): гость по номеру — свой секрет, свой белый список сетей.
    path('api/v1/internal/checkup/guest-card/', GuestCardView.as_view(), name='checkup-internal-guest-card'),
    # Вердикт по жалобе CheckUp → LoyalUP (контракт 5.2): на публичном хосте сеть — в теле.
    path('api/v1/internal/complaints/', include('apps.shared.relay.complaints_urls')),

    # Сетевой вход из каталога VK (новичок без QR) — публичная схема.
    path('api/v1/', include('apps.shared.discovery.api.urls')),

    # Мобильное API на public-схеме: auth (JWT) + lead-онбординг.
    path('api/v1/', include('apps.shared.users.api.urls')),
    path('api/v1/', include('apps.shared.leads.api.urls')),

    # Юридические страницы (для App Store / Google Play и VK).
    path('privacy', TemplateView.as_view(template_name='legal/privacy.html'), name='privacy'),
    path('terms', TemplateView.as_view(template_name='legal/terms.html'), name='terms'),
    path('support', TemplateView.as_view(template_name='legal/support.html'), name='support'),

    # API Docs
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
