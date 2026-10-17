from django.urls import path

from .views import (
    DiscoveryCitiesView,
    DiscoveryNetworksView,
    DiscoveryOpenView,
    DiscoveryPlayView,
    DiscoveryClaimView,
    DiscoveryStatusView,
    DiscoveryActivateView,
)

urlpatterns = [
    path('discovery/cities/',   DiscoveryCitiesView.as_view(),   name='discovery-cities'),
    path('discovery/networks/', DiscoveryNetworksView.as_view(), name='discovery-networks'),
    path('discovery/open/',     DiscoveryOpenView.as_view(),     name='discovery-open'),
    path('discovery/play/',     DiscoveryPlayView.as_view(),     name='discovery-play'),
    path('discovery/claim/',    DiscoveryClaimView.as_view(),    name='discovery-claim'),
    path('discovery/status/',   DiscoveryStatusView.as_view(),   name='discovery-status'),
    path('discovery/activate/', DiscoveryActivateView.as_view(), name='discovery-activate'),
]
