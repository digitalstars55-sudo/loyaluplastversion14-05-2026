"""
Маршруты «подключение ВКонтакте» для кабинета CheckUp (контракт платформы, №55).

Отдельный urls-модуль по образцу story_settings_urls.py: подключение стоит одной
строки в main/urls.py и не задевает существующие наборы маршрутов — в частности
`branch/api/urls.py`, где живёт приёмник ВК `POST /api/v1/vk/callback/`.
"""
from django.urls import path

from .vk_connect import BranchVkAPIView, BranchVkCheckAPIView, NetworkVkSettingsAPIView

urlpatterns = [
    path('settings/vk/', NetworkVkSettingsAPIView.as_view(), name='settings-vk'),
    path('mobile/branches/<int:pk>/vk/', BranchVkAPIView.as_view(), name='mobile-branch-vk'),
    path('mobile/branches/<int:pk>/vk/check/', BranchVkCheckAPIView.as_view(),
         name='mobile-branch-vk-check'),
]
