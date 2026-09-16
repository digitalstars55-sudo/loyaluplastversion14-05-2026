"""
Маршруты JSON-API рассылок для внешнего кабинета CheckUp.

Подключаются в main/urls.py одной строкой с префиксом api/v1/ — рядом с
остальными тенантными API. Существующие маршруты (в т.ч.
analytics/rf/send-broadcast/, которым пользуется мобилка) не затронуты.

ВАЖЕН ПОРЯДОК: 'broadcasts/sends/' объявлен ДО 'broadcasts/<int:pk>/',
иначе история запусков рискует уехать в карточку черновика при любой
смене конвертера пути.
"""
from django.urls import path

from .broadcasts import (
    BroadcastDraftDetailAPIView,
    BroadcastDraftListCreateAPIView,
    BroadcastDraftPreviewAPIView,
    BroadcastDraftSendAPIView,
    BroadcastSendCancelAPIView,
    BroadcastSendDeleteInVKAPIView,
    BroadcastSendEditInVKAPIView,
    BroadcastSendListAPIView,
)

urlpatterns = [
    path('broadcasts/',                             BroadcastDraftListCreateAPIView.as_view(), name='broadcasts-list'),
    path('broadcasts/sends/',                       BroadcastSendListAPIView.as_view(),        name='broadcasts-sends'),
    path('broadcasts/sends/<int:pk>/cancel/',       BroadcastSendCancelAPIView.as_view(),      name='broadcasts-send-cancel'),
    path('broadcasts/sends/<int:pk>/edit-in-vk/',   BroadcastSendEditInVKAPIView.as_view(),    name='broadcasts-send-edit-in-vk'),
    path('broadcasts/sends/<int:pk>/delete-in-vk/', BroadcastSendDeleteInVKAPIView.as_view(),  name='broadcasts-send-delete-in-vk'),
    path('broadcasts/<int:pk>/',                    BroadcastDraftDetailAPIView.as_view(),     name='broadcasts-detail'),
    path('broadcasts/<int:pk>/preview/',            BroadcastDraftPreviewAPIView.as_view(),    name='broadcasts-preview'),
    path('broadcasts/<int:pk>/send/',               BroadcastDraftSendAPIView.as_view(),       name='broadcasts-send'),
]
