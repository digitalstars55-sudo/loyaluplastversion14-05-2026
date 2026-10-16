"""
Маршруты AI-маркетолога (контракт платформы 3б.5, №29).

Отдельный urls-модуль: подключение стоит одной строки в main/urls.py.
"""
from django.urls import path

from .views import (
    MarketerContentPlanAPIView,
    MarketerJobDetailAPIView,
    MarketerJobListAPIView,
    MarketerPhotoDetailAPIView,
    MarketerPhotoListAPIView,
    MarketerPostUsePhotoAPIView,
    MarketerEstimateAPIView,
    MarketerPostGenerateImageAPIView,
    MarketerGenerateAPIView,
    MarketerPostImageAPIView,
    MarketerPostScheduleAPIView,
    MarketerPostContextAPIView,
    MarketerPostRewriteAPIView,
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
    path('marketer/posts/plan/', MarketerContentPlanAPIView.as_view(), name='marketer-posts-plan'),
    path('marketer/posts/estimate/', MarketerEstimateAPIView.as_view(), name='marketer-posts-estimate'),
    path('marketer/posts/<int:pk>/', MarketerPostDetailAPIView.as_view(),
         name='marketer-post-detail'),
    path('marketer/posts/<int:pk>/rewrite/', MarketerPostRewriteAPIView.as_view(),
         name='marketer-post-rewrite'),
    path('marketer/posts/<int:pk>/context/', MarketerPostContextAPIView.as_view(),
         name='marketer-post-context'),
    path('marketer/posts/<int:pk>/publish/', MarketerPostPublishAPIView.as_view(),
         name='marketer-post-publish'),
    path('marketer/posts/<int:pk>/schedule/', MarketerPostScheduleAPIView.as_view(),
         name='marketer-post-schedule'),
    path('marketer/posts/<int:pk>/generate-image/', MarketerPostGenerateImageAPIView.as_view(),
         name='marketer-post-generate-image'),
    path('marketer/posts/<int:pk>/image/', MarketerPostImageAPIView.as_view(),
         name='marketer-post-image'),
    path('marketer/posts/<int:pk>/reject/', MarketerPostRejectAPIView.as_view(),
         name='marketer-post-reject'),
    path('marketer/posts/<int:pk>/use-photo/', MarketerPostUsePhotoAPIView.as_view(),
         name='marketer-post-use-photo'),
    path('marketer/jobs/', MarketerJobListAPIView.as_view(), name='marketer-jobs'),
    path('marketer/jobs/<int:pk>/', MarketerJobDetailAPIView.as_view(), name='marketer-job'),
    path('marketer/photos/', MarketerPhotoListAPIView.as_view(), name='marketer-photos'),
    path('marketer/photos/<int:pk>/', MarketerPhotoDetailAPIView.as_view(), name='marketer-photo'),
]
