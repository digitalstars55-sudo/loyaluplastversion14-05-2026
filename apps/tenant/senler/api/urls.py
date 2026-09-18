"""
Маршруты JSON-API рассылок и авторассылок для внешнего кабинета CheckUp.

Подключаются в main/urls.py одной строкой с префиксом api/v1/ — рядом с
остальными тенантными API. Существующие маршруты (в т.ч.
analytics/rf/send-broadcast/, которым пользуется мобилка) не затронуты.

ВАЖЕН ПОРЯДОК: 'broadcasts/sends/' объявлен ДО 'broadcasts/<int:pk>/',
иначе история запусков рискует уехать в карточку черновика при любой
смене конвертера пути. По той же причине 'auto-broadcasts/events/' стоит
ПЕРВЫМ среди auto-broadcasts (int-конвертер слово events не съест, но
порядок не должен зависеть от этого).

⚠️ auto-broadcasts/* ПЕРЕКРЫВАЮТ одноимённые маршруты мобилки
(apps/tenant/mobile/api/urls.py:213-228): этот urls.py включён в main/urls.py
раньше (строка 32 против 39). Так и задумано — см. докстринг auto_broadcasts.py.
"""
from django.urls import path

from .auto_broadcasts import (
    AutoBroadcastEventsAPIView,
    AutoBroadcastRuleActivateAPIView,
    AutoBroadcastRuleDeactivateAPIView,
    AutoBroadcastRuleDetailAPIView,
    AutoBroadcastRuleListCreateAPIView,
    AutoBroadcastRuleLogAPIView,
    AutoBroadcastRulePreviewAPIView,
    AutoBroadcastRuleStatsAPIView,
    AutoBroadcastRuleTestSendAPIView,
    AutoBroadcastVariantCreateAPIView,
    AutoBroadcastVariantDetailAPIView,
)
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

    # ── Авторассылки (конструктор, контракт №31) ──────────────────────────────
    path('auto-broadcasts/events/',                    AutoBroadcastEventsAPIView.as_view(),         name='auto-broadcasts-events'),
    path('auto-broadcasts/',                           AutoBroadcastRuleListCreateAPIView.as_view(), name='auto-broadcasts-list'),
    path('auto-broadcasts/<int:pk>/variants/',         AutoBroadcastVariantCreateAPIView.as_view(),  name='auto-broadcasts-variants'),
    path('auto-broadcasts/<int:pk>/variants/<int:vid>/', AutoBroadcastVariantDetailAPIView.as_view(), name='auto-broadcasts-variant-detail'),
    path('auto-broadcasts/<int:pk>/preview/',          AutoBroadcastRulePreviewAPIView.as_view(),    name='auto-broadcasts-preview'),
    path('auto-broadcasts/<int:pk>/activate/',         AutoBroadcastRuleActivateAPIView.as_view(),   name='auto-broadcasts-activate'),
    path('auto-broadcasts/<int:pk>/deactivate/',       AutoBroadcastRuleDeactivateAPIView.as_view(), name='auto-broadcasts-deactivate'),
    path('auto-broadcasts/<int:pk>/log/',              AutoBroadcastRuleLogAPIView.as_view(),        name='auto-broadcasts-log'),
    path('auto-broadcasts/<int:pk>/stats/',            AutoBroadcastRuleStatsAPIView.as_view(),      name='auto-broadcasts-stats'),
    path('auto-broadcasts/<int:pk>/test-send/',        AutoBroadcastRuleTestSendAPIView.as_view(),   name='auto-broadcasts-test-send'),
    path('auto-broadcasts/<int:pk>/',                  AutoBroadcastRuleDetailAPIView.as_view(),     name='auto-broadcasts-detail'),
]
