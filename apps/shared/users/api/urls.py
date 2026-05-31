"""
URL-конфиг мобильного auth API. Подключается из main/urls.py
и main/public_urls.py с префиксом 'api/v1/'.
"""

from django.urls import path

from .views import (
    LoginAPIView, LogoutAPIView, MeAPIView, PushRegisterAPIView, RefreshAPIView,
    NotificationListAPIView, PushPrefsAPIView,
)

urlpatterns = [
    path('auth/login/',    LoginAPIView.as_view(),    name='mobile-auth-login'),
    path('auth/logout/',   LogoutAPIView.as_view(),   name='mobile-auth-logout'),
    path('auth/me/',       MeAPIView.as_view(),       name='mobile-auth-me'),
    path('me/',            MeAPIView.as_view(),       name='mobile-me'),
    path('me/push-prefs/', PushPrefsAPIView.as_view(), name='mobile-push-prefs'),
    path('auth/refresh/',  RefreshAPIView.as_view(),  name='mobile-auth-refresh'),
    path('push/register/', PushRegisterAPIView.as_view(), name='mobile-push-register'),
    path('notifications/', NotificationListAPIView.as_view(), name='mobile-notifications'),
]
