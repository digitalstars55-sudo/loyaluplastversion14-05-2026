"""
Студия в телефоне по макету владельца (22.10.2026): шаги сборки, очередь,
«На согласование и в контент-план», пуш «Ролик готов — опубликовать?».

  • шаги — те же шесть, что на экране «Собираем ролик»; считаются из того, что
    реально произошло (сценарий, материалы, стадия сборщика), ничего не выдумано;
  • очередь сети — версии этой схемы, ждущие сборщика раньше нашей; сборщик
    один, ролики идут по одному; время — по средним последним сборкам сети;
  • предложенный план — ВК клип + пост 9:16 и Телеграм-канал 1:1 в ближайший
    свободный слот, ВК-сторис 6 с на следующий день в 12:00; в план публикации
    встаёт только после «Опубликовать» того, у кого есть право на сеть;
  • события (готово / не собралось / вопрос / ждёт согласования) уходят в
    CheckUp тем же реле, что жалобы и подсказка официанту; CheckUp сам решает,
    кому пуш. Реле упало — ролик цел, только без пуша.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

import requests
from django.conf import settings
from django.db import connection
from django.utils import timezone

from .models import StudioAsset, StudioProject, StudioVersion

log = logging.getLogger(__name__)

USERNAME_PREFIX = 'checkup-'
FMT_LABEL = {'9x16': '9:16', '1x1': '1:1', '16x9': '16:9', '6s': '6 с'}
CHANNEL_LABEL = {'vk': 'ВК клип + пост', 'tg': 'Телеграм-канал', 'vk_story': 'ВК сторис'}
DEFAULT_RENDER_SECONDS = 240
STORY_HOUR = 12
TIMEOUT = 8


def checkup_id_of(user) -> str:
    """`checkup-<id>-<схема>` → `<id>`; чужой вход (не из CheckUp) → ''."""
    name = str(getattr(user, 'username', '') or '')
    if not name.startswith(USERNAME_PREFIX):
        return ''
    rest = name[len(USERNAME_PREFIX):]
    schema = connection.schema_name or ''
    if schema and rest.endswith('-' + schema):
        rest = rest[:-(len(schema) + 1)]
    return rest[:64]


# ── очередь и время ──────────────────────────────────────────────────────────

def queue_ahead(version: StudioVersion) -> int:
    if version.status != StudioVersion.Status.QUEUED:
        return 0
    return (StudioVersion.objects.filter(status__in=(StudioVersion.Status.QUEUED, StudioVersion.Status.RENDERING))
            .filter(created_at__lt=version.created_at).count())


def average_render_seconds() -> int:
    готовые = (StudioVersion.objects.filter(status=StudioVersion.Status.READY)
               .exclude(claimed_at=None).exclude(finished_at=None).order_by('-finished_at')
               .values_list('claimed_at', 'finished_at')[:10])
    длины = [(f - c).total_seconds() for c, f in готовые if f > c]
    return int(sum(длины) / len(длины)) if длины else DEFAULT_RENDER_SECONDS


def eta_seconds(version: StudioVersion) -> int | None:
    if version.status not in (StudioVersion.Status.QUEUED, StudioVersion.Status.RENDERING):
        return None
    avg = average_render_seconds()
    if version.status == StudioVersion.Status.RENDERING:
        прошло = (timezone.now() - version.claimed_at).total_seconds() if version.claimed_at else 0
        return max(int(avg - прошло), 30)
    return avg * (queue_ahead(version) + 1)


# ── шаги «Собираем ролик» ────────────────────────────────────────────────────

def _сек(sec: float) -> str:
    sec = int(round(sec or 0))
    return f'{sec // 60} мин {sec % 60} с' if sec >= 60 else f'{sec} с'


def _материалы(project: StudioProject) -> str:
    assets = list(project.assets.all())
    видео = [a for a in assets if a.kind == StudioAsset.Kind.VIDEO]
    фото = [a for a in assets if a.kind == StudioAsset.Kind.PHOTO]
    части = [f'{len(assets)} файл{"" if len(assets) == 1 else "ов" if len(assets) >= 5 or not assets else "а"}']
    if видео:
        части.append(f'{_сек(sum(a.duration or 0 for a in видео))} видео')
    if фото:
        части.append(f'{len(фото)} фото')
    return ', '.join(части)


def _сцены(scn: dict) -> str:
    сцены = scn.get('scenes') or []
    подписи = [s.get('caption') or {'video': 'видео', 'photo': 'фото'}.get(s.get('kind'), 'титр')
               for s in сцены]
    короткие = [p if len(p) <= 18 else p[:17] + '…' for p in подписи[:5]]
    if (scn.get('final') or {}).get('offer'):
        короткие.append('предложение')
    return ' · '.join(короткие)


def steps(project: StudioProject, version: StudioVersion | None) -> list[dict]:
    """Шесть шагов: state = done | active | pending | failed; progress 0–100 у активного."""
    scn = (version.scenario if version else None) or {}
    snap = (version.style_snapshot if version else None) or {}
    сцен = len(scn.get('scenes') or [])
    форматов = len(version.files.all()) if version and version.status == StudioVersion.Status.READY else 4
    s = [
        {'n': 1, 'title': 'Разобрал материалы', 'detail': _материалы(project)},
        {'n': 2, 'title': f'Сценарий из {сцен} сцен' if сцен else 'Сценарий', 'detail': _сцены(scn)},
        {'n': 3, 'title': 'Монтаж по ритму бренда',
         'detail': ', '.join(x for x in (
             f'склейки {snap.get("cut_pace")}' if snap.get('cut_pace') else '',
             'на долю музыки' if snap.get('music') and scn.get('music') != 'none' else 'без музыки') if x)},
        {'n': 4, 'title': 'Титры и графика', 'detail': snap.get('captions') or ''},
        {'n': 5, 'title': 'Рендер MP4 1080×1920',
         'detail': f'и ещё {форматов - 1} формата' if форматов > 1 else ''},
        {'n': 6, 'title': 'Проверка: звук, стыки, поля', 'detail': ''},
    ]
    # Где мы сейчас.
    if project.status in (StudioProject.Status.WRITING, StudioProject.Status.ASKING) or version is None:
        активный = 2 if project.assets.exists() else 1
        прогресс = 0
    elif version.status == StudioVersion.Status.READY:
        активный, прогресс = 7, 100
    elif version.status == StudioVersion.Status.QUEUED:
        активный, прогресс = 4, 0
    else:
        стадия = (version.stage or '').lower()
        прогресс = version.progress or 0
        if 'провер' in стадия or 'отправляю' in стадия:
            активный = 6
        elif 'собираю' in стадия and '(1 из' not in стадия:
            активный = 5
        else:
            активный = 4
    упал = version is not None and version.status == StudioVersion.Status.FAILED
    for шаг in s:
        n = шаг['n']
        if упал:
            шаг['state'] = 'done' if n < 4 else ('failed' if n == 4 else 'pending')
        elif n < активный:
            шаг['state'] = 'done'
        elif n == активный:
            шаг['state'] = 'active'
            шаг['progress'] = прогресс
        else:
            шаг['state'] = 'pending'
    if упал:
        s[3]['detail'] = version.error[:200]
    return s


def elapsed_seconds(version: StudioVersion) -> int | None:
    """Сколько ролик ждёт с нажатия «Собрать» (очередь + сборка)."""
    if version.status not in (StudioVersion.Status.QUEUED, StudioVersion.Status.RENDERING):
        return None
    return max(int((timezone.now() - version.created_at).total_seconds()), 0)


def is_stalled(version: StudioVersion) -> bool:
    """Дольше обычного вдвое (и минимум на 5 минут сверх среднего) — честно сказать «долго», а не «меньше минуты».

    Прогноз `eta_seconds` не опускается ниже 30 с, и зависший ролик (сборщик падал от нехватки памяти, 23.10.2026)
    вечно обещал «меньше чем через минуту». Очередь из других роликов в «долго» не считается.
    """
    прошло = elapsed_seconds(version)
    if прошло is None or queue_ahead(version):
        return False
    avg = average_render_seconds()
    return прошло > max(2 * avg, avg + 300)


def build_info(project: StudioProject) -> dict:
    v = project.current_version
    return {
        'steps': steps(project, v),
        'queue_ahead': queue_ahead(v) if v else 0,
        'eta_seconds': eta_seconds(v) if v else None,
        'elapsed_seconds': elapsed_seconds(v) if v else None,
        'stalled': is_stalled(v) if v else False,
    }


# ── план «на согласование и в контент-план» ─────────────────────────────────

def propose_plan(project: StudioProject, now=None) -> list[dict]:
    from .studio_publish import свободный_слот
    слот = свободный_слот(now)
    сторис = timezone.make_aware(datetime.combine(timezone.localtime(слот).date() + timedelta(days=1),
                                                  time(STORY_HOUR, 0)))
    готовые = set()
    if project.current_version_id:
        готовые = set(project.current_version.files.values_list('fmt', flat=True))
    rows = [{'channel': 'vk', 'fmt': '9x16', 'at': слот.isoformat()},
            {'channel': 'tg', 'fmt': '1x1', 'at': слот.isoformat()}]
    if '6s' in готовые:
        rows.append({'channel': 'vk_story', 'fmt': '6s', 'at': сторис.isoformat()})
    return rows


def plan_lines(project: StudioProject, rows: list[dict] | None = None) -> list[str]:
    """«пт 18:00 · ВК клип + пост» — строки для пуша и экрана."""
    дни = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']
    out = []
    for r in rows if rows is not None else (project.proposed_plan or []):
        try:
            at = timezone.localtime(datetime.fromisoformat(r['at']))
        except (KeyError, ValueError):
            continue
        канал = CHANNEL_LABEL.get(r.get('channel'), r.get('channel'))
        формат = '' if r.get('channel') == 'vk' else f' {FMT_LABEL.get(r.get("fmt"), r.get("fmt"))}'
        out.append(f'{дни[at.weekday()]} {at:%H:%M} · {канал}{формат}')
    return out


def price_line(version: StudioVersion | None) -> str:
    """«цена 1 990 ₽ сверена с меню» — только если цены в ролике есть (сверка прошла при сценарии)."""
    from .studio import _fmt_price, prices_in
    if version is None:
        return ''
    цены = prices_in(version.scenario or {})
    if not цены:
        return ''
    return f'цена {", ".join(_fmt_price(c) for c in цены[:2])} сверена с меню'


# ── события в CheckUp ────────────────────────────────────────────────────────

def _url() -> str:
    explicit = getattr(settings, 'CHECKUP_STUDIO_EVENT_URL', '')
    if explicit:
        return explicit
    base = str(getattr(settings, 'CHECKUP_COMPLAINTS_URL', '')
               or 'https://checkupapp.ru/api/v1/loyalup/complaints/inbound/')
    return base.split('/api/v1/')[0] + '/api/v1/loyalup/studio-event/'


def _poster(version: StudioVersion | None) -> str:
    """Кадр ролика полным https-адресом на домене сети — для картинки в пуше."""
    if version is None:
        return ''
    out = version.files.filter(fmt='9x16').first() or version.files.order_by('pk').first()
    if out is None or not out.poster:
        return ''
    try:
        domain = next((d.domain for d in connection.tenant.domains.all() if d.is_primary), '')
    except Exception:  # noqa: BLE001 — без домена пуш уйдёт без картинки
        domain = ''
    return f'https://{domain}{out.poster.url}' if domain else ''


def send_event(project: StudioProject, event: str) -> bool:
    """event: ready | failed | asking | approval. Ничего не роняет."""
    secret = str(getattr(settings, 'LOYALUP_RELAY_SECRET', '') or '')
    if not secret:
        return False
    v = project.current_version
    scn = (v.scenario if v else None) or {}
    body = {
        'tenant': connection.schema_name, 'event': event, 'project_id': project.pk,
        'title': project.title, 'author_checkup_id': project.author_checkup_id,
        'version': v.number if v else None, 'question': project.question if event == 'asking' else '',
        'error': (project.error or (v.error if v else ''))[:300] if event == 'failed' else '',
        'lines': plan_lines(project) if event == 'approval' else [],
        'price_line': price_line(v) if event in ('approval', 'ready') else '',
        'duration': round(sum(s.get('length', 0) for s in (scn.get('scenes') or []))
                          + (scn.get('final') or {}).get('length', 0)) if v else None,
        'poster': _poster(v),
    }
    try:
        sess = requests.Session()
        sess.trust_env = False      # сосед на той же машине, чужой прокси не нужен
        r = sess.post(_url(), json=body, timeout=TIMEOUT,
                      headers={'X-LoyalUP-Relay-Secret': secret, 'Content-Type': 'application/json'})
        if r.status_code >= 400:
            log.warning('studio event %s %s/%s: %s %s', event, connection.schema_name, project.pk,
                        r.status_code, r.text[:200])
            return False
        return True
    except Exception as exc:  # noqa: BLE001 — пуш не важнее ролика
        log.warning('studio event %s %s/%s: %s', event, connection.schema_name, project.pk, exc)
        return False


def send_event_later(project: StudioProject, event: str):
    """После коммита и вне запроса — реле может думать до 8 с."""
    from django.db import transaction

    from apps.tenant.marketer.tasks import studio_event_task
    schema, pk = connection.schema_name, project.pk
    transaction.on_commit(lambda: studio_event_task.delay(schema, pk, event))
