"""
Маршруты точек контакта (№26/27 контракта платформы).

Отдельный urls-модуль, чтобы подключение стоило одной строки в main/urls.py и
не задевало существующие наборы маршрутов.
"""
from django.urls import path

from .contact_points import (
    BranchMaterialsAPIView,
    ContactPointBatchTablesAPIView,
    ContactPointDetailAPIView,
    ContactPointGuestsAPIView,
    ContactPointListCreateAPIView,
)

urlpatterns = [
    path('contact-points/', ContactPointListCreateAPIView.as_view(),
         name='contact-points'),
    # Пакет столов — до <int:pk>, чтобы путь не съел слово batch-tables.
    path('contact-points/batch-tables/', ContactPointBatchTablesAPIView.as_view(),
         name='contact-points-batch-tables'),
    path('contact-points/<int:pk>/', ContactPointDetailAPIView.as_view(),
         name='contact-point-detail'),
    path('contact-points/<int:pk>/guests/', ContactPointGuestsAPIView.as_view(),
         name='contact-point-guests'),
    # Материалы точки (№27) живут рядом с её карточкой в mobile/branches/.
    path('mobile/branches/<int:pk>/materials/', BranchMaterialsAPIView.as_view(),
         name='mobile-branch-materials'),
]
