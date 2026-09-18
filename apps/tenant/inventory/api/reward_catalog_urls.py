"""
Маршруты «Каталога наград» для кабинета CheckUp (контракт платформы, №49).

Отдельный urls-модуль по образцу contact_points_urls.py: подключение стоит
одной строки в main/urls.py и не задевает гостевые маршруты инвентаря
(`apps.tenant.inventory.api.urls`).

⚠️ Строка в main/urls.py обязана стоять ВЫШЕ `apps.tenant.analytics.api.urls`:
пути совпадают, и Django отдаёт запрос ПЕРВОМУ совпавшему include. Так новый
модуль перекрывает старую вьюху `RFMRewardCatalogAPIView`
(analytics/api/views.py:1746), не трогая её код. Опустить строку ниже —
значит тихо вернуть старый ответ без POST/PATCH/DELETE.
"""
from django.urls import path

from .reward_catalog import RewardCatalogDetailAPIView, RewardCatalogListCreateAPIView

urlpatterns = [
    path('analytics/rf/reward-catalog/', RewardCatalogListCreateAPIView.as_view(),
         name='rf-reward-catalog'),
    path('analytics/rf/reward-catalog/<int:pk>/', RewardCatalogDetailAPIView.as_view(),
         name='rf-reward-catalog-detail'),
]
