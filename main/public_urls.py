from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path
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

urlpatterns = [
    path('api/v1/health/', health, name='health'),
    path('admin/', public_admin.urls),
    path('api/v1/', include('apps.shared.clients.api.urls')),
    path('api/v1/delivery/webhook/', PublicDeliveryWebhook.as_view(), name='public-delivery-webhook'),
    path('api/v1/orders/daily/', PublicDailyOrdersIngest.as_view(), name='public-daily-orders-ingest'),
    path('api/v1/internal/support/', include('apps.shared.relay.urls')),

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
