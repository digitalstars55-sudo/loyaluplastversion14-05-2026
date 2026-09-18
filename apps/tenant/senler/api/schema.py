"""
Схема OpenAPI для ручек рассылок (контракт платформы, №11 и №13) — только описание.

Вьюхи в broadcasts.py собирают JSON функциями serializers.py, а не
DRF-сериализаторами (модели тенантные, тесты на моках), поэтому drf-spectacular
видел у них пустые запросы и ответы: в срезе для CheckUp ручки рассылок шли без
формы (unknown в .d.ts). Здесь та же форма описана inline-сериализаторами,
которые в рантайме НЕ вызываются — на поведение ручек модуль не влияет.

Источник истины по форме — записи docs/platform/fixtures/w1/broadcasts_*.json
(перезаписываются на dev в транзакции с откатом). Меняя форму ответа, править
serializers.py, фикстуры и этот файл; удаление поля = новая версия контракта.
"""
from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers as s

from apps.tenant.senler.models import DraftMode, DraftStatus, GenderFilter, SendStatus, TriggerType

# Те же значения, что DEFAULT_LIMIT / MAX_LIMIT / MAX_TEXT_LEN в broadcasts.py
# (импорт оттуда невозможен: broadcasts.py импортирует этот модуль).
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_TEXT_LEN = 4096

ERROR_CODES = [
    ('not_found',               '404 — черновик/запуск не найден или вне точек пользователя'),
    ('invalid_payload',         '400 — тело запроса не разобрано'),
    ('branch_forbidden',        '403 — точка вне доступа пользователя'),
    ('segment_not_found',       '404 — нет такого RF-сегмента'),
    ('not_a_draft',             '409 — черновик уже отправлен или в архиве'),
    ('expected_count_required', '400 — нет expected_count из предпросмотра'),
    ('confirm_required',        '400 — нет confirm=true'),
    ('empty_audience',          '400 — аудитория пуста'),
    ('audience_changed',        '409 — аудитория разошлась с показанной (+expected, +actual)'),
    ('already_sent',            '409 — черновик уже отправлен'),
    ('not_cancellable',         '409 — запуск не в статусе pending/running'),
    ('not_editable',            '409 — правка текста в ВК невозможна'),
    ('not_deletable',           '409 — удаление в ВК невозможно'),
]


def _error(name: str = 'BroadcastApiError', **extra):
    fields = {'code': s.ChoiceField(choices=ERROR_CODES), 'detail': s.CharField()}
    fields.update(extra)
    return inline_serializer(name, fields=fields)


ERROR = _error()
AUDIENCE_CHANGED = _error(
    'BroadcastAudienceChanged',
    expected=s.IntegerField(help_text='сколько показал предпросмотр'),
    actual=s.IntegerField(help_text='сколько сейчас'),
)


# Фабрики: один и тот же набор полей → один компонент схемы; отдельные
# экземпляры нужны для many=True / allow_null=True.
def _segment(**kw):
    return inline_serializer('BroadcastSegment', fields={
        'id':    s.IntegerField(),
        'code':  s.CharField(),
        'name':  s.CharField(),
        'emoji': s.CharField(allow_blank=True),
    }, **kw)


def _period(**kw):
    return inline_serializer('BroadcastPeriod', fields={
        'start': s.DateField(allow_null=True),
        'end':   s.DateField(allow_null=True),
    }, **kw)


def _variant(**kw):
    return inline_serializer('BroadcastVariant', fields={
        'message_text': s.CharField(),
        'percent':      s.IntegerField(min_value=0, max_value=100),
    }, **kw)


def _draft(**kw):
    return inline_serializer('BroadcastDraft', fields={
        'id':            s.IntegerField(),
        'name':          s.CharField(allow_blank=True),
        'message_text':  s.CharField(allow_blank=True),
        'image':         s.CharField(allow_null=True, help_text='всегда null — картинки в v1 не поддержаны'),
        'mode':          s.ChoiceField(choices=DraftMode.choices),
        'segment':       _segment(allow_null=True),
        'r_score':       s.IntegerField(allow_null=True),
        'f_score':       s.IntegerField(allow_null=True),
        'period':        _period(),
        'branch_ids':    s.ListField(child=s.IntegerField(), help_text='ВНУТРЕННИЕ Branch.id (не публичные branch_id)'),
        'gender_filter': s.ChoiceField(choices=GenderFilter.choices),
        'variants':      _variant(many=True),
        'status':        s.ChoiceField(choices=DraftStatus.choices),
        'created_by':    s.CharField(allow_blank=True),
        'sent_at':       s.DateTimeField(allow_null=True),
        'last_send_id':  s.IntegerField(allow_null=True),
        'created_at':    s.DateTimeField(),
        'updated_at':    s.DateTimeField(),
    }, **kw)


def _draft_write(name: str, create: bool):
    """Тело POST (message_text и branch_ids обязательны) и PATCH (всё по желанию)."""
    return inline_serializer(name, fields={
        'name':          s.CharField(required=False, allow_blank=True, max_length=200),
        'message_text':  s.CharField(required=create, max_length=MAX_TEXT_LEN),
        'branch_ids':    s.ListField(child=s.IntegerField(), required=create,
                                     help_text='внутренние Branch.id; все должны быть в доступе пользователя'),
        'mode':          s.ChoiceField(choices=DraftMode.choices, required=False, help_text='неизвестное → restaurant'),
        'gender_filter': s.ChoiceField(choices=GenderFilter.choices, required=False, help_text='неизвестное → all'),
        'segment_id':    s.IntegerField(required=False, allow_null=True),
        'r_score':       s.IntegerField(required=False, allow_null=True),
        'f_score':       s.IntegerField(required=False, allow_null=True),
        'start':         s.DateField(required=False, allow_null=True),
        'end':           s.DateField(required=False, allow_null=True),
        'variants':      _variant(many=True, required=False),
        'status':        s.ChoiceField(choices=[c for c in DraftStatus.choices if c[0] != DraftStatus.SENT],
                                       required=False, help_text='только draft и archived'),
    })


def _send(**kw):
    return inline_serializer('BroadcastSend', fields={
        'id':               s.IntegerField(),
        'status':           s.ChoiceField(choices=SendStatus.choices),
        'branch_id':        s.IntegerField(allow_null=True, help_text='внутренний Branch.id'),
        'branch_name':      s.CharField(allow_blank=True),
        'name':             s.CharField(allow_blank=True),
        'message_text':     s.CharField(allow_blank=True, help_text='обрезан до 200 символов'),
        'trigger_type':     s.ChoiceField(choices=TriggerType.choices),
        'triggered_by':     s.CharField(allow_blank=True),
        'started_at':       s.DateTimeField(allow_null=True),
        'finished_at':      s.DateTimeField(allow_null=True),
        'recipients_count': s.IntegerField(),
        'sent_count':       s.IntegerField(),
        'failed_count':     s.IntegerField(),
        'skipped_count':    s.IntegerField(),
        'read_count':       s.IntegerField(),
        'created_at':       s.DateTimeField(),
        'can_cancel':       s.BooleanField(),
        'can_edit_in_vk':   s.BooleanField(help_text='done, есть доставленные, окно ВК 24 ч'),
        'can_delete_in_vk': s.BooleanField(),
    }, **kw)


def _page(name: str, results):
    return inline_serializer(name, fields={
        'total':   s.IntegerField(),
        'limit':   s.IntegerField(),
        'offset':  s.IntegerField(),
        'results': results,
    })


DRAFT = _draft()
DRAFT_LIST = _page('BroadcastDraftList', _draft(many=True))
DRAFT_CREATE_BODY = _draft_write('BroadcastDraftCreate', create=True)
DRAFT_PATCH_BODY = _draft_write('BroadcastDraftPatch', create=False)

PREVIEW = inline_serializer('BroadcastPreview', fields={
    'count':     s.IntegerField(help_text='вернуть как expected_count в POST …/send/'),
    'by_branch': inline_serializer('BroadcastPreviewBranch', fields={
        'branch_id': s.IntegerField(help_text='внутренний Branch.id'),
        'name':      s.CharField(),
        'count':     s.IntegerField(),
    }, many=True),
    'mode':      s.ChoiceField(choices=DraftMode.choices),
    'segment':   _segment(allow_null=True),
    'period':    _period(),
})

SEND_BODY = inline_serializer('BroadcastSendRequest', fields={
    'expected_count': s.IntegerField(help_text='count из предпросмотра'),
    'confirm':        s.BooleanField(help_text='true — человек видел цифру'),
})
SEND_RESULT = inline_serializer('BroadcastSendResult', fields={
    'ok':               s.BooleanField(),
    'queued':           s.BooleanField(help_text='true — >30 получателей, ушло одним celery-таском, в sends статус queued'),
    'sends':            inline_serializer('BroadcastSendResultRow', fields={
        'id':          s.IntegerField(help_text='id запуска (BroadcastSend)'),
        'branch_id':   s.IntegerField(help_text='внутренний Branch.id'),
        'branch_name': s.CharField(),
        'variant':     s.CharField(required=False, help_text='только при A/B: «#1 (50%)»'),
        'recipients':  s.IntegerField(),
        'status':      s.CharField(help_text='queued | ' + ' | '.join(v for v, _ in SendStatus.choices)),
        'sent':        s.IntegerField(required=False, help_text='только при синхронной отправке (≤30)'),
        'failed':      s.IntegerField(required=False),
        'skipped':     s.IntegerField(required=False),
        'error':       s.CharField(required=False, allow_blank=True),
    }, many=True),
    'total_recipients': s.IntegerField(),
})

SEND_LIST = _page('BroadcastSendList', _send(many=True))
CONFIRM_BODY = inline_serializer('BroadcastConfirmRequest', fields={'confirm': s.BooleanField()})
CANCEL_RESULT = inline_serializer('BroadcastCancelResult', fields={
    'ok':     s.BooleanField(),
    'status': s.ChoiceField(choices=SendStatus.choices),
    'note':   s.CharField(help_text='отмена не мгновенная: до 10 сообщений ещё может уйти'),
})
EDIT_IN_VK_BODY = inline_serializer('BroadcastEditInVkRequest', fields={
    'confirm':      s.BooleanField(),
    'message_text': s.CharField(max_length=MAX_TEXT_LEN),
})
EDIT_IN_VK_RESULT = inline_serializer('BroadcastEditInVkResult', fields={
    'ok':      s.BooleanField(),
    'updated': s.IntegerField(),
    'skipped': s.ListField(child=s.CharField(), help_text='«vk<id>: причина» — старше 24 ч'),
    'errors':  s.ListField(child=s.CharField()),
})
DELETE_IN_VK_RESULT = inline_serializer('BroadcastDeleteInVkResult', fields={
    'ok':      s.BooleanField(),
    'status':  s.ChoiceField(choices=SendStatus.choices),
    'deleted': s.IntegerField(),
    'skipped': s.ListField(child=s.CharField()),
    'errors':  s.ListField(child=s.CharField()),
})
OK = inline_serializer('BroadcastOk', fields={'ok': s.BooleanField()})

_TAG = ['broadcasts']
_PAGE_PARAMS = [
    OpenApiParameter('limit', int, description=f'1..{MAX_LIMIT}, по умолчанию {DEFAULT_LIMIT}'),
    OpenApiParameter('offset', int, description='с нуля'),
]


def _err(description: str) -> OpenApiResponse:
    return OpenApiResponse(response=ERROR, description=description)


# ── Декораторы для broadcasts.py (по одному на HTTP-метод) ───────────────────
draft_list = extend_schema(
    tags=_TAG, summary='Черновики рассылок (limit/offset/total)',
    parameters=_PAGE_PARAMS + [OpenApiParameter('status', str, enum=[v for v, _ in DraftStatus.choices],
                                                description='фильтр по статусу')],
    responses={200: DRAFT_LIST},
)
draft_create = extend_schema(
    tags=_TAG, summary='Создать черновик', request=DRAFT_CREATE_BODY,
    responses={201: DRAFT, 400: _err('invalid_payload'), 403: _err('branch_forbidden'), 404: _err('segment_not_found')},
)
draft_get = extend_schema(
    tags=_TAG, summary='Карточка черновика',
    responses={200: DRAFT, 404: _err('not_found — чужой или несуществующий')},
)
draft_patch = extend_schema(
    tags=_TAG, summary='Изменить черновик (только переданные поля)', request=DRAFT_PATCH_BODY,
    responses={200: DRAFT, 400: _err('invalid_payload'), 403: _err('branch_forbidden'),
               404: _err('not_found / segment_not_found'), 409: _err('not_a_draft — уже отправлен')},
)
draft_delete = extend_schema(
    tags=_TAG, summary='Удалить черновик',
    responses={200: OK, 404: _err('not_found'), 409: _err('not_a_draft — отправленный не удаляется')},
)
preview = extend_schema(
    tags=_TAG, summary='Предпросмотр аудитории (count → expected_count)',
    responses={200: PREVIEW, 404: _err('not_found')},
)
send = extend_schema(
    tags=_TAG, summary='Отправить черновик (expected_count + confirm обязательны)', request=SEND_BODY,
    responses={200: SEND_RESULT,
               400: _err('expected_count_required / confirm_required / empty_audience'),
               404: _err('not_found'),
               409: OpenApiResponse(response=AUDIENCE_CHANGED,
                                    description='audience_changed (+expected, +actual) / already_sent / not_a_draft')},
)
sends_list = extend_schema(
    tags=_TAG, summary='История запусков (только по точкам пользователя)',
    parameters=_PAGE_PARAMS + [OpenApiParameter('branch_id', int, description='внутренний Branch.id')],
    responses={200: SEND_LIST, 400: _err('invalid_payload — branch_id не число')},
)
send_cancel = extend_schema(
    tags=_TAG, summary='Отменить запуск (pending/running)', request=CONFIRM_BODY,
    responses={200: CANCEL_RESULT, 400: _err('confirm_required'), 404: _err('not_found'), 409: _err('not_cancellable')},
)
send_edit_in_vk = extend_schema(
    tags=_TAG, summary='Изменить текст доставленных сообщений в ВК (окно 24 ч)', request=EDIT_IN_VK_BODY,
    responses={200: EDIT_IN_VK_RESULT, 400: _err('confirm_required / invalid_payload'),
               404: _err('not_found'), 409: _err('not_editable')},
)
send_delete_in_vk = extend_schema(
    tags=_TAG, summary='Удалить доставленные сообщения в ВК (окно 24 ч)', request=CONFIRM_BODY,
    responses={200: DELETE_IN_VK_RESULT, 400: _err('confirm_required'), 404: _err('not_found'), 409: _err('not_deletable')},
)
