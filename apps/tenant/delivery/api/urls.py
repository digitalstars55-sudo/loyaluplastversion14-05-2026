from django.urls import path

from .views import DeliveryCodeView, DeliveryCodeResolveView

urlpatterns = [
    path('code/', DeliveryCodeView.as_view(), name='delivery-code'),
    path('code/resolve/', DeliveryCodeResolveView.as_view(), name='delivery-code-resolve'),
]
