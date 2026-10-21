"""
Студия видео для кабинета CheckUp (19.10.2026): ролики по заданию словами.

Права — как у остального маркетолога: смотреть может всякий с доступом к
рассылкам, менять — администратор сети и суперадмин (`_may_write`).
Сценарий и правки пишет ИИ в фоне (`studio_script_task`): ответ `202`, экран
опрашивает ролик. Сборку MP4 делает отдельный сборщик (`studio_worker.py`).

Ошибки — единой формой `{code, detail}`.
"""
from __future__ import annotations

import logging

from django.db import connection
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.users.feature_permissions import HasFeatureAccess
from apps.tenant.marketer import studio, studio_mobile
from apps.tenant.marketer.api.views import _author, _error, _iso, _may_write
from apps.tenant.marketer.models import (
    StudioAsset,
    StudioProject,
    StudioStyle,
    StudioStyleVersion,
    StudioVersion,
)

log = logging.getLogger(__name__)

PERMS = [IsAuthenticated, HasFeatureAccess.factory('broadcasts')]
MAX_BYTES = {
    StudioAsset.Kind.VIDEO: 300 * 1024 * 1024,
    StudioAsset.Kind.PHOTO: 15 * 1024 * 1024,
    StudioAsset.Kind.MUSIC: 20 * 1024 * 1024,
}
MIME = {
    StudioAsset.Kind.VIDEO: 'video',
    StudioAsset.Kind.PHOTO: 'image',
    StudioAsset.Kind.MUSIC: 'audio',
}
BRAND_FILES = {'logo': 'image', 'logo_dark': 'image', 'music': 'audio'}
STYLE_TEXT = ('heading_font', 'body_font', 'cut_pace', 'captions', 'final_card', 'forbidden', 'address')
MAX_BRIEF = 3000
BUSY = (StudioProject.Status.WRITING,)


def _url(request, f) -> str:
    """Полный https-адрес файла на домене сети.

    Кабинет CheckUp ходит к нам по loopback без TLS: `build_absolute_uri` дал
    бы `http://…`, и браузер на https вырезал бы его как смешанное содержимое.
    Домены сетей все на https — схему ставим сами, хост берём из запроса.
    """
    if not f:
        return ''
    try:
        return f'https://{request.get_host()}{f.url}'
    except Exception:
        return ''


def _deny(what: str):
    return _error('role_not_allowed', f'{what} — администратор сети', http_status.HTTP_403_FORBIDDEN)


def _queue(project: StudioProject, instruction: str = '', scene=None, author: str = ''):
    from apps.tenant.marketer.tasks import studio_script_task
    project.status = StudioProject.Status.WRITING
    project.error = ''
    project.save(update_fields=['status', 'error', 'updated_at'])
    studio_script_task.delay(connection.schema_name, project.pk, instruction, scene, author)


# ── представление ────────────────────────────────────────────────────────────

def style_to_dict(request, style: StudioStyle) -> dict:
    return {
        'version': style.version,
        'palette': style.palette or [],
        **{k: getattr(style, k) for k in STYLE_TEXT},
        'logo_url': _url(request, style.logo),
        'logo_dark_url': _url(request, style.logo_dark),
        'music_url': _url(request, style.music),
        'rules': style.rules or [],
        'updated_at': _iso(style.updated_at),
    }


def asset_to_dict(request, a: StudioAsset) -> dict:
    return {
        'id': a.pk, 'project_id': a.project_id, 'kind': a.kind, 'kind_label': a.get_kind_display(),
        'url': _url(request, a.file), 'title': a.title, 'duration': a.duration, 'size': a.size,
        'created_by': a.created_by, 'created_at': _iso(a.created_at),
    }


def version_to_dict(request, v: StudioVersion, *, full: bool = True) -> dict:
    out = {
        'id': v.pk, 'number': v.number, 'status': v.status, 'status_label': v.get_status_display(),
        'stage': v.stage, 'progress': v.progress, 'error': v.error,
        'instruction': v.instruction, 'scene': v.scene, 'changelog': v.changelog,
        'style_version': v.style_version, 'remember_suggestion': v.remember_suggestion,
        'created_at': _iso(v.created_at), 'finished_at': _iso(v.finished_at),
        'files': [{'fmt': f.fmt, 'url': _url(request, f.file), 'poster_url': _url(request, f.poster),
                   'width': f.width, 'height': f.height, 'duration': f.duration}
                  for f in v.files.all()],
    }
    if full:
        out['scenario'] = v.scenario
        out['length'] = studio.scenario_length(v.scenario or {})
    return out


def project_to_dict(request, p: StudioProject, *, full: bool = False) -> dict:
    cur = p.current_version
    out = {
        'id': p.pk, 'title': p.title, 'kind': p.kind, 'kind_label': p.get_kind_display(),
        'brief': p.brief, 'duration': p.duration,
        'status': p.status, 'status_label': p.get_status_display(),
        'question': p.question, 'answer': p.answer, 'error': p.error,
        'style_version': p.style_version,
        'approval': p.approval, 'approval_label': p.get_approval_display(),
        'approved_by': p.approved_by, 'approved_at': _iso(p.approved_at),
        'publish_plan': p.publish_plan, 'post_text': p.post_text, 'created_by': p.created_by,
        'created_at': _iso(p.created_at), 'updated_at': _iso(p.updated_at),
        'current': version_to_dict(request, cur, full=full) if cur else None,
        'series': p.series,
        'proposed_plan': p.proposed_plan or [],
        'proposed_lines': studio_mobile.plan_lines(p),
    }
    # Ход сборки (экран «Собираем ролик») — шаги, очередь сети, примерное время.
    if full or p.status in (StudioProject.Status.WRITING, StudioProject.Status.ASKING,
                            StudioProject.Status.QUEUED, StudioProject.Status.RENDERING):
        out['build'] = studio_mobile.build_info(p)
    if full:
        out['price_line'] = studio_mobile.price_line(cur)
        from apps.tenant.marketer.studio_publish import свободный_слот
        out['suggested_slot'] = свободный_слот().isoformat()
        out['versions'] = [version_to_dict(request, v, full=False)
                           for v in p.versions.prefetch_related('files').order_by('-number')]
        out['assets'] = [asset_to_dict(request, a) for a in p.assets.order_by('pk')]
    return out


def _drop_file(asset: StudioAsset):
    """Файл материала удаляется, только если его не держит другая строка (копия из библиотеки)."""
    from apps.tenant.marketer.models import MarketerPhoto
    if asset.file and not StudioAsset.objects.filter(file=asset.file.name).exclude(pk=asset.pk).exists() \
            and not MarketerPhoto.objects.filter(image=asset.file.name).exists():
        asset.file.delete(save=False)


def _project(pk):
    return (StudioProject.objects.select_related('current_version')
            .prefetch_related('current_version__files').filter(pk=pk).first())


# ── стиль бренда ─────────────────────────────────────────────────────────────

class StudioStyleAPIView(APIView):
    """GET/PATCH marketer/studio/style/ — стиль роликов; PATCH поднимает версию.

    PATCH принимает JSON (palette, шрифты, тексты, note) или multipart с файлами
    logo / logo_dark / music; `remove_<файл>=1` убирает файл.
    """
    permission_classes = PERMS

    def get(self, request):
        return Response(style_to_dict(request, studio.style_obj()))

    def patch(self, request):
        if not _may_write(request.user):
            return _deny('стиль роликов меняет')
        data = request.data or {}
        style = studio.style_obj()
        changed = []
        if 'palette' in data:
            pal = data.get('palette')
            if isinstance(pal, str):
                pal = [c.strip() for c in pal.split(',') if c.strip()]
            if not isinstance(pal, list) or len(pal) > 6 or not all(
                    isinstance(c, str) and c.startswith('#') and len(c) in (4, 7) for c in pal):
                return _error('invalid_payload', 'palette: до 6 цветов вида #1B2838',
                              http_status.HTTP_400_BAD_REQUEST)
            style.palette = pal
            changed.append('цвета')
        for key in STYLE_TEXT:
            if key in data:
                limit = StudioStyle._meta.get_field(key).max_length or 2000
                setattr(style, key, str(data.get(key) or '').strip()[:limit])
                changed.append(StudioStyle._meta.get_field(key).verbose_name.lower())
        for key, mime in BRAND_FILES.items():
            f = request.FILES.get(key)
            if f is not None:
                if (getattr(f, 'content_type', '') or '').split('/')[0] != mime:
                    return _error('invalid_payload', f'{key}: не тот вид файла',
                                  http_status.HTTP_400_BAD_REQUEST)
                if f.size > MAX_BYTES[StudioAsset.Kind.MUSIC]:
                    return _error('invalid_payload', f'{key}: не больше 20 МБ',
                                  http_status.HTTP_400_BAD_REQUEST)
                setattr(style, key, f)
                changed.append(StudioStyle._meta.get_field(key).verbose_name.lower())
            elif str(data.get(f'remove_{key}') or '') in ('1', 'true'):
                setattr(style, key, None)
                changed.append(StudioStyle._meta.get_field(key).verbose_name.lower())
        if not changed:
            return _error('invalid_payload', 'нечего менять', http_status.HTTP_400_BAD_REQUEST)
        note = str(data.get('note') or '').strip() or 'Изменено: ' + ', '.join(changed)
        studio.bump(style, note, _author(request.user))
        return Response(style_to_dict(request, style))


class StudioStyleRememberAPIView(APIView):
    """POST marketer/studio/style/remember/ {text} — запомнить правило; {forget: индекс} — убрать."""
    permission_classes = PERMS

    def post(self, request):
        if not _may_write(request.user):
            return _deny('правила бренда меняет')
        data = request.data or {}
        try:
            if 'forget' in data:
                style = studio.forget(int(data.get('forget')), _author(request.user))
            else:
                style = studio.remember(str(data.get('text') or ''), _author(request.user))
        except (TypeError, ValueError) as exc:
            return _error('invalid_payload', str(exc), http_status.HTTP_400_BAD_REQUEST)
        return Response(style_to_dict(request, style))


class StudioStyleVersionsAPIView(APIView):
    """GET marketer/studio/style/versions/ — история стиля; POST {version} — вернуть."""
    permission_classes = PERMS

    def get(self, request):
        rows = StudioStyleVersion.objects.order_by('-version')[:100]
        return Response({'results': [{'version': r.version, 'note': r.note, 'created_by': r.created_by,
                                      'created_at': _iso(r.created_at)} for r in rows]})

    def post(self, request):
        if not _may_write(request.user):
            return _deny('стиль роликов меняет')
        try:
            style = studio.restore_style(int((request.data or {}).get('version')), _author(request.user))
        except (TypeError, ValueError) as exc:
            return _error('invalid_payload', str(exc), http_status.HTTP_400_BAD_REQUEST)
        return Response(style_to_dict(request, style))


# ── материалы ────────────────────────────────────────────────────────────────

class StudioAssetListAPIView(APIView):
    """GET marketer/studio/assets/?project=<id>|library — материалы.
    POST multipart file, kind (video|photo|music), title, project_id?, duration?"""
    permission_classes = PERMS

    def get(self, request):
        qs = StudioAsset.objects.order_by('-pk')
        pid = request.query_params.get('project')
        if pid == 'library':
            qs = qs.filter(project__isnull=True)
        elif pid:
            qs = qs.filter(project_id=pid)
        return Response({'results': [asset_to_dict(request, a) for a in qs[:300]]})

    def post(self, request):
        if not _may_write(request.user):
            return _deny('материалы добавляет')
        if request.data.get('photo_id') not in (None, ''):
            return self._из_фотобанка(request)
        f = request.FILES.get('file')
        kind = str(request.data.get('kind') or '')
        if f is None or kind not in MIME:
            return _error('invalid_payload', 'file и kind (video, photo, music)',
                          http_status.HTTP_400_BAD_REQUEST)
        if (getattr(f, 'content_type', '') or '').split('/')[0] != MIME[kind]:
            return _error('invalid_payload', 'файл не того вида', http_status.HTTP_400_BAD_REQUEST)
        if f.size > MAX_BYTES[kind]:
            mb = MAX_BYTES[kind] // (1024 * 1024)
            return _error('too_large', f'не больше {mb} МБ', http_status.HTTP_400_BAD_REQUEST)
        project = None
        pid = request.data.get('project_id')
        if pid not in (None, ''):
            project = StudioProject.objects.filter(pk=pid).first()
            if project is None:
                return _error('not_found', 'Ролик не найден', http_status.HTTP_404_NOT_FOUND)
        try:
            duration = float(request.data.get('duration')) if request.data.get('duration') else None
        except (TypeError, ValueError):
            duration = None
        asset = StudioAsset.objects.create(
            project=project, kind=kind, file=f, title=str(request.data.get('title') or '').strip()[:200],
            duration=duration, size=f.size, created_by=_author(request.user))
        return Response(asset_to_dict(request, asset), status=http_status.HTTP_201_CREATED)


    def _из_фотобанка(self, request):
        """Фото из фотобанка маркетолога (22.10.2026, макет «можно из фотобанка»): файл общий,
        строка своя. Удаление материала файл фотобанка не трогает (см. `_drop_file`)."""
        from apps.tenant.marketer.models import MarketerPhoto
        photo = MarketerPhoto.objects.filter(pk=str(request.data.get('photo_id'))).first() \
            if str(request.data.get('photo_id')).isdigit() else None
        if photo is None or not photo.image:
            return _error('not_found', 'Фото не найдено в фотобанке', http_status.HTTP_404_NOT_FOUND)
        project = None
        pid = request.data.get('project_id')
        if pid not in (None, ''):
            project = StudioProject.objects.filter(pk=pid).first()
            if project is None:
                return _error('not_found', 'Ролик не найден', http_status.HTTP_404_NOT_FOUND)
        asset = StudioAsset.objects.create(
            project=project, kind=StudioAsset.Kind.PHOTO, file=photo.image.name,
            title=(photo.title or '')[:200], size=getattr(photo.image, 'size', 0) or 0,
            created_by=_author(request.user))
        return Response(asset_to_dict(request, asset), status=http_status.HTTP_201_CREATED)


class StudioAssetDetailAPIView(APIView):
    """PATCH/DELETE marketer/studio/assets/<id>/ — подпись, привязка к ролику; удалить."""
    permission_classes = PERMS

    def patch(self, request, pk: int):
        if not _may_write(request.user):
            return _deny('материалы правит')
        asset = StudioAsset.objects.filter(pk=pk).first()
        if asset is None:
            return _error('not_found', 'Материал не найден', http_status.HTTP_404_NOT_FOUND)
        data = request.data or {}
        if 'title' in data:
            asset.title = str(data.get('title') or '').strip()[:200]
        if 'project_id' in data:
            pid = data.get('project_id')
            asset.project = StudioProject.objects.filter(pk=pid).first() if pid else None
        asset.save()
        return Response(asset_to_dict(request, asset))

    def delete(self, request, pk: int):
        if not _may_write(request.user):
            return _deny('материалы удаляет')
        asset = StudioAsset.objects.filter(pk=pk).first()
        if asset is None:
            return _error('not_found', 'Материал не найден', http_status.HTTP_404_NOT_FOUND)
        # Готовые ролики хранят свои MP4 — удаление исходника их не трогает.
        _drop_file(asset)
        asset.delete()
        return Response(status=http_status.HTTP_204_NO_CONTENT)


# ── ролики ───────────────────────────────────────────────────────────────────

class StudioProjectListAPIView(APIView):
    """GET marketer/studio/projects/ — ролики; POST {title, kind, brief, duration, asset_ids} — новый."""
    permission_classes = PERMS

    def get(self, request):
        qs = (StudioProject.objects.select_related('current_version')
              .prefetch_related('current_version__files').order_by('-updated_at')[:100])
        серии = sorted({x for x in StudioProject.objects.exclude(series='').values_list('series', flat=True)})
        return Response({'results': [project_to_dict(request, p) for p in qs], 'series': серии,
                         'style_version': studio.style_obj().version})

    def post(self, request):
        if not _may_write(request.user):
            return _deny('ролики делает')
        data = request.data or {}
        brief = str(data.get('brief') or '').strip()
        if not brief:
            return _error('invalid_payload', 'brief: что снять, словами', http_status.HTTP_400_BAD_REQUEST)
        kind = data.get('kind') or StudioProject.Kind.REEL
        if kind not in StudioProject.Kind.values:
            return _error('invalid_payload', 'kind: reel, stories или banner', http_status.HTTP_400_BAD_REQUEST)
        try:
            duration = int(data.get('duration') or 20)
        except (TypeError, ValueError):
            duration = 20
        duration = min(max(duration, studio.MIN_DURATION), studio.MAX_DURATION)
        ids = data.get('asset_ids') or []
        if not isinstance(ids, list):
            return _error('invalid_payload', 'asset_ids: список', http_status.HTTP_400_BAD_REQUEST)
        title = str(data.get('title') or '').strip()[:200] or brief.split('\n')[0][:60]
        project = StudioProject.objects.create(
            title=title, kind=kind, brief=brief[:MAX_BRIEF], duration=duration,
            status=StudioProject.Status.WRITING, style_version=studio.style_obj().version,
            created_by=_author(request.user), series=str(data.get('series') or '').strip()[:120],
            author_checkup_id=studio_mobile.checkup_id_of(request.user))
        # Из библиотеки сети — копия строки (файл общий): ролик видит свои материалы.
        for asset in StudioAsset.objects.filter(pk__in=[i for i in ids if str(i).isdigit()]):
            if asset.project_id is None:
                asset.pk = None
                asset.project = project
                asset.save()
            elif asset.project_id != project.pk:
                continue
        _queue(project, author=_author(request.user))
        return Response(project_to_dict(request, project, full=True), status=http_status.HTTP_202_ACCEPTED)


class StudioProjectDetailAPIView(APIView):
    """GET marketer/studio/projects/<id>/ — ролик целиком; PATCH {title}; DELETE."""
    permission_classes = PERMS

    def get(self, request, pk: int):
        project = _project(pk)
        if project is None:
            return _error('not_found', 'Ролик не найден', http_status.HTTP_404_NOT_FOUND)
        return Response(project_to_dict(request, project, full=True))

    def patch(self, request, pk: int):
        if not _may_write(request.user):
            return _deny('ролики правит')
        project = _project(pk)
        if project is None:
            return _error('not_found', 'Ролик не найден', http_status.HTTP_404_NOT_FOUND)
        data = request.data or {}
        fields = []
        if 'title' in data:
            title = str(data.get('title') or '').strip()[:200]
            if not title:
                return _error('invalid_payload', 'title', http_status.HTTP_400_BAD_REQUEST)
            project.title = title
            fields.append('title')
        if 'post_text' in data:
            project.post_text = str(data.get('post_text') or '').strip()[:5000]
            fields.append('post_text')
        if 'series' in data:
            project.series = str(data.get('series') or '').strip()[:120]
            fields.append('series')
        if not fields:
            return _error('invalid_payload', 'нечего менять', http_status.HTTP_400_BAD_REQUEST)
        project.save(update_fields=fields + ['updated_at'])
        return Response(project_to_dict(request, project, full=True))

    def delete(self, request, pk: int):
        if not _may_write(request.user):
            return _deny('ролики удаляет')
        project = _project(pk)
        if project is None:
            return _error('not_found', 'Ролик не найден', http_status.HTTP_404_NOT_FOUND)
        for v in project.versions.all():
            for f in v.files.all():
                f.file.delete(save=False)
                if f.poster:
                    f.poster.delete(save=False)
        for a in project.assets.all():
            _drop_file(a)
        project.delete()
        return Response(status=http_status.HTTP_204_NO_CONTENT)


class _ProjectAction(APIView):
    permission_classes = PERMS
    what = 'ролики правит'

    def post(self, request, pk: int):
        if not _may_write(request.user):
            return _deny(self.what)
        project = _project(pk)
        if project is None:
            return _error('not_found', 'Ролик не найден', http_status.HTTP_404_NOT_FOUND)
        return self.act(request, project, request.data or {})

    def act(self, request, project, data):  # pragma: no cover
        raise NotImplementedError


class StudioAnswerAPIView(_ProjectAction):
    """POST …/answer/ {answer} — ответ на вопрос ИИ, сценарий пишется заново."""

    def act(self, request, project, data):
        answer = str(data.get('answer') or '').strip()
        if not answer:
            return _error('invalid_payload', 'answer', http_status.HTTP_400_BAD_REQUEST)
        if project.status in BUSY:
            return _error('busy', 'ИИ ещё пишет', http_status.HTTP_409_CONFLICT)
        project.answer = (project.answer + '\n' + answer).strip()[:2000] if project.answer else answer[:2000]
        project.save(update_fields=['answer', 'updated_at'])
        had_version = project.current_version_id is not None
        if had_version and project.current_version.instruction:
            # Вопрос был на правку — та же правка с ответом.
            _queue(project, instruction=project.current_version.instruction + '\nУточнение: ' + answer,
                   author=_author(request.user))
        else:
            _queue(project, author=_author(request.user))
        return Response(project_to_dict(request, project, full=True), status=http_status.HTTP_202_ACCEPTED)


class StudioEditAPIView(_ProjectAction):
    """POST …/edit/ {instruction, scene?} — правка словами → новая версия."""

    def act(self, request, project, data):
        text = str(data.get('instruction') or '').strip()
        if not text:
            return _error('invalid_payload', 'instruction: что поменять', http_status.HTTP_400_BAD_REQUEST)
        if project.status in BUSY:
            return _error('busy', 'ИИ ещё пишет', http_status.HTTP_409_CONFLICT)
        if project.current_version_id is None:
            return _error('no_version', 'сначала дождитесь первой версии', http_status.HTTP_409_CONFLICT)
        scene = data.get('scene')
        try:
            scene = int(scene) if scene not in (None, '') else None
        except (TypeError, ValueError):
            scene = None
        _queue(project, instruction=text[:2000], scene=scene, author=_author(request.user))
        return Response(project_to_dict(request, project, full=True), status=http_status.HTTP_202_ACCEPTED)


class StudioRestoreAPIView(_ProjectAction):
    """POST …/restore/ {number} — вернуть прежнюю версию (без пересборки)."""

    def act(self, request, project, data):
        try:
            studio.restore(project, int(data.get('number')))
        except (TypeError, ValueError) as exc:
            return _error('invalid_payload', str(exc), http_status.HTTP_400_BAD_REQUEST)
        return Response(project_to_dict(request, _project(project.pk), full=True))


class StudioRerenderAPIView(_ProjectAction):
    """POST …/rerender/ — собрать тот же сценарий заново (сборщик упал)."""

    def act(self, request, project, data):
        try:
            studio.rerender(project)
        except ValueError as exc:
            return _error('no_version', str(exc), http_status.HTTP_409_CONFLICT)
        return Response(project_to_dict(request, _project(project.pk), full=True))


class StudioRememberAPIView(_ProjectAction):
    """POST …/remember/ {text?} — «запомнить для бренда» (по умолчанию — подсказка версии)."""
    what = 'правила бренда меняет'

    def act(self, request, project, data):
        text = str(data.get('text') or '').strip()
        cur = project.current_version
        if not text and cur:
            text = cur.remember_suggestion
        if not text:
            return _error('invalid_payload', 'text: что запомнить', http_status.HTTP_400_BAD_REQUEST)
        style = studio.remember(text, _author(request.user))
        if cur and cur.remember_suggestion:
            cur.remember_suggestion = ''
            cur.save(update_fields=['remember_suggestion', 'updated_at'])
        return Response({'style': style_to_dict(request, style),
                         'project': project_to_dict(request, _project(project.pk), full=True)})


class StudioApprovalAPIView(_ProjectAction):
    """POST …/approval/ {action: submit|approve|reject, comment?} — согласование ролика."""
    what = 'ролики согласует'

    def act(self, request, project, data):
        action = data.get('action')
        A = StudioProject.Approval
        if action == 'submit':
            if project.status != StudioProject.Status.READY:
                return _error('not_ready', 'ролик ещё не собран', http_status.HTTP_409_CONFLICT)
            project.approval = A.WAITING
        elif action == 'approve':
            if project.status != StudioProject.Status.READY:
                return _error('not_ready', 'ролик ещё не собран', http_status.HTTP_409_CONFLICT)
            project.approval = A.APPROVED
            project.approved_by = _author(request.user)
            project.approved_at = timezone.now()
        elif action == 'reject':
            project.approval = A.REJECTED
            comment = str(data.get('comment') or '').strip()
            if comment:
                project.save(update_fields=['approval', 'updated_at'])
                if project.status not in BUSY and project.current_version_id:
                    _queue(project, instruction=comment[:2000], author=_author(request.user))
                return Response(project_to_dict(request, _project(project.pk), full=True))
        else:
            return _error('invalid_payload', 'action: submit, approve или reject',
                          http_status.HTTP_400_BAD_REQUEST)
        project.save(update_fields=['approval', 'approved_by', 'approved_at', 'updated_at'])
        return Response(project_to_dict(request, _project(project.pk), full=True))


class StudioPublishAPIView(_ProjectAction):
    """POST …/publish/ {rows: [{channel: vk|tg, fmt?, at?}], text?} — в план публикации.
    {cancel: <id строки>} — снять строку из плана. Публикует тик маркетолога (раз в 5 минут)."""
    what = 'ролики публикует'

    def act(self, request, project, data):
        from apps.tenant.marketer import studio_publish as sp
        try:
            if data.get('cancel'):
                sp.отменить(project, str(data['cancel']))
            else:
                rows = data.get('rows')
                if not isinstance(rows, list) or not rows:
                    return _error('invalid_payload', 'rows: куда и когда', http_status.HTTP_400_BAD_REQUEST)
                text = data.get('text')
                sp.запланировать(project, rows, None if text is None else str(text))
        except sp.PublishError as exc:
            return _error('not_publishable', str(exc), http_status.HTTP_409_CONFLICT)
        return Response(project_to_dict(request, _project(project.pk), full=True))


# ── студия в телефоне по макету (22.10.2026) ────────────────────────────────

class StudioStopAPIView(_ProjectAction):
    """POST …/stop/ — «Остановить» на экране «Собираем ролик».

    Сценарий пишется → ролик помечается остановленным (готовую версию задача не
    переиграет: её проверка статуса ниже). Версия в очереди или в сборке →
    «Остановлено»; сборщику после этого сервер не даёт отчитываться (он
    принимает только версию в сборке). Была прежняя готовая версия — ролик
    возвращается к ней.
    """
    what = 'сборку останавливает'

    def act(self, request, project, data):
        from apps.tenant.marketer.models import StudioVersion
        S = StudioProject.Status
        v = project.current_version
        if project.status not in (S.WRITING, S.ASKING, S.QUEUED, S.RENDERING):
            return _error('not_busy', 'ролик сейчас не собирается', http_status.HTTP_409_CONFLICT)
        if v is not None and v.status in (StudioVersion.Status.QUEUED, StudioVersion.Status.RENDERING) \
                and project.status in (S.QUEUED, S.RENDERING):
            v.status = StudioVersion.Status.FAILED
            v.error = f'Остановлено: {_author(request.user)}'[:2000]
            v.stage = 'Остановлено'
            v.finished_at = timezone.now()
            v.save(update_fields=['status', 'error', 'stage', 'finished_at', 'updated_at'])
        прежняя = (project.versions.filter(status=StudioVersion.Status.READY)
                   .exclude(pk=getattr(v, 'pk', None)).order_by('-number').first())
        if прежняя is not None:
            project.current_version = прежняя
            project.status = S.READY
            project.error = ''
        else:
            project.status = S.FAILED
            project.error = 'Сборку остановили'
        project.question = ''
        project.save(update_fields=['current_version', 'status', 'error', 'question', 'updated_at'])
        return Response(project_to_dict(request, _project(project.pk), full=True))


class StudioSubmitPlanAPIView(_ProjectAction):
    """POST …/submit-plan/ — «На согласование и в контент-план».

    Ролик уходит на согласование с ПРЕДЛОЖЕННЫМ планом (ВК клип + пост и Телеграм
    в свободный слот, сторис 6 с на следующий день). Ничего не публикуется, пока
    согласующий не нажмёт «Опубликовать» (в пуше или на экране).
    """
    what = 'ролики отправляет на согласование'

    def act(self, request, project, data):
        if project.status != StudioProject.Status.READY:
            return _error('not_ready', 'ролик ещё не собран', http_status.HTTP_409_CONFLICT)
        project.proposed_plan = studio_mobile.propose_plan(project)
        project.approval = StudioProject.Approval.WAITING
        if data.get('post_text') is not None:
            project.post_text = str(data.get('post_text') or '').strip()[:5000]
        project.save(update_fields=['proposed_plan', 'approval', 'post_text', 'updated_at'])
        studio_mobile.send_event_later(project, 'approval')
        return Response(project_to_dict(request, _project(project.pk), full=True))


class StudioPublishPlanAPIView(_ProjectAction):
    """POST …/publish-plan/ {confirm: true} — «Опубликовать»: согласовать и поставить
    предложенный план в публикацию. Посты выйдут сами в своё время."""
    what = 'ролики публикует'

    def act(self, request, project, data):
        from apps.tenant.marketer import studio_publish as sp
        if data.get('confirm') is not True:
            return _error('confirm_required', 'нужно подтверждение публикации', http_status.HTTP_400_BAD_REQUEST)
        if project.status != StudioProject.Status.READY:
            return _error('not_ready', 'ролик ещё не собран', http_status.HTTP_409_CONFLICT)
        rows = project.proposed_plan or studio_mobile.propose_plan(project)
        уже = {(r.get('channel'), r.get('version')) for r in (project.publish_plan or [])
               if r.get('status') in ('scheduled', 'published')}
        номер = project.current_version.number if project.current_version else None
        rows = [r for r in rows if (r.get('channel'), номер) not in уже]
        project.approval = StudioProject.Approval.APPROVED
        project.approved_by = _author(request.user)
        project.approved_at = timezone.now()
        project.save(update_fields=['approval', 'approved_by', 'approved_at', 'updated_at'])
        try:
            if rows:
                sp.запланировать(project, rows, None)
        except sp.PublishError as exc:
            return _error('not_publishable', str(exc), http_status.HTTP_409_CONFLICT)
        project.proposed_plan = []
        project.save(update_fields=['proposed_plan', 'updated_at'])
        return Response(project_to_dict(request, _project(project.pk), full=True))
