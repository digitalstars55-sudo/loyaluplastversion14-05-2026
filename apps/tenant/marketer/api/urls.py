"""
Маршруты AI-маркетолога (контракт платформы 3б.5, №29).

Отдельный urls-модуль: подключение стоит одной строки в main/urls.py.
"""
from django.urls import path

from .views import (
    MarketerGenerateAPIView,
    MarketerPostContextAPIView,
    MarketerPostDetailAPIView,
    MarketerPostListAPIView,
    MarketerPostPublishAPIView,
    MarketerPostRejectAPIView,
    MarketerSettingsAPIView,
)

urlpatterns = [
    path('marketer/settings/', MarketerSettingsAPIView.as_view(), name='marketer-settings'),
    path('marketer/posts/', MarketerPostListAPIView.as_view(), name='marketer-posts'),
    # generate — до <int:pk>, чтобы путь не съел слово generate.
    path('marketer/posts/generate/', MarketerGenerateAPIView.as_view(),
         name='marketer-posts-generate'),
    path('marketer/posts/<int:pk>/', MarketerPostDetailAPIView.as_view(),
         name='marketer-post-detail'),
    path('marketer/posts/<int:pk>/context/', MarketerPostContextAPIView.as_view(),
         name='marketer-post-context'),
    path('marketer/posts/<int:pk>/publish/', MarketerPostPublishAPIView.as_view(),
         name='marketer-post-publish'),
    path('marketer/posts/<int:pk>/reject/', MarketerPostRejectAPIView.as_view(),
         name='marketer-post-reject'),
]
