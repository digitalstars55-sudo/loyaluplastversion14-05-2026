"""
Стоп-лист перед рассылкой (15.10.2026, Ops Bridge аудита по брифу: «не
рекламировать то, чего нет»).

Перед отправкой рассылки LoyalUP спрашивает CheckUp, какие блюда сейчас в стопе
(`GET /api/v1/loyalup/stoplist/`, секрет `LOYALUP_RELAY_SECRET`), и ищет их в
тексте. Нашлось — отправка отвечает 409 `stoplist_conflict` со списком; человек
видит предупреждение и может отправить всё равно (`ignore_stoplist: true`).

Совпадение — по первому значимому слову названия блюда (основа слова, без
окончания): «Сырники со сметаной 180 г» ловит «сырники», «сырников». Это
предупреждение, а не запрет: лишнее срабатывание стоит одного клика, пропуск —
рекламы того, чего нет.

Включается настройкой сети `stoplist_guard_enabled` (по умолчанию выключено).
CheckUp не ответил — рассылку не держим (лог WARNING): проверка не имеет права
сама остановить маркетинг.
"""
from __future__ import annotations

import logging
import re

import requests
from django.conf import settings

log = logging.getLogger('loyalup.stoplist_guard')

TIMEOUT = 5
_WORD = re.compile(r'[a-zа-яё]+', re.I)
# Слова, по которым блюдо не узнать: служебные и слишком общие.
_SKIP = frozenset('''
и в с со на по для из без или а к от до за под над при про шт гр г кг мл л порция
соус напиток добавка топпинг доп комбо сет набор порц
'''.split())


def _stem(word: str) -> str:
    w = word.lower().replace('ё', 'е')
    if len(w) > 6:
        return w[:-2]
    if len(w) > 4:
        return w[:-1]
    return w


def _key(name: str) -> str | None:
    """Основа первого значимого слова названия блюда."""
    for word in _WORD.findall(name or ''):
        w = word.lower()
        if len(w) >= 4 and w not in _SKIP:
            return _stem(w)
    return None


def find_conflicts(texts, items) -> list[dict]:
    """Блюда из стопа (items: [{name, branch}]), упомянутые в текстах рассылки."""
    stems = {_stem(w) for t in texts if t for w in _WORD.findall(t) if len(w) >= 4}
    out, seen = [], set()
    for item in items or ():
        key = _key(item.get('name', ''))
        if not key:
            continue
        hit = key in stems or any(s.startswith(key) or key.startswith(s) for s in stems if len(s) >= 4)
        if hit and (item.get('name'), item.get('branch')) not in seen:
            seen.add((item.get('name'), item.get('branch')))
            out.append({'name': item.get('name'), 'branch': item.get('branch')})
    return out


def _url() -> str:
    explicit = getattr(settings, 'CHECKUP_STOPLIST_URL', '')
    if explicit:
        return explicit
    base = str(getattr(settings, 'CHECKUP_COMPLAINTS_URL', '')
               or 'https://checkupapp.ru/api/v1/loyalup/complaints/inbound/')
    return base.split('/api/v1/')[0] + '/api/v1/loyalup/stoplist/'


def fetch_stoplist(schema: str, point_id=None) -> list[dict] | None:
    """Стоп-лист из CheckUp или None (не настроено / не ответил)."""
    secret = str(getattr(settings, 'LOYALUP_RELAY_SECRET', '') or '')
    if not secret:
        return None
    params = {'tenant': schema}
    if point_id:
        params['point_id'] = str(point_id)
    try:
        sess = requests.Session()
        sess.trust_env = False
        r = sess.get(_url(), params=params, headers={'X-LoyalUP-Relay-Secret': secret}, timeout=TIMEOUT)
    except Exception as e:  # noqa: BLE001
        log.warning('stoplist: CheckUp не ответил (%s)', e)
        return None
    if r.status_code != 200:
        log.warning('stoplist: CheckUp ответил %s для %s', r.status_code, schema)
        return None
    try:
        return list((r.json() or {}).get('items') or [])
    except ValueError:
        return None


def enabled(config) -> bool:
    return bool(getattr(config, 'stoplist_guard_enabled', False))


def check(schema: str, texts, branch_pks) -> list[dict]:
    """
    Конфликты рассылки со стоп-листом по точкам рассылки (пусто — можно слать).
    branch_pks — внутренние Branch.id; в CheckUp уходит публичный Branch.branch_id
    (тот же, что в QR и в словаре точек LoyalupBranchMap).
    """
    from apps.tenant.branch.models import Branch

    pks = {b for b in branch_pks if b}
    point_ids = sorted({str(x) for x in Branch.objects.filter(pk__in=pks).values_list('branch_id', flat=True)})
    items: list[dict] = []
    if len(point_ids) == 1:
        got = fetch_stoplist(schema, point_ids[0])
        items = got or []
    else:
        got = fetch_stoplist(schema)
        items = got or []
    conflicts = find_conflicts(texts, items)
    if conflicts:
        log.info('stoplist: %s конфликт(ов) в рассылке %s: %s', len(conflicts), schema,
                 ', '.join(c['name'] for c in conflicts[:5]))
    return conflicts
