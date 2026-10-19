"""
Очередь сборщика роликов (19.10.2026) — публичная схема, без входа, по токену.

Сборщик (`tools/studio-worker`) живёт на отдельной машине: там HyperFrames,
Chrome и FFmpeg, которых в контейнере web нет и не будет. Он ходит сюда:

  POST /api/v1/studio-worker/claim/                       → взять версию в сборку
  GET  /api/v1/studio-worker/<схема>/<версия>/file/<ключ>/ → скачать материал
        ключ: asset-<id> | logo | logo_dark | music
  POST /api/v1/studio-worker/<схема>/<версия>/progress/   {stage, progress}
  POST /api/v1/studio-worker/<схема>/<версия>/output/     multipart fmt, mp4, poster, width, height, duration
  POST /api/v1/studio-worker/<схема>/<версия>/done/       {} или {error}

Доступ — заголовок `X-Studio-Worker-Token` = `STUDIO_WORKER_TOKEN`. Пустой
токен = очередь выключена (503). Сеть сборщик берёт из ответа claim; чужую
схему без версии в сборке он не прочтёт — каждая ручка сверяет, что версия
действительно «собирается» и выдана этому же сборщику.

Файлы по форматам шлются отдельными запросами: один MP4 — один запрос, тело
не упирается в лимит nginx на всё сразу.
"""
from __future__ import annotations

import hmac
import json
import logging
import re
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.http import FileResponse, JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django_tenants.utils import schema_context

log = logging.getLogger(__name__)

HEADER = 'HTTP_X_STUDIO_WORKER_TOKEN'
STALE_AFTER = timedelta(minutes=30)
MAX_MP4 = 200 * 1024 * 1024
_SCHEMA_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,62}$')
_FMT_RE = re.compile(r'^[0-9a-z]{2,8}$')


def _err(status: int, code: str, detail: str = '') -> JsonResponse:
    return JsonResponse({'code': code, 'detail': detail}, status=status)


def _auth(request):
    token = getattr(settings, 'STUDIO_WORKER_TOKEN', '') or ''
    if not token:
        return _err(503, 'disabled', 'очередь сборки выключена')
    got = request.META.get(HEADER, '') or ''
    if not got or not hmac.compare_digest(got.encode(), token.encode()):
        return _err(401, 'bad_token')
    return None


def _schemas() -> list[str]:
    from apps.shared.clients.beat_guard import beat_tenants
    return [t.schema_name for t in beat_tenants()]


def job_payload(schema: str, version) -> dict:
    """Всё, что сборщику нужно для одной версии: сценарий, стиль, ссылки на файлы."""
    from apps.tenant.marketer.models import StudioAsset, StudioStyle
    project = version.project
    base = f'/api/v1/studio-worker/{schema}/{version.pk}/file/'
    ids = {s.get('asset_id') for s in (version.scenario or {}).get('scenes', []) if s.get('asset_id')}
    assets = []
    for a in StudioAsset.objects.filter(pk__in=ids):
        assets.append({'id': a.pk, 'kind': a.kind, 'url': f'{base}asset-{a.pk}/',
                       'name': a.file.name.rsplit('/', 1)[-1], 'duration': a.duration})
    style = StudioStyle.objects.order_by('pk').first()
    snap = dict(version.style_snapshot or {})
    brand = {}
    for key in ('logo', 'logo_dark', 'music'):
        if snap.get(key) or (style and getattr(style, key)):
            brand[key] = f'{base}{key}/'
    return {
        'schema': schema, 'version_id': version.pk, 'number': version.number,
        'project_id': project.pk, 'title': project.title, 'kind': project.kind,
        'duration': project.duration, 'scenario': version.scenario, 'style': snap,
        'assets': assets, 'brand': brand, 'formats': ['9x16', '1x1', '16x9', '6s'],
    }


def _requeue_stale():
    from apps.tenant.marketer.models import StudioVersion
    cutoff = timezone.now() - STALE_AFTER
    StudioVersion.objects.filter(status=StudioVersion.Status.RENDERING, claimed_at__lt=cutoff).update(
        status=StudioVersion.Status.QUEUED, stage='Сборщик пропал — снова в очереди', progress=0,
        worker='', claimed_at=None)


def claim_one(schema: str, worker: str):
    from apps.tenant.marketer.models import StudioProject, StudioVersion
    with schema_context(schema):
        _requeue_stale()
        with transaction.atomic():
            version = (StudioVersion.objects.select_for_update(skip_locked=True)
                       .filter(status=StudioVersion.Status.QUEUED)
                       .select_related('project').order_by('created_at').first())
            # Версия, которую уже заменила новая правка, не собирается: вернут — соберём.
            while version is not None and version.project.current_version_id != version.pk:
                version = (StudioVersion.objects.select_for_update(skip_locked=True)
                           .filter(status=StudioVersion.Status.QUEUED, created_at__gt=version.created_at)
                           .select_related('project').order_by('created_at').first())
            if version is None:
                return None
            version.status = StudioVersion.Status.RENDERING
            version.worker = worker[:100]
            version.claimed_at = timezone.now()
            version.stage = 'Сборщик взял ролик'
            version.progress = 1
            version.error = ''
            version.save(update_fields=['status', 'worker', 'claimed_at', 'stage', 'progress', 'error',
                                        'updated_at'])
            StudioProject.objects.filter(pk=version.project_id).update(
                status=StudioProject.Status.RENDERING, updated_at=timezone.now())
        return job_payload(schema, version)


@method_decorator(csrf_exempt, name='dispatch')
class StudioClaimView(View):
    def post(self, request):
        bad = _auth(request)
        if bad:
            return bad
        try:
            body = json.loads(request.body or b'{}')
        except ValueError:
            body = {}
        worker = str(body.get('worker') or 'worker')[:100]
        for schema in _schemas():
            try:
                job = claim_one(schema, worker)
            except Exception:
                log.exception('studio claim failed in %s', schema)
                continue
            if job:
                log.info('studio claim %s v%s → %s', schema, job['version_id'], worker)
                return JsonResponse({'job': job})
        return JsonResponse({'job': None})


@method_decorator(csrf_exempt, name='dispatch')
class _VersionView(View):
    """Общая часть: токен, схема, версия «собирается»."""

    def dispatch(self, request, schema: str, vid: int, *args, **kwargs):
        bad = _auth(request)
        if bad:
            return bad
        if not _SCHEMA_RE.match(schema or '') or schema not in _schemas():
            return _err(404, 'no_schema')
        with schema_context(schema):
            from apps.tenant.marketer.models import StudioVersion
            version = StudioVersion.objects.select_related('project').filter(pk=vid).first()
            if version is None or version.status != StudioVersion.Status.RENDERING:
                return _err(409, 'not_rendering', 'версия не в сборке')
            return super().dispatch(request, version, *args, **kwargs)


class StudioFileView(_VersionView):
    def get(self, request, version, key: str):
        from apps.tenant.marketer.models import StudioAsset, StudioStyle
        f = None
        if key.startswith('asset-') and key[6:].isdigit():
            asset = StudioAsset.objects.filter(pk=int(key[6:]), project=version.project).first()
            f = asset.file if asset else None
        elif key in ('logo', 'logo_dark', 'music'):
            name = (version.style_snapshot or {}).get(key)
            style = StudioStyle.objects.order_by('pk').first()
            if name:
                f = StudioStyle._meta.get_field(key).storage.open(name, 'rb')
                return FileResponse(f, filename=name.rsplit('/', 1)[-1])
            f = getattr(style, key) if style else None
        if not f:
            return _err(404, 'no_file')
        return FileResponse(f.open('rb'), filename=f.name.rsplit('/', 1)[-1])


class StudioProgressView(_VersionView):
    def post(self, request, version):
        try:
            body = json.loads(request.body or b'{}')
        except ValueError:
            return _err(400, 'bad_json')
        try:
            progress = min(max(int(body.get('progress') or 0), 0), 99)
        except (TypeError, ValueError):
            progress = version.progress
        version.stage = str(body.get('stage') or version.stage)[:200]
        version.progress = progress
        version.claimed_at = timezone.now()  # живой сборщик не считается пропавшим
        version.save(update_fields=['stage', 'progress', 'claimed_at', 'updated_at'])
        return JsonResponse({'ok': True})


class StudioOutputView(_VersionView):
    def post(self, request, version):
        from apps.tenant.marketer.models import StudioOutput
        fmt = str(request.POST.get('fmt') or '')
        mp4 = request.FILES.get('mp4')
        if not _FMT_RE.match(fmt) or mp4 is None:
            return _err(400, 'bad_payload', 'fmt и mp4')
        if mp4.size > MAX_MP4:
            return _err(400, 'too_large')

        def _n(key, cast):
            try:
                return cast(request.POST.get(key) or 0)
            except (TypeError, ValueError):
                return cast(0)

        old = StudioOutput.objects.filter(version=version, fmt=fmt).first()
        if old:
            old.file.delete(save=False)
            if old.poster:
                old.poster.delete(save=False)
            old.delete()
        StudioOutput.objects.create(
            version=version, fmt=fmt, file=mp4, poster=request.FILES.get('poster'),
            width=_n('width', int), height=_n('height', int), duration=_n('duration', float))
        version.claimed_at = timezone.now()
        version.save(update_fields=['claimed_at', 'updated_at'])
        return JsonResponse({'ok': True}, status=201)


class StudioDoneView(_VersionView):
    def post(self, request, version):
        from apps.tenant.marketer.models import StudioProject, StudioVersion
        try:
            body = json.loads(request.body or b'{}')
        except ValueError:
            return _err(400, 'bad_json')
        error = str(body.get('error') or '').strip()
        if not error and not version.files.exists():
            error = 'сборщик не прислал ни одного файла'
        now = timezone.now()
        if error:
            version.status = StudioVersion.Status.FAILED
            version.error = error[:2000]
            version.stage = 'Не собралось'
        else:
            version.status = StudioVersion.Status.READY
            version.stage = 'Готово'
            version.progress = 100
        version.finished_at = now
        version.save(update_fields=['status', 'error', 'stage', 'progress', 'finished_at', 'updated_at'])
        if version.project.current_version_id == version.pk:
            StudioProject.objects.filter(pk=version.project_id).update(
                status=StudioProject.Status.FAILED if error else StudioProject.Status.READY,
                error=('Сборка: ' + error)[:1000] if error else '', updated_at=now)
        log.info('studio done v%s: %s', version.pk, error or 'ok')
        return JsonResponse({'ok': True})
