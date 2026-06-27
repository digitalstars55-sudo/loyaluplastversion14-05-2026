"""
Авто-ответ AI: при поступлении нового отзыва (после AI-классификации)
готовит черновик через Claude и шлёт push админу тенанта.

Логика «когда генерим»:
- Включён master-toggle (ReviewAutoReplyConfig.enabled)
- Sentiment не выключен в sentiment_enabled
- Точка не выключена явно в branch_enabled (если вообще задано)
- Админ ещё не ответил
- Черновик ещё не сгенерён или предыдущий не отвергнут
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from django.utils import timezone

logger = logging.getLogger(__name__)

# Тихие часы для пушей об отзывах (по TIME_ZONE проекта = Europe/Moscow):
# с 22:00 до 09:00 push не шлём, чтобы не будить владельца ночью пачками.
# Запись в журнал уведомлений ОСТАЁТСЯ — владелец увидит отзыв утром в приложении.
REVIEW_PUSH_QUIET_START_HOUR = int(os.getenv('REVIEW_PUSH_QUIET_START', '22'))  # включительно
REVIEW_PUSH_QUIET_END_HOUR = int(os.getenv('REVIEW_PUSH_QUIET_END', '9'))       # до этого часа


def _in_review_quiet_hours() -> bool:
    """True, если сейчас тихие часы (МСК) — пуш об отзыве слать не нужно."""
    hour = timezone.localtime().hour
    start, end = REVIEW_PUSH_QUIET_START_HOUR, REVIEW_PUSH_QUIET_END_HOUR
    if start <= end:
        return start <= hour < end
    # окно через полночь (22..9): тихо, если час >= start ИЛИ < end
    return hour >= start or hour < end


def maybe_generate_auto_draft(conversation_id: int) -> Optional[str]:
    """
    Возвращает текст черновика, если все условия пройдены.
    Должен вызываться внутри schema_context(tenant_schema).
    Сохраняет результат в conv.ai_draft.
    """
    from apps.tenant.branch.models import (
        TestimonialConversation, ReviewAutoReplyConfig,
    )

    try:
        conv = TestimonialConversation.objects.select_related('branch').get(pk=conversation_id)
    except TestimonialConversation.DoesNotExist:
        return None

    # Уже отвечено админом — не нужен черновик
    if conv.is_replied:
        return None
    # Уже есть актуальный черновик и он не отвергнут
    if conv.ai_draft and not conv.ai_draft_rejected:
        return None
    # Черновик отвергнут админом — не перегенерируем без явного запроса
    if conv.ai_draft_rejected:
        return None

    cfg = ReviewAutoReplyConfig.get_singleton()
    if not cfg.enabled:
        return None

    # Sentiment-фильтр (поля модели — отдельные булевые)
    sent_key = (conv.sentiment or '').upper()
    SENTIMENT_FIELD = {
        'POSITIVE':           'sentiment_positive',
        'NEGATIVE':           'sentiment_negative',
        'PARTIALLY_NEGATIVE': 'sentiment_partially_negative',
        'NEUTRAL':            'sentiment_neutral',
        'PENDING':            'sentiment_pending',
    }
    if sent_key == 'SPAM':
        return None
    if sent_key in ('', 'WAITING'):
        return None
    field = SENTIMENT_FIELD.get(sent_key)
    if field is None or not getattr(cfg, field, True):
        return None

    # Branch-фильтр
    branch_map = cfg.branch_enabled or {}
    if conv.branch_id and branch_map:
        # Поддержим оба варианта ключей: int и str (JSON приходит со str)
        bid = conv.branch_id
        if branch_map.get(bid) is False or branch_map.get(str(bid)) is False:
            return None

    text = _call_claude_for_draft(conv, cfg.ai_tone)
    if not text:
        return None

    # Сохраняем
    TestimonialConversation.objects.filter(pk=conversation_id).update(
        ai_draft=text,
        ai_draft_rejected=False,
    )
    return text


def _call_claude_for_draft(conv, ai_tone: str) -> Optional[str]:
    """Вызов Anthropic Claude. Возвращает текст черновика или None."""
    from django.conf import settings
    from django.db import connection

    api_key = getattr(settings, 'ANTHROPIC_API_KEY', None)
    if not api_key:
        logger.warning('auto_reply: no ANTHROPIC_API_KEY')
        return None

    try:
        import anthropic
    except ImportError:
        logger.warning('auto_reply: anthropic package not installed')
        return None

    tone_human = {
        'formal':   'официальный, вежливый',
        'friendly': 'дружелюбный, тёплый',
        'neutral':  'нейтральный, профессиональный',
    }.get(ai_tone or 'friendly', 'дружелюбный')

    company_name = getattr(connection.tenant, 'name', 'наше заведение')
    sentiment_human = (
        conv.get_sentiment_display() if conv.sentiment else 'не определён'
    )

    msgs = list(conv.messages.order_by('created_at').values('source', 'text'))
    thread = '\n'.join(
        f"[{m['source']}] {m['text']}" for m in msgs if (m.get('text') or '').strip()
    )
    if not thread:
        return None

    system_prompt = (
        'Ты — менеджер заведения, отвечающий на отзывы гостей.\n'
        'Правила:\n'
        f'- Пиши на русском, тон: {tone_human}.\n'
        '- По умолчанию коротко (3-4 предложения), без воды. Если в треде менеджер '
        'явно просит написать подробный ответ — выполни, до 4000 символов.\n'
        '- Без markdown, HTML и эмодзи (максимум один по необходимости).\n'
        '- Обращайся на «Вы».\n'
        '- Если негатив — извинись, не оправдывайся, предложи решение.\n'
        '- Если позитив — поблагодари искренне, без шаблонов.\n'
        '- Не упоминай скидки/компенсации без явной просьбы.\n'
        '- Верни ТОЛЬКО текст ответа, без пояснений и подписи.'
    )

    # Подмешиваем инструкции из базы знаний тенанта (тон, факты о заведении,
    # типовые формулировки). Без этого Claude отвечает в отрыве от контекста.
    from apps.tenant.analytics.ai_service import _get_knowledge_base_text
    kb_text = _get_knowledge_base_text()
    if kb_text:
        system_prompt += (
            '\n\n--- Инструкции из базы знаний заведения ---\n'
            + kb_text
        )
    user_message = (
        f'Заведение: {company_name}\n'
        f'Тональность отзыва: {sentiment_human}\n\n'
        f'Тред:\n{thread}\n\n'
        f'Напиши черновик ответа от имени заведения.'
    )

    proxy_url = os.getenv('AI_PROXY_URL', '')
    client = (
        anthropic.Anthropic(api_key=api_key, base_url=proxy_url)
        if proxy_url else anthropic.Anthropic(api_key=api_key)
    )
    try:
        message = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=2048,
            system=system_prompt,
            messages=[{'role': 'user', 'content': user_message}],
        )
        return message.content[0].text.strip()
    except Exception as e:
        logger.warning('Claude draft generation failed for conv %s: %s', conv.pk, e)
        return None


def _resolve_push_recipients(
    schema_name: str,
    push_type: str,
    branch_id: int | None = None,
) -> tuple[list, list[str]]:
    """
    Возвращает (admin_users, tokens) с учётом:
      - SU всегда получает push с любого тенанта
      - network_admin/superadmin role — только если companies содержит этот тенант
      - per-user push_prefs фильтр: типы + тенанты (см. is_push_allowed)
      - per-user branch_access фильтр: если push привязан к branch_id, исключаем
        тех, у кого нет доступа к этой точке (NULL/общетенантный push — всем)

    PushToken и User лежат в public schema — оборачиваем в schema_context.
    """
    from django_tenants.utils import schema_context
    from django.db.models import Q

    with schema_context('public'):
        from apps.shared.users.models import PushToken
        from apps.shared.users.push import filter_users_by_prefs
        from apps.shared.users.access import user_allowed_branches
        from django.contrib.auth import get_user_model
        User = get_user_model()

        admin_users = list(User.objects.filter(
            Q(is_superuser=True)
            | (
                (Q(role='network_admin') | Q(role='superadmin'))
                & Q(companies__schema_name=schema_name)
            ),
        ).distinct())

        # Per-user prefs: исключаем тех, кто отключил этот тенант или тип
        admin_users = filter_users_by_prefs(admin_users, schema_name, push_type)

        # Per-user branch_access: если push привязан к конкретной точке,
        # исключаем тех, у кого нет к ней доступа.
        if branch_id is not None:
            def _has_branch(u):
                allowed = user_allowed_branches(u, schema_name)
                return allowed is None or branch_id in allowed
            admin_users = [u for u in admin_users if _has_branch(u)]

        tokens = list(
            PushToken.objects.filter(user__in=admin_users)
            .values_list('token', flat=True)
        )
    return admin_users, tokens


def push_draft_ready(schema_name: str, tenant_name: str, conversation_id: int) -> dict:
    """
    Отправить push 'draft_ready' всем админам тенанта.
    Должен вызываться ПОСЛЕ генерации черновика.
    """
    admin_users, tokens = _resolve_push_recipients(schema_name, 'draft_ready')

    title = 'Готов AI-черновик'
    body = f'{tenant_name}: новый отзыв ждёт ответа. Черновик готов.'
    data = {'type': 'draft_ready', 'review_id': conversation_id}

    from apps.shared.users.push import send_expo_push, log_notification
    log_notification(admin_users, 'draft_ready', title, body, data)

    # Тихие часы: тоже относится к отзывам — ночью не будим (журнал остаётся).
    if _in_review_quiet_hours():
        return {'sent': 0, 'reason': 'quiet_hours'}

    if not tokens:
        return {'sent': 0, 'reason': 'no_tokens'}

    return send_expo_push(tokens=tokens, title=title, body=body, data=data)


def push_chat_message(
    schema_name: str,
    tenant_name: str,
    message_id: int,
    manager_name: str,
    preview: str = '',
) -> dict:
    """
    Отправить push 'chat_message' админам тенанта.
    Вызывается из inbound-reply (CheckUp → LoyalUP) когда менеджер ответил.
    """
    admin_users, tokens = _resolve_push_recipients(schema_name, 'chat_message')

    body_text = (preview if preview else 'Новое сообщение в саппорт-чате')[:200]
    title = manager_name or 'Менеджер'
    data = {'type': 'chat_message', 'message_id': message_id, 'tenant_name': tenant_name}

    from apps.shared.users.push import send_expo_push, log_notification
    log_notification(admin_users, 'chat_message', title, body_text, data)

    if not tokens:
        return {'sent': 0, 'reason': 'no_tokens'}

    return send_expo_push(tokens=tokens, title=title, body=body_text, data=data)

def push_review_new(
    schema_name: str,
    tenant_name: str,
    conversation_id: int,
    source: str = 'APP',
    branch_id: int | None = None,
) -> dict:
    """
    Отправить push 'review_new' админам тенанта.
    Вызывается ТОЛЬКО при создании ПЕРВОГО сообщения в треде (новый отзыв).
    `source` — APP / VK_MESSAGE — для display и data routing на клиенте.
    `branch_id` — если передан, push идёт только тем у кого есть RBAC-доступ.
    """
    admin_users, tokens = _resolve_push_recipients(schema_name, 'review_new', branch_id=branch_id)

    label = 'из приложения' if source == 'APP' else 'из ВКонтакте'
    title = 'Новый отзыв'
    body = f'{tenant_name}: новый отзыв {label}. Открой и ответь.'
    data = {'type': 'review_new', 'review_id': conversation_id, 'source': source}

    from apps.shared.users.push import send_expo_push, log_notification
    log_notification(admin_users, 'review_new', title, body, data)

    # Тихие часы: запись в журнал уже сделана, но ночью владельца не будим.
    if _in_review_quiet_hours():
        return {'sent': 0, 'reason': 'quiet_hours'}

    if not tokens:
        return {'sent': 0, 'reason': 'no_tokens'}

    return send_expo_push(tokens=tokens, title=title, body=body, data=data)


def push_daily_codes(schema_name: str, tenant_name: str, body: str) -> dict:
    """
    Отправить push 'daily_codes' админам тенанта — утренняя сводка кодов дня.
    Вызывается из beat-таски push_daily_codes_task в 08:00 MSK.
    """
    admin_users, tokens = _resolve_push_recipients(schema_name, 'daily_code')

    title = 'Коды дня обновлены'
    data = {'type': 'daily_codes'}

    from apps.shared.users.push import send_expo_push, log_notification
    log_notification(admin_users, 'daily_codes', title, body, data)

    if not tokens:
        return {'sent': 0, 'reason': 'no_tokens'}

    return send_expo_push(tokens=tokens, title=title, body=body[:250], data=data)