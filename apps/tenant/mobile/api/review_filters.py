"""
Серверные фильтры и пагинация ленты отзывов (`GET /api/v1/mobile/reviews/`).

Зачем отдельным модулем: до 16.09.2026 лента отдавала все треды сети разом, а
фильтровала мобилка у себя. Для экрана CheckUp (контракт платформы, ручка №1)
нужны фильтры на сервере и `limit/offset`. Всё аддитивно: без новых параметров
ответ ровно тот же, что раньше (весь список в `reviews`), к нему лишь
добавляются `total`, `limit`, `offset`.

Чистые функции над QuerySet и словарём query-параметров — тестируются без
запросов в базу (смотрим на собранный SQL).
"""
from __future__ import annotations

from django.db.models import Exists, OuterRef, Q

# Синонимы значений — чтобы и мобилка, и CheckUp могли писать как удобно.
_SENTIMENTS = {'POSITIVE', 'NEGATIVE', 'PARTIALLY_NEGATIVE', 'NEUTRAL', 'SPAM', 'WAITING'}
_SENTIMENT_ALIASES = {
    'positive': 'POSITIVE', 'negative': 'NEGATIVE', 'partially_negative': 'PARTIALLY_NEGATIVE',
    'partial': 'PARTIALLY_NEGATIVE', 'neutral': 'NEUTRAL', 'spam': 'SPAM', 'waiting': 'WAITING',
    # «весь негатив» одним словом — так считает «негатив без ответа» дашборд
    'bad': ('NEGATIVE', 'PARTIALLY_NEGATIVE'),
}
_SOURCE_ALIASES = {
    'app': 'APP', 'vk': 'VK_MESSAGE', 'vk_message': 'VK_MESSAGE', 'admin_reply': 'ADMIN_REPLY',
}
_STATUSES = ('unread', 'replied', 'unanswered', 'all')
_CHECKUP_STATUSES = ('in_progress', 'resolved', 'rejected', 'none', 'any')

MAX_LIMIT = 200


def _split(raw: str | None) -> list[str]:
    return [p.strip() for p in str(raw or '').split(',') if p.strip()]


def parse_sentiments(raw: str | None) -> list[str]:
    """`sentiment=NEGATIVE,PARTIALLY_NEGATIVE` или `sentiment=bad` → список значений модели. Неизвестное — пропускаем."""
    out: list[str] = []
    for token in _split(raw):
        key = token.lower()
        value = _SENTIMENT_ALIASES.get(key, token.upper())
        values = value if isinstance(value, tuple) else (value,)
        for v in values:
            if v in _SENTIMENTS and v not in out:
                out.append(v)
    return out


def parse_sources(raw: str | None) -> list[str]:
    out: list[str] = []
    for token in _split(raw):
        value = _SOURCE_ALIASES.get(token.lower(), token.upper())
        if value in ('APP', 'VK_MESSAGE', 'ADMIN_REPLY') and value not in out:
            out.append(value)
    return out


def parse_status(raw: str | None) -> str:
    value = str(raw or '').strip().lower()
    return value if value in _STATUSES else 'all'


def parse_checkup_status(raw: str | None) -> str:
    value = str(raw or '').strip().lower()
    return value if value in _CHECKUP_STATUSES else 'any'


def parse_pagination(params) -> tuple[int | None, int]:
    """
    `limit` (1..200) и `offset` (>=0). Нет `limit` → None = весь список, как раньше.
    Мусор в параметрах не роняет запрос — считается как «не задано».
    """
    limit = None
    raw_limit = params.get('limit')
    if raw_limit not in (None, ''):
        try:
            limit = max(1, min(int(raw_limit), MAX_LIMIT))
        except (TypeError, ValueError):
            limit = None
    offset = 0
    raw_offset = params.get('offset')
    if raw_offset not in (None, ''):
        try:
            offset = max(0, int(raw_offset))
        except (TypeError, ValueError):
            offset = 0
    return limit, offset


def apply_review_filters(qs, params):
    """
    Накладывает на QuerySet тредов серверные фильтры из query-параметров:

      sentiment=NEGATIVE,PARTIALLY_NEGATIVE | bad    — тональность треда
      status=unread | replied | unanswered            — ждёт ответа / отвечен / без ответа
      source=app | vk                                 — есть ли в треде сообщение такого источника
      checkup_status=in_progress|resolved|rejected|none — статус жалобы в CheckUp
      q=<строка>                                      — по VK ID отправителя или имени гостя

    RBAC по точкам, branch_ids и period накладывает вызывающий код до этого.
    """
    sentiments = parse_sentiments(params.get('sentiment'))
    if sentiments:
        qs = qs.filter(sentiment__in=sentiments)

    status = parse_status(params.get('status'))
    if status == 'unread':
        qs = qs.filter(has_unread=True, is_replied=False)
    elif status == 'replied':
        qs = qs.filter(is_replied=True)
    elif status == 'unanswered':
        qs = qs.filter(is_replied=False)

    sources = parse_sources(params.get('source'))
    if sources:
        from apps.tenant.branch.models import TestimonialMessage
        qs = qs.filter(Exists(
            TestimonialMessage.objects.filter(conversation_id=OuterRef('pk'), source__in=sources)
        ))

    checkup = parse_checkup_status(params.get('checkup_status'))
    if checkup == 'none':
        qs = qs.filter(checkup_status='')
    elif checkup != 'any':
        qs = qs.filter(checkup_status=checkup)

    query = str(params.get('q') or '').strip()[:80]
    if query:
        qs = qs.filter(
            Q(vk_sender_id__icontains=query)
            | Q(client__client__first_name__icontains=query)
            | Q(client__client__last_name__icontains=query)
            | Q(vk_guest__first_name__icontains=query)
            | Q(vk_guest__last_name__icontains=query)
        )
    return qs
