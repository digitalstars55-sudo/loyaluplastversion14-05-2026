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


def _is_quiet_hour(dt_local) -> bool:
    """
    True, если ЛОКАЛЬНОЕ (МСК) время dt_local попадает в тихие часы.
    Вынесено из _in_review_quiet_hours(), чтобы планировщик автоотправки
    мог проверить произвольный момент, а не только «сейчас».
    """
    hour = dt_local.hour
    start, end = REVIEW_PUSH_QUIET_START_HOUR, REVIEW_PUSH_QUIET_END_HOUR
    if start <= end:
        return start <= hour < end
    # окно через полночь (22..9): тихо, если час >= start ИЛИ < end
    return hour >= start or hour < end


def _in_review_quiet_hours() -> bool:
    """True, если сейчас тихие часы (МСК) — пуш об отзыве слать не нужно."""
    return _is_quiet_hour(timezone.localtime())


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
        # QuerySet.update() не трогает auto_now — а send_draft_reminders_task
        # отсчитывает reminder_minutes именно от updated_at. Без этой строки
        # напоминание «черновик готов» прилетало на ближайшем 30-минутном тике,
        # а не через 3 часа (замечено 08.09 на отзыве 538).
        updated_at=timezone.now(),
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


# ═══════════════════════════════════════════════════════════════════════════
# АВТООТПРАВКА ответов ИИ на ПОЗИТИВНЫЕ отзывы
# ═══════════════════════════════════════════════════════════════════════════
#
# Цепочка целиком:
#   ingest (submit_app_review / handle_vk_incoming_message)
#     → analyze_and_save (sentiment + ai_needs_human)
#     → auto_generate_draft_task (черновик ИИ)
#     → schedule_auto_send  ← ЗДЕСЬ решаем, отправлять ли самим
#     → auto_send_review_reply_task (eta = auto_send_at) → send_vk_reply
#
# ВСЁ работает только при ReviewAutoReplyConfig.auto_send_enabled=True.
# Дефолт False → на проде ничего не меняется: как и раньше, ИИ готовит
# черновик, а отправляет человек.


# Человекочитаемые причины (для веб-интерфейса и логов).
AUTO_SEND_REASON_LABELS: dict[str, str] = {
    'config_off':        'автоотправка выключена в настройках',
    'not_positive':      'отзыв не позитивный',
    'manual_reply':      'на отзыв уже ответил сотрудник',
    'rejected_draft':    'черновик отклонён сотрудником',
    'no_draft':          'нет черновика ИИ',
    'needs_human':       'в отзыве вопрос или просьба — нужен человек',
    'numeric_only':      'только оценка-цифра, без текста',
    'no_vk_sender':      'нет VK-адресата — писать некому',
    'branch_disabled':   'автоотправка выключена для этой точки',
    'daily_limit':       'исчерпан дневной лимит автоответов',
    'draft_changed':     'черновик изменили после планирования',
    'cancelled_by_user': 'отменено сотрудником',
    'schedule_failed':   'не удалось поставить задачу в очередь',
    'not_found':         'отзыв не найден',
    'quiet_hours':       'тихие часы — перенесено на утро',
}


def auto_send_reason_label(reason: str) -> str:
    """Русская расшифровка причины (для UI). Для vk_error: … отдаём текст ошибки."""
    reason = (reason or '').strip()
    if not reason:
        return ''
    if reason.startswith('vk_error:'):
        return 'ошибка отправки в ВК: ' + reason.split(':', 1)[1].strip()
    return AUTO_SEND_REASON_LABELS.get(reason, reason)


def draft_hash(text: str) -> str:
    """sha256 черновика — чтобы отправить РОВНО то, что было одобрено планировщиком."""
    import hashlib
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def compute_auto_send_time(now=None, delay_minutes: int = 15):
    """
    Плановое время автоответа: now + delay. Если попадает в тихие часы —
    переносим на ближайшие 09:00 МСК + delay (гостю ночью не пишем).

    Возвращает tz-aware datetime в локальной зоне проекта (Europe/Moscow).
    """
    from datetime import timedelta

    if now is None:
        now = timezone.now()
    delay = int(delay_minutes or 0)

    local = timezone.localtime(now)
    target = local + timedelta(minutes=delay)
    if not _is_quiet_hour(target):
        return target

    # Ближайшее «утро» (конец тихих часов) НЕ раньше target.
    naive_target = target.replace(tzinfo=None)
    naive_morning = naive_target.replace(
        hour=REVIEW_PUSH_QUIET_END_HOUR, minute=0, second=0, microsecond=0,
    )
    if naive_morning <= naive_target:
        naive_morning = naive_morning + timedelta(days=1)
    morning = timezone.make_aware(naive_morning, timezone.get_current_timezone())
    return morning + timedelta(minutes=delay)


def _has_meaningful_guest_text(conv) -> bool:
    """
    Есть ли у треда гостевое сообщение с осмысленным текстом (>3 букв после
    выброса цифр и пунктуации). «5», «10!!!», «👍» — не считаются.
    """
    import re
    try:
        texts = list(
            conv.messages.exclude(source='ADMIN_REPLY')
            .values_list('text', flat=True)
        )
    except Exception:
        # Не смогли проверить — не блокируем автоотправку по этой причине.
        return True
    for t in texts:
        letters = re.sub(r'[\W\d_]+', '', t or '', flags=re.UNICODE)
        if len(letters) > 3:
            return True
    return False


def _is_numeric_only(conv) -> bool:
    """Отзыв — голая оценка без текста (отвечать по сути нечем)."""
    comment = (getattr(conv, 'ai_comment', '') or '').strip()
    if comment.startswith('Оценка-цифра:'):
        return True
    return not _has_meaningful_guest_text(conv)


def _auto_sent_today_count() -> int:
    """Сколько автоответов ИИ уже ушло за текущие сутки по МСК."""
    from apps.tenant.branch.models import TestimonialMessage

    local_now = timezone.localtime()
    start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return TestimonialMessage.objects.filter(
        is_ai_generated=True,
        created_at__gte=start_local,
    ).count()


def auto_send_precheck(conv, cfg) -> tuple[bool, str]:
    """
    Все гварды автоотправки одним местом. Возвращает (можно_отправлять, причина).
    Причина пустая, если можно. Порядок — от дешёвых проверок к запросам в БД.
    """
    if cfg is None or not getattr(cfg, 'enabled', False):
        return (False, 'config_off')
    if not getattr(cfg, 'auto_send_enabled', False):
        return (False, 'config_off')

    if (getattr(conv, 'sentiment', '') or '').upper() != 'POSITIVE':
        return (False, 'not_positive')
    if getattr(conv, 'is_replied', False):
        return (False, 'manual_reply')
    if getattr(conv, 'ai_draft_rejected', False):
        return (False, 'rejected_draft')
    if not (getattr(conv, 'ai_draft', '') or '').strip():
        return (False, 'no_draft')
    if getattr(conv, 'ai_needs_human', False):
        return (False, 'needs_human')
    if _is_numeric_only(conv):
        return (False, 'numeric_only')
    if not str(getattr(conv, 'vk_sender_id', '') or '').strip():
        return (False, 'no_vk_sender')

    # Точка выключена явно. ВК-треды (branch=None) разрешены мастер-флагом.
    branch_id = getattr(conv, 'branch_id', None)
    if branch_id:
        bmap = getattr(cfg, 'auto_send_branch_enabled', None) or {}
        if bmap.get(str(branch_id)) is False or bmap.get(branch_id) is False:
            return (False, 'branch_disabled')

    limit = int(getattr(cfg, 'auto_send_daily_limit', 0) or 0)
    if limit > 0 and _auto_sent_today_count() >= limit:
        return (False, 'daily_limit')

    return (True, '')


def build_review_links_keyboard(conv) -> dict | None:
    """
    VK inline-клавиатура со ссылками «оставьте отзыв»: ссылки точки, а если
    кафе не определено (общий ВК-тред) — ссылки основной точки сети.
    None, если ссылок нет вовсе.
    """
    yandex = gis = ''
    if getattr(conv, 'branch_id', None):
        branch = getattr(conv, 'branch', None)
        if branch is not None:
            yandex = (getattr(branch, 'review_link_yandex', '') or '').strip()
            gis    = (getattr(branch, 'review_link_2gis', '') or '').strip()

    if not yandex and not gis:
        try:
            from apps.tenant.branch.api.services import get_fallback_review_links
            fb_yandex, fb_gis = get_fallback_review_links()
        except Exception:
            fb_yandex, fb_gis = '', ''
        yandex = (fb_yandex or '').strip()
        gis    = (fb_gis or '').strip()

    buttons = []
    if yandex:
        buttons.append([{'action': {'type': 'open_link', 'link': yandex, 'label': '⭐ Яндекс Карты'}}])
    if gis:
        buttons.append([{'action': {'type': 'open_link', 'link': gis, 'label': '⭐ 2ГИС'}}])
    if not buttons:
        return None
    return {'inline': True, 'buttons': buttons}


def _tenant_name(schema_name: str) -> str:
    """Название тенанта для текста пуша (не падает, если модель недоступна)."""
    try:
        from django_tenants.utils import get_tenant_model
        tenant = get_tenant_model().objects.filter(schema_name=schema_name).first()
        return tenant.name if tenant else schema_name
    except Exception:
        return schema_name


def _set_auto_send(conversation_id: int, status: str, reason: str = '', **extra) -> None:
    """Точечное обновление полей автоотправки (без save() всей модели)."""
    from apps.tenant.branch.models import TestimonialConversation
    fields = {'auto_send_status': status, 'auto_send_reason': (reason or '')[:120]}
    fields.update(extra)
    TestimonialConversation.objects.filter(pk=conversation_id).update(**fields)


def cancel_auto_send(conv, reason: str = 'manual_reply') -> bool:
    """
    Снять запланированный автоответ. Вызывается ИЗ ВСЕХ точек ручного вмешательства:
    ответ сотрудника (веб/мобилка/админка/ВК), отклонение черновика, кнопка «Отменить».

    Ничего не делает, если автоответ не был запланирован. Никогда не бросает —
    отмена не должна ронять основной сценарий (отправку ответа человеком).
    """
    try:
        from apps.tenant.branch.models import TestimonialConversation
        S = TestimonialConversation.AutoSendStatus

        if (getattr(conv, 'auto_send_status', '') or '') != S.SCHEDULED:
            return False
        # Условие в filter — защита от гонки с уже стартовавшей задачей.
        updated = TestimonialConversation.objects.filter(
            pk=conv.pk, auto_send_status=S.SCHEDULED,
        ).update(
            auto_send_status=S.CANCELLED,
            auto_send_reason=(reason or '')[:120],
        )
        if updated:
            conv.auto_send_status = S.CANCELLED
            conv.auto_send_reason = (reason or '')[:120]
            logger.info('auto_send отменён conv=%s reason=%s', conv.pk, reason)
        return bool(updated)
    except Exception:
        logger.warning('cancel_auto_send failed conv=%s', getattr(conv, 'pk', None), exc_info=True)
        return False


def schedule_auto_send(conversation_id: int, schema_name: str = '') -> dict:
    """
    Запланировать автоответ. Вызывается ТОЛЬКО из auto_generate_draft_task
    сразу после успешной генерации черновика. Должен выполняться внутри
    schema_context нужного тенанта.

    Возвращает {'scheduled': bool, 'reason': str, 'send_at': iso?}.
    """
    from datetime import timedelta
    from django.db import connection
    from apps.tenant.branch.models import (
        TestimonialConversation, ReviewAutoReplyConfig,
    )
    S = TestimonialConversation.AutoSendStatus

    schema_name = schema_name or connection.schema_name

    conv = (
        TestimonialConversation.objects.select_related('branch')
        .filter(pk=conversation_id).first()
    )
    if conv is None:
        return {'scheduled': False, 'reason': 'not_found'}

    cfg = ReviewAutoReplyConfig.get_singleton()
    ok, reason = auto_send_precheck(conv, cfg)
    if not ok:
        # config_off = владелец автоотправку не включал. Статус НЕ трогаем,
        # чтобы у таких тенантов поле оставалось пустым (и в UI ничего не лезло).
        if reason != 'config_off':
            _set_auto_send(conversation_id, S.SKIPPED, reason, auto_send_at=None)
        return {'scheduled': False, 'reason': reason}

    send_at = compute_auto_send_time(timezone.now(), cfg.auto_send_delay_minutes)
    _set_auto_send(
        conversation_id, S.SCHEDULED, '',
        auto_send_at=send_at,
        auto_send_draft_hash=draft_hash(conv.ai_draft),
    )

    try:
        from apps.tenant.analytics.tasks import auto_send_review_reply_task
        auto_send_review_reply_task.apply_async(
            args=[conversation_id, schema_name], eta=send_at,
        )
    except Exception:
        # Брокер недоступен — откатываем статус, чтобы отзыв не завис
        # «запланированным» навсегда, и вернулись обычные черновик+пуш.
        logger.warning(
            'schedule_auto_send: не удалось поставить задачу conv=%s', conversation_id,
            exc_info=True,
        )
        _set_auto_send(conversation_id, '', 'schedule_failed', auto_send_at=None)
        return {'scheduled': False, 'reason': 'schedule_failed'}

    delay = int(cfg.auto_send_delay_minutes or 0)
    preview = (conv.ai_draft or '')[:90]
    tenant_name = _tenant_name(schema_name)

    if _in_review_quiet_hours():
        # Ночью пуш «ИИ ответит в 09:15» не шлём — он уедет в 09:00,
        # чтобы у сотрудника всё равно осталось окно на отмену.
        push_at = send_at - timedelta(minutes=delay)
        try:
            from apps.tenant.analytics.tasks import auto_send_pending_push_task
            auto_send_pending_push_task.apply_async(
                args=[conversation_id, schema_name, send_at.isoformat(), preview],
                eta=push_at,
            )
            push_result = {'sent': 0, 'reason': 'deferred', 'push_at': push_at.isoformat()}
        except Exception:
            logger.warning('auto_send_pending_push_task dispatch failed', exc_info=True)
            push_result = {'sent': 0, 'reason': 'push_dispatch_failed'}
    else:
        push_result = push_auto_reply_pending(
            schema_name, tenant_name, conversation_id, send_at,
            preview=preview, branch_id=conv.branch_id,
        )

    logger.info(
        'auto_send запланирован conv=%s schema=%s at=%s',
        conversation_id, schema_name, send_at.isoformat(),
    )
    return {'scheduled': True, 'send_at': send_at.isoformat(), 'push': push_result}


def _log_auto_send_audit(conv, text: str) -> None:
    """AuditLog AUTO_REPLY_SENT от «ИИ-ассистента» (staff_id пустой — это не человек)."""
    try:
        from apps.tenant.branch.models import AuditLog
        AuditLog.objects.create(
            staff_id=None,
            staff_name='ИИ-ассистент',
            action_type=AuditLog.Action.AUTO_REPLY_SENT,
            target_type='review',
            target_id=str(conv.pk),
            target_label=str(conv)[:255],
            details=(text or '')[:500],
            delta={},
        )
    except Exception:
        logger.warning('AuditLog AUTO_REPLY_SENT failed conv=%s', conv.pk, exc_info=True)


def perform_auto_send(conversation_id: int, schema_name: str) -> dict:
    """
    Тело задачи auto_send_review_reply_task. Выполняется ВНУТРИ schema_context.
    Перепроверяет все гварды (за время окна отмены могло измениться что угодно).
    """
    from apps.tenant.branch.models import (
        TestimonialConversation, ReviewAutoReplyConfig,
    )
    S = TestimonialConversation.AutoSendStatus

    conv = (
        TestimonialConversation.objects.select_related('branch')
        .filter(pk=conversation_id).first()
    )
    if conv is None:
        return {'sent': False, 'reason': 'not_found'}

    # Отменили / уже отправили / перепланировали — задача-дубль просто уходит.
    if (conv.auto_send_status or '') != S.SCHEDULED:
        return {'sent': False, 'reason': 'status_' + (conv.auto_send_status or 'none')}

    cfg = ReviewAutoReplyConfig.get_singleton()
    tenant_name = _tenant_name(schema_name)

    ok, reason = auto_send_precheck(conv, cfg)
    if not ok:
        _set_auto_send(conversation_id, S.SKIPPED, reason)
        return {'sent': False, 'reason': reason}

    # Черновик правили после планирования — отправлять «не то» нельзя.
    if conv.auto_send_draft_hash and draft_hash(conv.ai_draft) != conv.auto_send_draft_hash:
        _set_auto_send(conversation_id, S.SKIPPED, 'draft_changed')
        push_draft_ready(schema_name, tenant_name, conversation_id)
        return {'sent': False, 'reason': 'draft_changed'}

    # Тихие часы (beat/воркер стартанули позже eta) — переносим на утро.
    if _in_review_quiet_hours():
        new_at = compute_auto_send_time(timezone.now(), cfg.auto_send_delay_minutes)
        _set_auto_send(conversation_id, S.SCHEDULED, '', auto_send_at=new_at)
        try:
            from apps.tenant.analytics.tasks import auto_send_review_reply_task
            auto_send_review_reply_task.apply_async(
                args=[conversation_id, schema_name], eta=new_at,
            )
        except Exception:
            logger.warning('auto_send reschedule failed conv=%s', conversation_id, exc_info=True)
        return {'sent': False, 'reason': 'quiet_hours', 'rescheduled_to': new_at.isoformat()}

    keyboard = build_review_links_keyboard(conv) if cfg.auto_send_attach_links else None
    text = (conv.ai_draft or '').strip()
    tail = (cfg.auto_send_links_text or '').strip()
    if keyboard and tail:
        text = f'{text}\n\n{tail}'

    from apps.tenant.branch.api.services import send_vk_reply
    try:
        send_vk_reply(
            conv, text,
            sender_name='ИИ-ассистент',
            keyboard=keyboard,
            is_ai_generated=True,
        )
    except Exception as e:
        _set_auto_send(conversation_id, S.FAILED, f'vk_error: {e}'[:120])
        logger.warning('auto_send: VK отказал conv=%s: %s', conversation_id, e)
        # Черновик остаётся — пусть человек ответит руками.
        push_draft_ready(schema_name, tenant_name, conversation_id)
        return {'sent': False, 'reason': 'vk_error', 'error': str(e)}

    _set_auto_send(conversation_id, S.SENT, '', auto_send_at=timezone.now())
    _log_auto_send_audit(conv, text)
    push_result = push_auto_reply_sent(
        schema_name, tenant_name, conversation_id,
        preview=text[:90], branch_id=conv.branch_id,
    )
    logger.info('auto_send ОТПРАВЛЕН conv=%s schema=%s', conversation_id, schema_name)
    return {'sent': True, 'push': push_result}


# ── Пуши автоотправки ─────────────────────────────────────────────────────────

def push_auto_reply_pending(
    schema_name: str,
    tenant_name: str,
    conversation_id: int,
    send_at,
    preview: str = '',
    branch_id: int | None = None,
) -> dict:
    """
    Push 'auto_reply_pending' — «ИИ ответит гостю в 14:35», окно на отмену.
    Тихие часы уважаем так же, как в push_draft_ready (журнал пишется всегда).
    """
    admin_users, tokens = _resolve_push_recipients(
        schema_name, 'auto_reply_pending', branch_id=branch_id,
    )

    try:
        at_human = timezone.localtime(send_at).strftime('%H:%M')
    except Exception:
        at_human = ''
    title = f'🤖 ИИ ответит гостю в {at_human}' if at_human else '🤖 ИИ ответит гостю'
    body = (preview or f'{tenant_name}: черновик готов, отправка по таймеру.')[:200]
    data = {
        'type': 'auto_reply_pending',
        'review_id': conversation_id,
        'send_at': send_at.isoformat() if hasattr(send_at, 'isoformat') else str(send_at),
    }

    from apps.shared.users.push import send_expo_push, log_notification
    log_notification(admin_users, 'auto_reply_pending', title, body, data)

    if _in_review_quiet_hours():
        return {'sent': 0, 'reason': 'quiet_hours'}
    if not tokens:
        return {'sent': 0, 'reason': 'no_tokens'}
    return send_expo_push(tokens=tokens, title=title, body=body, data=data)


def push_auto_reply_sent(
    schema_name: str,
    tenant_name: str,
    conversation_id: int,
    preview: str = '',
    branch_id: int | None = None,
) -> dict:
    """Push 'auto_reply_sent' — «ИИ ответил гостю» + начало текста."""
    admin_users, tokens = _resolve_push_recipients(
        schema_name, 'auto_reply_sent', branch_id=branch_id,
    )

    title = '🤖 ИИ ответил гостю'
    body = (preview or f'{tenant_name}: автоответ отправлен.')[:200]
    data = {'type': 'auto_reply_sent', 'review_id': conversation_id}

    from apps.shared.users.push import send_expo_push, log_notification
    log_notification(admin_users, 'auto_reply_sent', title, body, data)

    if _in_review_quiet_hours():
        return {'sent': 0, 'reason': 'quiet_hours'}
    if not tokens:
        return {'sent': 0, 'reason': 'no_tokens'}
    return send_expo_push(tokens=tokens, title=title, body=body, data=data)
