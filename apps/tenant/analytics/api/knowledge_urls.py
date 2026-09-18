"""Маршруты базы знаний ИИ (контракт 3б.7, №52) — одна строка include."""
from django.urls import path

from .knowledge import KnowledgeDetailAPIView, KnowledgeListCreateAPIView, KnowledgeTextAPIView

urlpatterns = [
    path('ai/knowledge/', KnowledgeListCreateAPIView.as_view(), name='ai-knowledge'),
    path('ai/knowledge/<int:pk>/', KnowledgeDetailAPIView.as_view(), name='ai-knowledge-detail'),
    path('ai/knowledge/<int:pk>/text/', KnowledgeTextAPIView.as_view(),
         name='ai-knowledge-text'),
]
