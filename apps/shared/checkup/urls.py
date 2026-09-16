from django.urls import path

from .views import TokenExchangeView

urlpatterns = [
    path('exchange/', TokenExchangeView.as_view(), name='checkup-token-exchange'),
]
