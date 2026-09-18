"""
Маршруты настроек механики «Игра через сториз» (контракт платформы, 3б.3 / №23).

Отдельный urls-модуль: подключение стоит одной строки в main/urls.py и не
задевает существующие наборы маршрутов.
"""
from django.urls import path

from .feature_flags import FeatureFlagsAPIView
from .story_settings import BranchStorySettingsAPIView, NetworkStorySettingsAPIView

urlpatterns = [
    path('settings/story/', NetworkStorySettingsAPIView.as_view(), name='settings-story'),
    path('mobile/branches/<int:pk>/story/', BranchStorySettingsAPIView.as_view(),
         name='mobile-branch-story'),
    # Флаги механик сети (№56) — только чтение; лежит здесь, чтобы не плодить
    # ещё один include в main/urls.py.
    path('settings/features/', FeatureFlagsAPIView.as_view(), name='settings-features'),
]
