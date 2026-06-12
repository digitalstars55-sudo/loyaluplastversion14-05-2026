from django.conf import settings
from django.conf.urls.static import static
from django.urls import path, include

from drf_spectacular.views import SpectacularAPIView
from drf_spectacular.views import SpectacularSwaggerView as _SwaggerView
from drf_spectacular.views import SpectacularRedocView as _RedocView


class SpectacularSwaggerView(_SwaggerView):
    schema = None


class SpectacularRedocView(_RedocView):
    schema = None

from apps.shared.config.admin_sites import tenant_admin
from main.views import health

urlpatterns = [
    path('api/v1/health/', health, name='health'),
    path('admin/', tenant_admin.urls),
    path('api/v1/', include('apps.tenant.branch.api.urls')),
    path('api/v1/', include('apps.tenant.catalog.api.urls')),
    path('api/v1/', include('apps.tenant.delivery.api.urls')),
    path('api/v1/', include('apps.tenant.game.api.urls')),
    path('api/v1/', include('apps.tenant.inventory.api.urls')),
    path('api/v1/', include('apps.tenant.quest.api.urls')),
    path('telegram/', include('apps.tenant.telegram.api.urls')),
    path('api/v1/', include('apps.tenant.analytics.api.urls')),
    path('analytics/', include('apps.tenant.analytics.urls')),

    # Мобильное API: auth (JWT) + push register + tenant data. Аддитивно, веб не трогает.
    path('api/v1/', include('apps.shared.users.api.urls')),
    path('api/v1/', include('apps.tenant.mobile.api.urls')),

    # API Docs
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)