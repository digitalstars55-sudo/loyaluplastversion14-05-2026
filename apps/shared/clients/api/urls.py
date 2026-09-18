from django.urls import path

from .views import CrossTenantOverviewExportView, CrossTenantOverviewView, CrossTenantReviewsView, TenantDomainView

urlpatterns = [
    path('company/<int:client_id>/', TenantDomainView.as_view(), name='tenant-domain'),
    path('overview/stats/', CrossTenantOverviewView.as_view(), name='cross-overview-stats'),
    path('overview/reviews/', CrossTenantReviewsView.as_view(), name='cross-overview-reviews'),
    path('overview/export/', CrossTenantOverviewExportView.as_view(), name='cross-overview-export'),  # №34, контракт v1.8
]
