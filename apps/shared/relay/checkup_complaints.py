"""
Отправка ЖАЛОБ гостя из LoyalUP в CheckUp.

Назначение на проде: /home/levone/levelup-back/apps/shared/relay/checkup_complaints.py

────────────────────────────────────────────────────────────────────────
Что это
────────────────────────────────────────────────────────────────────────
У владельца две системы на одной машине: LoyalUP (лояльность, отзывы
гостей) и CheckUp (операционка сети). Жалоба гостя должна попадать в
реестр «Отзывы/Жалобы» CheckUp — там она получает ответственных, вердикт
и, при необходимости, удержание. Здесь — ИСХОДЯЩАЯ половина: собрать
payload и отдать его в CheckUp по HTTP.

Приёмник на стороне CheckUp уже живёт и работает:
  POST https://checkupapp.ru/api/v1/loyalup/complaints/inbound/
  header X-LoyalUP-Relay-Secret: <LOYALUP_RELAY_SECRET>   (тот же секрет,
  что у саппорт-релея — он уже лежит в .env обоих проектов).

Транспорт скопирован с уже работающего `_safe_relay_to_checkup`
(apps/tenant/mobile/api/views.py) — та же схема авторизации, тот же адрес
через nginx. Отличия ровно два, и оба намеренные:
  • отправка идёт ЧЕРЕЗ CELERY, а не в запросе гостя: жалоба не должна
    теряться из-за секундной недоступности соседа, и гость не должен
    ждать чужой сервис;
  • ретраи с нарастающей паузой, но ТОЛЬКО на сетевых и 5xx. 400/403/404
    — это настройка, а не сбой: ретрай их не вылечит, они идут в лог.

────────────────────────────────────────────────────────────────────────
Что считается жалобой
────────────────────────────────────────────────────────────────────────
1. `sentiment` треда — NEGATIVE или PARTIALLY_NEGATIVE (решение AI).
2. Либо (страховка на случай, когда AI молчит — кончился баланс, сеть):
   оценка из мини-аппа `rating <= 3`. Без этой ветки отключённый AI тихо
   выключил бы весь канал жалоб, и никто бы этого не заметил.
POSITIVE / NEUTRAL / SPAM не отправляются никогда.

────────────────────────────────────────────────────────────────────────
Точка обязательна (главное требование владельца)
────────────────────────────────────────────────────────────────────────
«Жалоба с Красноармейской не должна попасть на Институтскую».

Поэтому по умолчанию отправляются ТОЛЬКО треды, у которых есть точка
(`conversation.branch`) — это отзывы из мини-приложения. Сообщения,
пришедшие в ВК-группу, живут в треде с `branch=None` (так устроен
`handle_vk_incoming_message`): у них точки нет ВООБЩЕ, и угадывать её
нельзя — у «Автосуши» точки в двух городах сразу.

Такие «бесточечные» жалобы можно включить отдельно
(`CHECKUP_COMPLAINTS_RELAY_UNPOINTED=1`) — тогда они лягут в CheckUp
в «Без филиала», и разбирающийся привяжет точку руками. Молчаливой
догадки о точке нет и не будет.

В payload точка уходит ТРЕМЯ полями сразу:
  point_id  — `Branch.branch_id` (публичный ID точки, тот же, что в QR),
  point_name — название точки,
  address    — `BranchConfig.address`.
CheckUp сначала смотрит словарь `LoyalupBranchMap` по `point_id`, и лишь
если словаря нет — пробует адрес. То есть id — основной ключ, адрес —
подстраховка и способ завести соответствие руками.

────────────────────────────────────────────────────────────────────────
Дедуп: почему ключ не «id сообщения»
────────────────────────────────────────────────────────────────────────
Гость редко пишет одно сообщение — он пишет три подряд. Если ключом
сделать id сообщения, в CheckUp появятся три жалобы, три раза сработает
маршрутизация и трижды прилетит пуш ответственным. Это уже проходили в
CheckUp («дедуп жалоб = external_id»), повторять не будем.

Ключ = `<id треда>-<номер 6-часового окна>`. Все сообщения гостя внутри
одного окна складываются в ОДНУ жалобу; следующая жалоба того же гостя
через сутки — уже новая. Отправка отложена на `DISPATCH_COUNTDOWN` секунд:
за это время «очередь» сообщений успевает дописаться, и в CheckUp уезжает
весь текст сразу, а не первая фраза.

⚠️ Известное ограничение: CheckUp на повторной доставке возвращает
существующую жалобу и НЕ переписывает её текст. Значит сообщение,
дописанное гостем ПОСЛЕ ухода жалобы (но внутри окна), в CheckUp не
догонит — оно останется в LoyalUP. Это осознанный размен: лучше одна
жалоба без хвоста, чем четыре жалобы и четыре пуша.

────────────────────────────────────────────────────────────────────────
Как выключить
────────────────────────────────────────────────────────────────────────
`CHECKUP_COMPLAINTS_ENABLED=0` в окружении — рубильник на стороне
LoyalUP. Второй рубильник (по каждой организации отдельно) живёт на
стороне CheckUp: `ClientConfig.loyalup_complaints_enabled`. Пока он
выключен, CheckUp отвечает 403 — мы это логируем и не ретраим.
"""
from __future__ import annotations

import logging

from celery import shared_task

log = logging.getLogger(__name__)

# Тональности, которые считаем жалобой (значения из TestimonialConversation.Sentiment).
NEGATIVE_SENTIMENTS = ('NEGATIVE', 'PARTIALLY_NEGATIVE')

# Окно склейки сообщений одного треда в одну жалобу.
BUCKET_SECONDS = 6 * 60 * 60

# Пауза перед отправкой: даём гостю дописать очередь сообщений.
DISPATCH_COUNTDOWN = 90

# Потолки payload'а. Ручка на той стороне публичная (авторизация секретом),
# без потолков один тред может утянуть в неё что угодно.
MAX_TEXT = 8000
MAX_PHOTOS = 10
HTTP_TIMEOUT = 10


# ──────────────────────────────────────────────────────────────────
# Настройки
# ──────────────────────────────────────────────────────────────────
def _conf(name: str, default):
    from django.conf import settings
    return getattr(settings, name, default)


def _is_enabled() -> bool:
    return bool(_conf('CHECKUP_COMPLAINTS_ENABLED', True)) and bool(
        _conf('LOYALUP_RELAY_SECRET', '')
    )


# ──────────────────────────────────────────────────────────────────
# Сбор payload'а
# ──────────────────────────────────────────────────────────────────
def _bucket(dt) -> int:
    """Номер 6-часового окна. Один тред + одно окно = одна жалоба."""
    return int(dt.timestamp()) // BUCKET_SECONDS


def _abs_url(url: str) -> str:
    """Относительный /media/... → абсолютный. VK-ссылку отдаём как есть."""
    if not url:
        return ''
    if url.startswith('http://') or url.startswith('https://'):
        return url
    base = str(_conf('CHECKUP_RELAY_MEDIA_BASE', 'https://levelupapp.ru')).rstrip('/')
    return f'{base}/{url.lstrip("/")}'


def _guest_name(conv) -> str:
    """Имя гостя: профиль в точке → карточка ВК → пусто."""
    if conv.client_id:
        try:
            return str(conv.client)[:255]
        except Exception:
            pass
    if conv.vk_guest_id:
        g = conv.vk_guest
        name = f'{g.first_name} {g.last_name}'.strip()
        if name:
            return name[:255]
    return ''


def build_payload(conv, schema_name: str, message_id: int | None = None) -> dict | None:
    """Собрать payload жалобы или вернуть None, если отправлять нечего.

    None — это НЕ ошибка: сюда попадают позитивные отзывы, спам, треды без
    точки (когда отправка бесточечных выключена) и пустые сообщения.
    """
    from apps.tenant.branch.models import TestimonialMessage

    guest_sources = [TestimonialMessage.Source.APP, TestimonialMessage.Source.VK_MESSAGE]

    msg = None
    if message_id:
        msg = conv.messages.filter(pk=message_id).first()
    if msg is None:
        msg = (conv.messages.filter(source__in=guest_sources)
               .order_by('-created_at', '-id').first())
    if msg is None:
        return None

    # ── жалоба ли это ──────────────────────────────────────────────
    sentiment = (conv.sentiment or '').upper()
    rating = msg.rating if msg.rating not in (None, '') else None
    is_negative = sentiment in NEGATIVE_SENTIMENTS
    # Страховка на выключенный AI: тональности ещё нет, но оценка низкая.
    ai_silent = sentiment in ('', 'WAITING')
    if not is_negative and not (ai_silent and rating is not None and rating <= 3):
        return None

    # ── точка ─────────────────────────────────────────────────────
    branch = conv.branch if conv.branch_id else None
    if branch is None and not _conf('CHECKUP_COMPLAINTS_RELAY_UNPOINTED', False):
        # Точки нет и угадывать её нельзя — см. шапку модуля.
        return None

    point_id, point_name, address = '', '', ''
    if branch is not None:
        point_id = str(branch.branch_id)
        point_name = (branch.name or '')[:255]
        cfg = getattr(branch, 'config', None)
        address = ((getattr(cfg, 'address', '') or '') if cfg else '')[:500]

    # ── текст: все сообщения гостя внутри того же окна ───────────────
    bucket = _bucket(msg.created_at)
    parts, photos, phone = [], [], (msg.phone or '')
    window = (conv.messages
              .filter(source__in=guest_sources)
              .order_by('created_at', 'id'))
    for m in window:
        if _bucket(m.created_at) != bucket:
            continue
        t = (m.text or '').strip()
        if t:
            parts.append(t)
        if not phone and (m.phone or ''):
            phone = m.phone
        try:
            for att in m.display_attachments():
                url = _abs_url(att.get('url') or '')
                if url and url not in photos:
                    photos.append(url)
        except Exception as e:  # вложения не повод потерять жалобу
            log.warning('checkup relay: вложения conv=%s msg=%s: %s', conv.pk, m.pk, e)

    text = '\n'.join(parts).strip()
    if not text and not photos:
        return None
    if not text:
        # Жалоба-фотография без слов: пустой text приёмник отбивает 400.
        text = '(гость прислал фото без текста)'

    return {
        'tenant_schema': schema_name,
        'complaint_id':  f'{conv.pk}-{bucket}',
        'text':          text[:MAX_TEXT],
        'created_at':    msg.created_at.isoformat() if msg.created_at else None,
        'point_id':      point_id,
        'point_name':    point_name,
        'address':       address,
        'rating':        rating,
        'guest_name':    _guest_name(conv),
        'guest_phone':   (phone or '')[:50],
        'photos':        photos[:MAX_PHOTOS],
    }


# ──────────────────────────────────────────────────────────────────
# Задача отправки
# ──────────────────────────────────────────────────────────────────
@shared_task(
    name='apps.shared.relay.checkup_complaints.relay_complaint_to_checkup_task',
    bind=True,
    max_retries=5,
    acks_late=True,
)
def relay_complaint_to_checkup_task(self, conversation_id: int, schema_name: str,
                                    message_id: int | None = None) -> dict:
    """Отправить жалобу в CheckUp. Идемпотентно: повтор вернёт ту же жалобу.

    Ретраим только сеть и 5xx. 400/403/404 — это настройка на той стороне
    (не заведён словарь тенанта, выключен флаг организации, разъехался
    секрет): ретрай их не лечит, они должны быть видны в логе.
    """
    import requests
    from django_tenants.utils import schema_context
    from apps.tenant.branch.models import TestimonialConversation

    if not _is_enabled():
        return {'skipped': True, 'reason': 'disabled'}

    try:
        with schema_context(schema_name):
            conv = (TestimonialConversation.objects
                    .select_related('branch', 'client', 'vk_guest')
                    .filter(pk=conversation_id).first())
            if conv is None:
                return {'skipped': True, 'reason': 'conversation_gone'}
            payload = build_payload(conv, schema_name, message_id)
    except Exception:
        log.exception('checkup relay: не собрался payload conv=%s schema=%s',
                      conversation_id, schema_name)
        return {'skipped': True, 'reason': 'payload_error'}

    if payload is None:
        return {'skipped': True, 'reason': 'not_a_complaint'}

    url = str(_conf('CHECKUP_COMPLAINTS_URL',
                    'https://checkupapp.ru/api/v1/loyalup/complaints/inbound/'))
    secret = str(_conf('LOYALUP_RELAY_SECRET', ''))

    try:
        # trust_env=False намеренно: воркер не должен утащить чужой прокси
        # из окружения — сосед живёт на этой же машине.
        sess = requests.Session()
        sess.trust_env = False
        r = sess.post(
            url,
            json=payload,
            headers={'X-LoyalUP-Relay-Secret': secret,
                     'Content-Type': 'application/json'},
            timeout=HTTP_TIMEOUT,
        )
    except Exception as exc:
        log.warning('checkup relay: сеть недоступна conv=%s (%s), попытка %s',
                    conversation_id, exc, self.request.retries)
        raise self.retry(exc=exc, countdown=min(600, 60 * (2 ** self.request.retries)))

    if r.status_code in (200, 201):
        try:
            body = r.json()
        except Exception:
            body = {}
        log.info('checkup relay: жалоба %s ушла (conv=%s, review=%s, created=%s, branch=%s)',
                 payload['complaint_id'], conversation_id,
                 body.get('checkup_review_id'), body.get('created'), body.get('branch_id'))
        return {'ok': True, 'created': body.get('created'),
                'checkup_review_id': body.get('checkup_review_id')}

    if r.status_code in (400, 403, 404):
        # Настройка, а не сбой: словарь тенанта не заведён / флаг выключен /
        # секрет разъехался. Ретрай не поможет — нужен человек.
        log.error('checkup relay: CheckUp отказал %s: %s (conv=%s, tenant=%s) — НАСТРОЙКА',
                  r.status_code, r.text[:300], conversation_id, schema_name)
        return {'ok': False, 'status': r.status_code, 'permanent': True}

    log.warning('checkup relay: CheckUp ответил %s: %s (conv=%s), попытка %s',
                r.status_code, r.text[:300], conversation_id, self.request.retries)
    raise self.retry(countdown=min(600, 60 * (2 ** self.request.retries)))


# ──────────────────────────────────────────────────────────────────
# Точка вызова из кода приёма отзывов
# ──────────────────────────────────────────────────────────────────
def dispatch_complaint_relay(conversation_id: int, message_id: int | None = None,
                             schema_name: str | None = None) -> None:
    """Поставить отправку жалобы в очередь. НИКОГДА не бросает.

    Вызывается сразу после AI-классификации отзыва. Приём отзыва — вещь
    неприкосновенная: если соседняя система, брокер или наша же логика
    сломались, гость всё равно должен успешно отправить отзыв.
    """
    try:
        if not _is_enabled():
            return
        if not schema_name:
            from django.db import connection
            schema_name = getattr(connection, 'schema_name', '') or getattr(
                getattr(connection, 'tenant', None), 'schema_name', '')
        if not schema_name or schema_name == 'public':
            return
        relay_complaint_to_checkup_task.apply_async(
            args=[conversation_id, schema_name, message_id],
            countdown=DISPATCH_COUNTDOWN,
        )
    except Exception:
        log.exception('checkup relay: не удалось поставить задачу conv=%s', conversation_id)
