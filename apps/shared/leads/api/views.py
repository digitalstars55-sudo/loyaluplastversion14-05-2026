"""
API для онбординга:
  POST   /api/v1/leads/                       — создать новый лид (без auth)
  GET    /api/v1/leads/{session_token}/       — получить состояние лида (без auth)
  PATCH  /api/v1/leads/{session_token}/       — обновить поля лида
  POST   /api/v1/leads/{session_token}/submit/ — финализировать (status → submitted)
  POST   /api/v1/leads/{session_token}/chat/   — Pack F3 заглушка (Anthropic)

Авторизация по session_token, не по JWT — онбординг идёт ДО логина.
"""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import Lead, LeadStatus
from .serializers import (
    LeadCreateSerializer,
    LeadDetailSerializer,
    LeadSubmitSerializer,
)


class LeadListCreateAPIView(APIView):
    """POST /api/v1/leads/  — создать новый лид"""
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        ser = LeadCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        lead = ser.save()
        return Response(LeadCreateSerializer(lead).data, status=status.HTTP_201_CREATED)


class LeadDetailAPIView(APIView):
    """GET / PATCH /api/v1/leads/{session_token}/"""
    permission_classes = [AllowAny]
    authentication_classes = []

    def get_lead(self, session_token: str) -> Lead:
        return get_object_or_404(Lead, session_token=session_token)

    def get(self, request, session_token: str):
        lead = self.get_lead(session_token)
        return Response(LeadDetailSerializer(lead).data)

    def patch(self, request, session_token: str):
        lead = self.get_lead(session_token)
        # Не разрешаем редактировать после подтверждения
        if lead.status not in (LeadStatus.DRAFT, LeadStatus.SUBMITTED):
            return Response(
                {'detail': 'Нельзя редактировать заявку в этом статусе'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        ser = LeadSubmitSerializer(lead, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(LeadDetailSerializer(lead).data)


class LeadSubmitAPIView(APIView):
    """POST /api/v1/leads/{session_token}/submit/  — финализировать"""
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, session_token: str):
        lead = get_object_or_404(Lead, session_token=session_token)
        if lead.status != LeadStatus.DRAFT:
            return Response(
                {'detail': f'Заявка уже в статусе «{lead.get_status_display()}»'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not lead.is_complete:
            return Response(
                {'detail': 'Заполните все обязательные поля', 'lead': LeadDetailSerializer(lead).data},
                status=status.HTTP_400_BAD_REQUEST,
            )
        lead.mark_submitted()
        # TODO(Pack F2.5): отправить уведомление супер-админу (push + email)
        return Response(LeadDetailSerializer(lead).data)


class LeadChatAPIView(APIView):
    """
    POST /api/v1/leads/{session_token}/chat/  body: {message: str}

    Реальный AI-чат через Anthropic Claude Haiku. Использует tool-use
    для атомарного обновления полей Lead.

    Возвращает:
      200: {assistant_message, lead_state, is_complete, updated_fields}
      503: если Anthropic недоступен — мобайл переходит на fallback-скрипт.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, session_token: str):
        from ..ai import run_chat_turn  # импорт здесь чтобы не ломать загрузку, если anthropic не установлен
        from .serializers import LeadDetailSerializer

        lead = get_object_or_404(Lead, session_token=session_token)
        if lead.status not in (LeadStatus.DRAFT, LeadStatus.SUBMITTED):
            return Response(
                {'detail': 'Заявка уже обработана, чат недоступен'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user_message = (request.data.get('message') or '').strip()

        # Append user message to history. Если пустое — это первый turn,
        # клиент только открыл чат и хочет приветствие AI.
        if user_message:
            lead.append_chat('user', user_message)

        try:
            assistant_text, updated_fields = run_chat_turn(lead, user_message)
        except Exception as e:
            # Не падаем на 500 — мобайл сможет переключиться на скрипт.
            import logging
            logging.getLogger(__name__).exception('AI onboarding chat failed: %s', e)
            return Response(
                {
                    'detail': 'AI временно недоступен. Используйте локальный сценарий.',
                    'fallback': True,
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # Сохраняем ответ ассистента в историю
        if assistant_text:
            lead.append_chat('assistant', assistant_text)

        # Refresh lead from DB чтобы отдать актуальное состояние (run_chat_turn
        # делает save через update_fields, поля могли поменяться)
        lead.refresh_from_db()

        return Response({
            'assistant_message': assistant_text,
            'lead_state': LeadDetailSerializer(lead).data,
            'is_complete': lead.is_complete,
            'updated_fields': updated_fields,
        })
