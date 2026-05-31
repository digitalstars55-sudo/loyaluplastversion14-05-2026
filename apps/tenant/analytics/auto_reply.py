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

logger = logging.getLogger(__name__)


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


def push_draft_ready(schema_name: str, tenant_name: str, conversation_id: int) -> dict:
    """
    Отправить push 'draft_ready' всем админам тенанта.
    Должен вызываться ПОСЛЕ генерации черновика.
    """
    from django_tenants.utils import schema_context
    from django.db.models import Q

    # PushToken и User лежат в public schema
    with schema_context('public'):
        from apps.shared.users.models import PushToken
        from django.contrib.auth import get_user_model
        User = get_user_model()

        # SU всегда получает push с любого тенанта — без привязки к companies
        # (раньше И-связка с companies__schema_name давала пуш SU только
        # с тенантов, явно включённых ему в companies M2M; обычно SU там пусто,
        # и юзер видел push только с одного тенанта где случайно был в companies).
        # network_admin/superadmin role — только с явно привязанных тенантов.
        admin_users = list(User.objects.filter(
            Q(is_superuser=True)
            | (
                (Q(role='network_admin') | Q(role='superadmin'))
                & Q(companies__schema_name=schema_name)
            ),
        ).distinct())
        tokens = list(
            PushToken.objects.filter(user__in=admin_users)
            .values_list('token', flat=True)
        )

    title = 'Готов AI-черновик'
    body = f'{tenant_name}: новый отзыв ждёт ответа. Черновик готов.'
    data = {'type': 'draft_ready', 'review_id': conversation_id}

    from apps.shared.users.push import send_expo_push, log_notification
    log_notification(admin_users, 'draft_ready', title, body, data)

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
    from django_tenants.utils import schema_context
    from django.db.models import Q

    with schema_context('public'):
        from apps.shared.users.models import PushToken
        from django.contrib.auth import get_user_model
        User = get_user_model()

        # SU всегда получает push с любого тенанта — без привязки к companies
        # (раньше И-связка с companies__schema_name давала пуш SU только
        # с тенантов, явно включённых ему в companies M2M; обычно SU там пусто,
        # и юзер видел push только с одного тенанта где случайно был в companies).
        # network_admin/superadmin role — только с явно привязанных тенантов.
        admin_users = list(User.objects.filter(
            Q(is_superuser=True)
            | (
                (Q(role='network_admin') | Q(role='superadmin'))
                & Q(companies__schema_name=schema_name)
            ),
        ).distinct())
        tokens = list(
            PushToken.objects.filter(user__in=admin_users)
            .values_list('token', flat=True)
        )

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
) -> dict:
    """
    Отправить push 'review_new' админам тенанта.
    Вызывается ТОЛЬКО при создании ПЕРВОГО сообщения в треде (новый отзыв).
    `source` — APP / VK_MESSAGE — для display и data routing на клиенте.
    """
    from django_tenants.utils import schema_context
    from django.db.models import Q

    with schema_context('public'):
        from apps.shared.users.models import PushToken
        from django.contrib.auth import get_user_model
        User = get_user_model()

        # SU всегда получает push с любого тенанта — без привязки к companies
        # (раньше И-связка с companies__schema_name давала пуш SU только
        # с тенантов, явно включённых ему в companies M2M; обычно SU там пусто,
        # и юзер видел push только с одного тенанта где случайно был в companies).
        # network_admin/superadmin role — только с явно привязанных тенантов.
        admin_users = list(User.objects.filter(
            Q(is_superuser=True)
            | (
                (Q(role='network_admin') | Q(role='superadmin'))
                & Q(companies__schema_name=schema_name)
            ),
        ).distinct())
        tokens = list(
            PushToken.objects.filter(user__in=admin_users)
            .values_list('token', flat=True)
        )

    label = 'из приложения' if source == 'APP' else 'из ВКонтакте'
    title = 'Новый отзыв'
    body = f'{tenant_name}: новый отзыв {label}. Открой и ответь.'
    data = {'type': 'review_new', 'review_id': conversation_id, 'source': source}

    from apps.shared.users.push import send_expo_push, log_notification
    log_notification(admin_users, 'review_new', title, body, data)

    if not tokens:
        return {'sent': 0, 'reason': 'no_tokens'}

    return send_expo_push(tokens=tokens, title=title, body=body, data=data)


def push_daily_codes(schema_name: str, tenant_name: str, body: str) -> dict:
    """
    Отправить push 'daily_codes' админам тенанта — утренняя сводка кодов дня.
    Вызывается из beat-таски push_daily_codes_task в 08:00 MSK.
    """
    from django_tenants.utils import schema_context
    from django.db.models import Q

    with schema_context('public'):
        from apps.shared.users.models import PushToken
        from django.contrib.auth import get_user_model
        User = get_user_model()

        # SU всегда получает push с любого тенанта — без привязки к companies
        # (раньше И-связка с companies__schema_name давала пуш SU только
        # с тенантов, явно включённых ему в companies M2M; обычно SU там пусто,
        # и юзер видел push только с одного тенанта где случайно был в companies).
        # network_admin/superadmin role — только с явно привязанных тенантов.
        admin_users = list(User.objects.filter(
            Q(is_superuser=True)
            | (
                (Q(role='network_admin') | Q(role='superadmin'))
                & Q(companies__schema_name=schema_name)
            ),
        ).distinct())
        tokens = list(
            PushToken.objects.filter(user__in=admin_users)
            .values_list('token', flat=True)
        )

    title = 'Коды дня обновлены'
    data = {'type': 'daily_codes'}

    from apps.shared.users.push import send_expo_push, log_notification
    log_notification(admin_users, 'daily_codes', title, body, data)

    if not tokens:
        return {'sent': 0, 'reason': 'no_tokens'}

    return send_expo_push(tokens=tokens, title=title, body=body[:250], data=data)