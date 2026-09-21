"""
Быстрый путь VK Callback API: ответить ВК за миллисекунды, тяжёлое — в очередь.

ЗАЧЕМ (инцидент 21.09.2026). `VKCallbackView` обрабатывала событие прямо в
HTTP-запросе, а внутри `message_new` висел синхронный вызов ВК
`messages.getHistory` (контекст «↳ …» к ответу). ВК шлёт события всплесками
ретраев: 21.09 за сутки пришло 116 022 запроса, из них 89 993 за час
10:00–11:00, пик 22 837 за минуту (~380/с) при норме 50–270 в ЧАС. Воркеры
gunicorn (14×4 = 56 слотов) вставали в очередь на сетевой вызов, ВК не
дожидался ответа (~3 с) и слал заново — петля раскручивала сама себя:
53 415 ответов 499, 8 710 ответов 429, 25 [CRITICAL] WORKER TIMEOUT, владелец
получил 504 на кабинете аналитики.

ПРАВИЛА ЭТОГО МОДУЛЯ. Каждое — след инцидента, нарушение стоит сети:
  1. Ручка отвечает 200 ВСЕГДА, кроме двух осознанных случаев: строка
     подтверждения и 403 на неверный секрет. Не разобрали тело, не легло в
     очередь, недоступен Redis — пишем в лог и отвечаем «ok». ВК отключает
     callback-сервер не только за 429, но и за 5xx: 15.09 так отвалились пять
     сетей, поднимать их потом руками через editCallbackServer.
  2. Секрет проверяем ЗДЕСЬ, до очереди. Иначе очередь становится открытым
     входом для любого постороннего запроса, а ответ 403 теряется.
  3. `confirmation` — только синхронно: ВК ждёт код подтверждения в теле
     ответа, из очереди его не отдать.
  4. `message_event` (нажатие кнопки «поделиться номером», №78) — только
     синхронно: в ответ мы зовём `messages.sendMessageEventAnswer`, ВК ждёт
     быстро.
  5. Дедуп — ЗДЕСЬ, до очереди. Иначе затор просто переедет на шаг правее: те
     же десятки тысяч повторов лягут в очередь и встанут уже там.
  6. Дедуп обязан быть ОБЩИМ для всех процессов gunicorn, поэтому только
     Redis. Кэш Django по умолчанию внутрипроцессный (`CACHES` в проекте не
     задан), на 14 воркерах он пропустил бы до 14 копий одного события.
  7. Секрет в очередь НЕ кладём: он уже проверен здесь, а задача зовёт
     `handle_vk_callback(..., verify_secret=False)`.

Отказ Redis не роняет приём: дедуп просто выключается (события пойдут в
очередь с повторами, их отсечёт дедуп по `vk_message_id` в базе), а если
недоступен и брокер — событие теряется, и его подберёт резервный опрос
`poll_all_vk_messages_task` (раз в 2 минуты).
"""
from __future__ import annotations

import hashlib
import logging

from django.conf import settings
from django.core.cache import caches

logger = logging.getLogger(__name__)

# События, которые уходят в очередь. Всё остальное обрабатывается синхронно
# (confirmation, message_event) либо игнорируется.
QUEUED_EVENTS = frozenset({
    'message_new', 'message_reply',
    'group_join', 'group_leave', 'message_allow', 'message_deny',
})

# Дедуп: у сообщений есть сквозной id ВК — ключ уникален навсегда, держим час
# (всплеск ретраев 21.09 уложился в час). У событий подписки id нет, ключ
# «тип + пользователь» — держим минуту, чтобы не проглотить настоящий повторный
# вход в сообщество через полчаса.
TTL_MESSAGE    = 3600
TTL_MEMBERSHIP = 60
TTL_SECRETS    = 300


def _cache():
    """Redis-кэш колбэка; None — если алиас не настроен или Redis недоступен."""
    try:
        return caches[getattr(settings, 'VK_CALLBACK_CACHE_ALIAS', 'vk_callback')]
    except Exception:
        return None


def _sha(value: str) -> str:
    return hashlib.sha256((value or '').encode('utf-8')).hexdigest()


# ── Дедуп ─────────────────────────────────────────────────────────────────────

def dedup_key(schema_name: str, data: dict) -> tuple[str | None, int]:
    """
    Ключ повтора и срок его жизни. (None, 0) — событие не дедуплицируем.

    Приоритет у сквозного `event_id`, если ВК его прислал; в коде он сейчас
    разбирается только для `message_event` (vk_message_event.py), поэтому для
    остальных типов собираем ключ сами.
    """
    event    = data.get('type') or ''
    group_id = data.get('group_id')
    obj      = data.get('object') or {}

    # `message_event` кладёт свой event_id внутрь object (vk_message_event.py),
    # у остальных типов он может прийти на верхнем уровне — смотрим оба места.
    event_id = data.get('event_id') or (obj.get('event_id') if isinstance(obj, dict) else None)
    if event_id:
        return f'vkcb:{schema_name}:{group_id}:eid:{event_id}', TTL_MESSAGE

    if event in ('message_new', 'message_reply'):
        message = obj.get('message') if isinstance(obj.get('message'), dict) else obj
        msg_id  = (message or {}).get('id')
        if msg_id:
            return f'vkcb:{schema_name}:{group_id}:{event}:{msg_id}', TTL_MESSAGE
        return None, 0

    user_id = obj.get('user_id')
    if user_id:
        return f'vkcb:{schema_name}:{group_id}:{event}:u{user_id}', TTL_MEMBERSHIP
    return None, 0


def seen_before(key: str | None, ttl: int) -> bool:
    """
    True — это повтор, обрабатывать не надо.

    `cache.add` у Redis — атомарный SET NX: два воркера на два ретрая одного
    события не пройдут оба. Redis недоступен → False («не повтор»): лучше
    обработать дважды (в базе есть дедуп по vk_message_id), чем потерять.
    """
    if not key:
        return False
    try:
        cache = _cache()
        if cache is None:
            return False
        return not cache.add(key, 1, ttl)
    except Exception:
        # Любая беда с Redis не должна мешать приёму события.
        logger.warning('vk callback: dedup unavailable, processing without it')
        return False


# ── Секрет ────────────────────────────────────────────────────────────────────

def _load_secret_state(group_id) -> dict:
    """
    Состояние секретов группы, читается из базы.

      {'state': 'none'}             — конфигов группы нет
      {'state': 'any'}              — конфиги есть, секрет ни у одного не задан
      {'state': 'hashes', 'h': […]} — принимаем секрет, совпавший с любым

    Хранится и сверяется ТОЛЬКО хеш: сам секрет не покидает базу.
    """
    from apps.tenant.senler.models import SenlerConfig
    secrets = list(
        SenlerConfig.objects
        .filter(vk_group_id=group_id)
        .values_list('vk_callback_secret', flat=True)
    )
    if not secrets:
        return {'state': 'none'}
    hashes = sorted({_sha(s) for s in secrets if s})
    if not hashes:
        return {'state': 'any'}
    return {'state': 'hashes', 'h': hashes}


def check_secret(schema_name: str, group_id, secret: str) -> str:
    """
    'ok' — принимаем, 'forbidden' — 403, 'no_config' — группа не наша (200).

    Кэш на 5 минут снимает запрос в базу с каждого события всплеска. Промах по
    хешу всегда перечитывает базу, поэтому смена секрета в админке подхватывается
    сразу, а не через срок жизни кэша.
    """
    cache    = _cache()
    ckey     = f'vkcb:{schema_name}:secrets:{group_id}'
    state    = None
    if cache is not None:
        try:
            state = cache.get(ckey)
        except Exception:
            state = None

    def _verdict(st: dict) -> str:
        if st.get('state') == 'none':
            return 'no_config'
        if st.get('state') == 'any':
            return 'ok'
        return 'ok' if _sha(secret) in (st.get('h') or []) else 'forbidden'

    if state is not None:
        verdict = _verdict(state)
        if verdict != 'forbidden':
            return verdict
        # Промах: секрет мог быть заменён в админке — перечитываем базу.

    state = _load_secret_state(group_id)
    if cache is not None:
        try:
            cache.set(ckey, state, TTL_SECRETS)
        except Exception:
            pass
    return _verdict(state)


# ── Очередь ───────────────────────────────────────────────────────────────────

def enqueue(schema_name: str, data: dict) -> bool:
    """
    Кладёт событие в ОТДЕЛЬНУЮ очередь. False — не легло (пишем в лог, но ВК
    всё равно отвечаем «ok»; сообщения подберёт резервный опрос).

    Секрет из полезной нагрузки вырезан: он проверен в быстром пути, а в
    брокере ему делать нечего.
    """
    payload = {k: v for k, v in data.items() if k != 'secret'}
    try:
        from apps.tenant.branch.tasks import handle_vk_callback_task
        handle_vk_callback_task.apply_async(
            kwargs={'schema_name': schema_name, 'payload': payload},
            queue=getattr(settings, 'VK_CALLBACK_QUEUE', 'vkcb'),
            # Воркер лежал полчаса — разбирать накопившееся поздно и вредно
            # (полезли бы старые вызовы к ВК пачкой). Сообщения подберёт
            # резервный опрос, он ходит раз в 2 минуты.
            expires=1800,
        )
        return True
    except Exception as e:
        logger.error('vk callback: enqueue failed (%s): %s', type(e).__name__, e)
        return False
