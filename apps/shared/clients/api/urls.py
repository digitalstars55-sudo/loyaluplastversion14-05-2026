from django.urls import path

from .views import CrossTenantOverviewView, TenantDomainView

urlpatterns = [
    path('company/<int:client_id>/', TenantDomainView.as_view(), name='tenant-domain'),
    path('overview/stats/', CrossTenantOverviewView.as_view(), name='cross-overview-stats'),
]
