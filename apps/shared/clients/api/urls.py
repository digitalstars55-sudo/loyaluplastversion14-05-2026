from django.urls import path

from .views import CrossTenantOverviewView, CrossTenantReviewsView, TenantDomainView

urlpatterns = [
    path('company/<int:client_id>/', TenantDomainView.as_view(), name='tenant-domain'),
    path('overview/stats/', CrossTenantOverviewView.as_view(), name='cross-overview-stats'),
    path('overview/reviews/', CrossTenantReviewsView.as_view(), name='cross-overview-reviews'),
]
