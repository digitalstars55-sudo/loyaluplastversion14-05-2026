"""
База знаний ИИ для кабинета CheckUp — контракт 3б.7 (№52).

Документы базы знаний (`KnowledgeBaseDocument`) уходят в Claude системным
контекстом при анализе отзывов и при генерации черновиков ответов. До сих пор
управлять ими можно было только из админки LoyalUP, и владелец не видел из
CheckUp, что именно «читает» ИИ.

Решения:
  • DELETE — это АРХИВ (`is_active=false`), файл и текст остаются. Удалять
    физически нельзя: документ мог участвовать в анализе тысяч отзывов, а
    восстановить его из ниоткуда потом невозможно. Архивные видны по
    `?include_archived=1`.
  • Принимаем только `.docx` и `.txt` — ровно то, из чего умеет извлекать
    текст `_extract_document_text`. PDF по-прежнему не поддерживается: молча
    загруженный PDF дал бы документ с пустым текстом, и ИИ «читал» бы пустоту.
  • `GET /{id}/text/` отдаёт не больше `TEXT_LIMIT` символов с флагом
    `truncated` — это ответ на вопрос «что видит ИИ», а не способ выкачать
    файл целиком в браузер кабинета.
  • Писать может `network_admin` и суперадмин, читать — обе роли.

Ошибки — единой формой `{code, detail}`. Миграций модуль не требует.
"""
from __future__ import annotations

import logging

from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers as drf_serializers
from rest_framework import status as http_status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.users.access import current_schema_name
from apps.tenant.analytics.models import KnowledgeBaseDocument

log = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_TITLE_LEN = 255
# Потолок файла: инструкции для ИИ — это текст, а не архив фотографий.
MAX_FILE_BYTES = 10 * 1024 * 1024
ALLOWED_EXTENSIONS = ('.docx', '.txt')
# Сколько текста отдаём в «что видит ИИ»: больше в кабинете всё равно не читают.
TEXT_LIMIT = 20000


def _error(code: str, detail: str, status_code: int, **extra):
    payload = {'code': code, 'detail': detail}
    payload.update(extra)
    return Response(payload, status=status_code)


def _iso(value):
    return value.isoformat() if value else None


def _may_write(user) -> bool:
    return bool(getattr(user, 'is_superuser', False)
                or getattr(user, 'is_superadmin', False)
                or getattr(user, 'is_network_admin', False))


def _page_params(request):
    try:
        limit = int(request.query_params.get('limit') or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    try:
        offset = int(request.query_params.get('offset') or 0)
    except (TypeError, ValueError):
        offset = 0
    return max(1, min(limit, MAX_LIMIT)), max(0, offset)


def _filename(doc) -> str:
    name = getattr(getattr(doc, 'file', None), 'name', '') or ''
    return name.rsplit('/', 1)[-1]


def doc_to_dict(doc) -> dict:
    text = doc.extracted_text or ''
    return {
        'id': doc.pk,
        'title': doc.title,
        'is_active': doc.is_active,
        'filename': _filename(doc),
        'has_text': bool(text.strip()),
        'char_count': len(text),
        'created_at': _iso(doc.created_at),
        'updated_at': _iso(doc.updated_at),
    }


def _doc_or_none(pk):
    return KnowledgeBaseDocument.objects.filter(pk=pk).first()


def _truthy(value) -> bool:
    return str(value).strip().lower() in ('1', 'true', 'yes')


# ── OpenAPI ──────────────────────────────────────────────────────────────────

_DOC_OUT = inline_serializer(name='KnowledgeDocument', fields={
    'id': drf_serializers.IntegerField(),
    'title': drf_serializers.CharField(),
    'is_active': drf_serializers.BooleanField(),
    'filename': drf_serializers.CharField(),
    'has_text': drf_serializers.BooleanField(),
    'char_count': drf_serializers.IntegerField(),
})
_LIST_OUT = inline_serializer(name='KnowledgeDocuments', fields={
    'total': drf_serializers.IntegerField(),
    'limit': drf_serializers.IntegerField(),
    'offset': drf_serializers.IntegerField(),
    'documents': drf_serializers.ListField(child=drf_serializers.DictField()),
})
_UPLOAD_IN = inline_serializer(name='KnowledgeUpload', fields={
    'title': drf_serializers.CharField(),
    'file': drf_serializers.FileField(help_text='.docx или .txt, до 10 МБ'),
})
_TEXT_OUT = inline_serializer(name='KnowledgeText', fields={
    'text': drf_serializers.CharField(),
    'truncated': drf_serializers.BooleanField(),
    'char_count': drf_serializers.IntegerField(),
})
_ERR = OpenApiResponse(inline_serializer(name='KnowledgeError', fields={
    'code': drf_serializers.CharField(),
    'detail': drf_serializers.CharField(),
}), description='invalid_payload (400) · role_not_allowed (403) · not_found (404)')


# ── ручки ────────────────────────────────────────────────────────────────────

class KnowledgeListCreateAPIView(APIView):
    """
    GET  /api/v1/ai/knowledge/?include_archived=1&limit=&offset=
    POST /api/v1/ai/knowledge/ — multipart: `file` (.docx/.txt ≤10 МБ) + `title`.

    Архивные документы в списке по умолчанию скрыты.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @extend_schema(parameters=[
        OpenApiParameter('include_archived', bool, description='показать архивные'),
        OpenApiParameter('limit', int), OpenApiParameter('offset', int),
    ], responses={200: _LIST_OUT}, tags=['v1'])
    def get(self, request):
        qs = KnowledgeBaseDocument.objects.all().order_by('-created_at')
        if not _truthy(request.query_params.get('include_archived')):
            qs = qs.filter(is_active=True)
        limit, offset = _page_params(request)
        total = qs.count()
        rows = list(qs[offset:offset + limit])
        return Response({'total': total, 'limit': limit, 'offset': offset,
                         'documents': [doc_to_dict(d) for d in rows]})

    @extend_schema(request=_UPLOAD_IN, responses={201: _DOC_OUT, 400: _ERR, 403: _ERR},
                   tags=['v1'])
    def post(self, request):
        if not _may_write(request.user):
            return _error('role_not_allowed',
                          'загружать документы может только администратор сети',
                          http_status.HTTP_403_FORBIDDEN)
        upload = request.FILES.get('file')
        if upload is None:
            return _error('invalid_payload', 'file: приложите файл .docx или .txt',
                          http_status.HTTP_400_BAD_REQUEST)
        name = (getattr(upload, 'name', '') or '').lower()
        if not name.endswith(ALLOWED_EXTENSIONS):
            return _error('invalid_payload',
                          'file: принимаем только .docx и .txt — из других форматов мы не '
                          'умеем извлекать текст, и ИИ получил бы пустой документ',
                          http_status.HTTP_400_BAD_REQUEST)
        size = getattr(upload, 'size', 0) or 0
        if size > MAX_FILE_BYTES:
            return _error('invalid_payload',
                          f'file: не больше {MAX_FILE_BYTES // (1024 * 1024)} МБ',
                          http_status.HTTP_400_BAD_REQUEST)
        # Без названия берём имя файла: пустой заголовок в списке кабинета
        # выглядит как сломанная запись.
        original_name = (getattr(upload, 'name', '') or '').rsplit('/', 1)[-1]
        title = (str(request.data.get('title') or '').strip() or original_name)[:MAX_TITLE_LEN]

        doc = KnowledgeBaseDocument.objects.create(title=title, file=upload, is_active=True)
        doc.refresh_from_db()
        log.info('knowledge doc uploaded: %s id=%s title=%r by=%s',
                 current_schema_name(), doc.pk, doc.title, getattr(request.user, 'username', ''))
        return Response(doc_to_dict(doc), status=http_status.HTTP_201_CREATED)


class KnowledgeDetailAPIView(APIView):
    """
    PATCH  /api/v1/ai/knowledge/{id}/ {title, is_active}
    DELETE /api/v1/ai/knowledge/{id}/ — архивирует (файл и текст остаются).
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: _DOC_OUT, 404: _ERR}, tags=['v1'])
    def get(self, request, pk: int):
        doc = _doc_or_none(pk)
        if doc is None:
            return _error('not_found', 'Документ не найден', http_status.HTTP_404_NOT_FOUND)
        return Response(doc_to_dict(doc))

    @extend_schema(request=_DOC_OUT, responses={200: _DOC_OUT, 400: _ERR, 403: _ERR, 404: _ERR},
                   tags=['v1'])
    def patch(self, request, pk: int):
        if not _may_write(request.user):
            return _error('role_not_allowed', 'менять документы может только администратор сети',
                          http_status.HTTP_403_FORBIDDEN)
        doc = _doc_or_none(pk)
        if doc is None:
            return _error('not_found', 'Документ не найден', http_status.HTTP_404_NOT_FOUND)
        data = request.data or {}
        allowed = ('title', 'is_active')
        unknown = [k for k in data.keys() if k not in allowed]
        if unknown:
            return _error('invalid_payload', 'нельзя менять здесь: ' + ', '.join(sorted(unknown)),
                          http_status.HTTP_400_BAD_REQUEST, editable=list(allowed))
        fields = []
        if 'title' in data:
            title = str(data.get('title') or '').strip()
            if not title:
                return _error('invalid_payload', 'title: название не может быть пустым',
                              http_status.HTTP_400_BAD_REQUEST)
            doc.title = title[:MAX_TITLE_LEN]
            fields.append('title')
        if 'is_active' in data:
            raw = data.get('is_active')
            doc.is_active = raw if isinstance(raw, bool) else _truthy(raw)
            fields.append('is_active')
        if not fields:
            return _error('invalid_payload', 'нечего менять: title или is_active',
                          http_status.HTTP_400_BAD_REQUEST)
        doc.save(update_fields=sorted(fields) + ['updated_at'])
        log.info('knowledge doc patched: %s id=%s fields=%s', current_schema_name(), doc.pk,
                 sorted(fields))
        return Response(doc_to_dict(doc))

    @extend_schema(responses={204: OpenApiResponse(description='архивирован'), 403: _ERR,
                              404: _ERR}, tags=['v1'])
    def delete(self, request, pk: int):
        if not _may_write(request.user):
            return _error('role_not_allowed', 'архивировать документы может только '
                                              'администратор сети',
                          http_status.HTTP_403_FORBIDDEN)
        doc = _doc_or_none(pk)
        if doc is None:
            return _error('not_found', 'Документ не найден', http_status.HTTP_404_NOT_FOUND)
        # Архив, а не удаление: документ участвовал в анализе отзывов, и
        # восстановить его потом будет неоткуда.
        if doc.is_active:
            doc.is_active = False
            doc.save(update_fields=['is_active', 'updated_at'])
        log.info('knowledge doc archived: %s id=%s', current_schema_name(), doc.pk)
        return Response(status=http_status.HTTP_204_NO_CONTENT)


class KnowledgeTextAPIView(APIView):
    """GET /api/v1/ai/knowledge/{id}/text/ — что из документа видит ИИ."""
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: _TEXT_OUT, 404: _ERR}, tags=['v1'])
    def get(self, request, pk: int):
        doc = _doc_or_none(pk)
        if doc is None:
            return _error('not_found', 'Документ не найден', http_status.HTTP_404_NOT_FOUND)
        text = doc.extracted_text or ''
        return Response({'text': text[:TEXT_LIMIT], 'truncated': len(text) > TEXT_LIMIT,
                         'char_count': len(text)})
