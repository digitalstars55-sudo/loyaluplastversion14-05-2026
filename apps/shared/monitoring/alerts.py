"""
Дедуп и доставка сигналов мониторинга.

Главная мысль: проверки крутятся раз в час, а беда живёт днями. Без памяти
владелец получил бы сотню одинаковых пушей про один и тот же сертификат и
отключил бы уведомления — то есть мониторинг убил бы сам себя. Поэтому:

  * один и тот же key будим пушем не чаще repeat_hours;
  * НО если warn вырос до critical — будим сразу, не дожидаясь окна: «осталось
    14 дней» и «истёк» — это разные новости;
  * сигнал, который перестал приходить, закрываем и шлём «пришло в норму»
    (только если по нему вообще будили — иначе это шум ни о чём);
  * больше трёх пушей за раз сворачиваем в один сводный: когда падает всё
    сразу, десять вибраций подряд не помогают, а мешают.

Доставка — best-effort: мониторинг не имеет права уронить beat-задачу.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django_tenants.utils import schema_context

from apps.shared.users.push import log_notification, send_expo_push

from .checks import SEVERITY_CRITICAL, Finding
from .models import MonitorAlertState

logger = logging.getLogger(__name__)

PUSH_TYPE = 'platform_alert'

# Больше скольких пушей за один проход сворачиваем в сводку.
FOLD_THRESHOLD = 3


def _icon(severity: str) -> str:
    return '🔴' if severity == SEVERITY_CRITICAL else '⚠️'


def _signals_word(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return 'сигналов'
    last = n % 10
    if last == 1:
        return 'сигнал'
    if last in (2, 3, 4):
        return 'сигнала'
    return 'сигналов'


def should_send(state, finding: Finding, now, repeat_hours: int) -> bool:
    """
    Будить ли пушем по этому сигналу. Чистая функция — состояние приходит
    объектом с полями severity/last_sent_at (может быть None = сигнал новый),
    поэтому решение проверяется тестами без БД.

    Шлём, когда: сигнал новый; по нему ещё ни разу не слали; прошло окно
    repeat_hours; важность выросла с warn до critical.
    """
    if state is None or state.last_sent_at is None:
        return True
    if state.severity != SEVERITY_CRITICAL and finding.severity == SEVERITY_CRITICAL:
        return True
    return (now - state.last_sent_at) >= timedelta(hours=repeat_hours)


def _superadmin_recipients():
    """
    Получатели платформенных алертов — все живые суперадмины и их устройства.

    Намеренно БЕЗ is_push_allowed: пер-тенантные настройки пушей заданы для
    сетей, а у платформенной беды тенанта нет (схема public в prefs никем не
    выставлена, зато tenants['*']=False заглушил бы ЧП-канал целиком).
    Сертификат, домен и упавший вход — не та новость, которую можно пропустить.
    """
    from apps.shared.users.models import PushToken, User

    users = list(User.objects.filter(is_superuser=True, is_active=True))
    if not users:
        return [], []
    tokens = list(
        PushToken.objects.filter(user__in=users).values_list('token', flat=True)
    )
    return users, tokens


def _send_one(users, tokens, title: str, body: str, data: dict) -> None:
    """Один пуш + запись в историю уведомлений. Журнал пишем всегда, даже без токенов."""
    try:
        log_notification(users, PUSH_TYPE, title, body, data)
    except Exception as e:
        logger.warning('monitoring: log_notification упал: %s', e)
    if not tokens:
        return
    try:
        send_expo_push(tokens=tokens, title=title, body=body, data=data)
    except Exception as e:
        logger.warning('monitoring: send_expo_push упал: %s', e)


def _push_findings(to_send: list[Finding], users, tokens) -> int:
    """Пуши по активным сигналам. Возвращает число отправленных СООБЩЕНИЙ (сводка = одно)."""
    if not to_send:
        return 0

    if len(to_send) > FOLD_THRESHOLD:
        worst = SEVERITY_CRITICAL if any(f.severity == SEVERITY_CRITICAL for f in to_send) else 'warn'
        title = f'{_icon(worst)} LoyalUP: {len(to_send)} {_signals_word(len(to_send))} мониторинга'
        body = '; '.join(f.title for f in to_send)
        _send_one(users, tokens, title, body, {
            'type': PUSH_TYPE,
            'severity': worst,
            'keys': [f.key for f in to_send],
        })
        return 1

    for finding in to_send:
        _send_one(users, tokens, f'{_icon(finding.severity)} LoyalUP: {finding.title}', finding.body, {
            'type': PUSH_TYPE,
            'severity': finding.severity,
            'key': finding.key,
        })
    return len(to_send)


def _push_resolved(states: list, users, tokens) -> int:
    """
    «Пришло в норму» по закрытым сигналам. Сворачиваем так же, как и беды:
    после починки пяти сетей разом пять отдельных вибраций — тот же спам.
    """
    if not states:
        return 0

    if len(states) > FOLD_THRESHOLD:
        titles = '; '.join(s.title for s in states)
        _send_one(users, tokens, f'✅ LoyalUP: {len(states)} {_signals_word(len(states))} пришли в норму', titles, {
            'type': PUSH_TYPE,
            'severity': 'resolved',
            'keys': [s.key for s in states],
        })
        return 1

    for state in states:
        _send_one(users, tokens, f'✅ {state.title}: пришло в норму', state.body or '', {
            'type': PUSH_TYPE,
            'severity': 'resolved',
            'key': state.key,
        })
    return len(states)


def deliver(findings: list[Finding], now, repeat_hours: int) -> dict:
    """
    Сохранить состояние сигналов и разбудить суперадминов.

    Возвращает счётчики: new — сигналов увидели впервые, repeated — старые, по
    которым пора будить снова, suppressed — старые внутри окна тишины,
    resolved — закрытых, pushed — сколько пуш-сообщений реально ушло
    (после сворачивания их меньше, чем сигналов).

    last_sent_at проставляем ПОСЛЕ доставки: если отправка упала целиком,
    отметки нет и на следующем часе попробуем снова.
    """
    stats = {'new': 0, 'repeated': 0, 'suppressed': 0, 'resolved': 0, 'pushed': 0}

    with schema_context('public'):
        to_send: list[Finding] = []
        sent_states: list[MonitorAlertState] = []
        seen_keys: list[str] = []

        for finding in findings:
            seen_keys.append(finding.key)
            state = MonitorAlertState.objects.filter(key=finding.key).first()
            send = should_send(state, finding, now, repeat_hours)

            if state is None:
                state = MonitorAlertState(key=finding.key)
                stats['new'] += 1
            elif send:
                stats['repeated'] += 1
            else:
                stats['suppressed'] += 1

            state.severity = finding.severity
            state.title = finding.title
            state.body = finding.body
            state.data = finding.data or {}
            state.last_seen_at = now
            state.resolved_at = None          # вернулась старая беда — снова активна
            state.save()

            if send:
                to_send.append(finding)
                sent_states.append(state)

        # Сигналы, которых в этом проходе не было, считаем починенными.
        resolved_states = []
        stale = MonitorAlertState.objects.filter(resolved_at__isnull=True).exclude(key__in=seen_keys)
        for state in stale:
            state.resolved_at = now
            state.save(update_fields=['resolved_at'])
            stats['resolved'] += 1
            if state.last_sent_at:
                resolved_states.append(state)

        try:
            users, tokens = _superadmin_recipients()
            stats['pushed'] = _push_findings(to_send, users, tokens) + _push_resolved(resolved_states, users, tokens)

            for state in sent_states:
                state.last_sent_at = now
                state.sent_count = (state.sent_count or 0) + 1
                state.save(update_fields=['last_sent_at', 'sent_count'])
        except Exception as e:
            logger.exception('monitoring: доставка алертов упала: %s', e)

    return stats
