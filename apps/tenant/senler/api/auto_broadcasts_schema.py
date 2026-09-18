"""
Схема OpenAPI для конструктора авторассылок (контракт №31) — только описание.

Вьюхи auto_broadcasts.py собирают JSON функциями (модели тенантные, тесты на
моках), поэтому drf-spectacular видел бы у них пустые запросы и ответы, а срез
схемы для CheckUp выходил бы с unknown в .d.ts. Здесь та же форма описана
inline-сериализаторами, которые в рантайме НЕ вызываются — на поведение ручек
модуль не влияет.

Меняя форму ответа, править auto_broadcasts_serializers.py, фикстуры
docs/platform/fixtures/w2/ и этот файл; удаление поля = новая версия контракта.
"""
from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers as s

from apps.tenant.senler.models import AutoBroadcastType, FollowUpCondition, GenderFilter, RecipientStatus

from .auto_broadcasts_serializers import GIFT_TIER_CODES, MAX_NAME_LEN, MAX_TEXT_LEN, MAX_VARIANT_NAME_LEN

# Те же значения, что DEFAULT_LIMIT / MAX_LIMIT в auto_broadcasts.py
# (импорт оттуда невозможен: тот модуль импортирует этот).
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

EVENT_CHOICES = [v for v, _ in AutoBroadcastType.choices]

ERROR_CODES = [
    ('not_found',               '404 — правило/вариант/точка/сегмент не найдены или вне доступа'),
    ('invalid_payload',         '400 — тело запроса не разобрано (в т.ч. пустой branch_ids у роли с ограничением)'),
    ('event_unknown',           '400 — нет такого события'),
    ('delay_required',          '400 — у события нет задержки по умолчанию, нужен delay_days'),
    ('variant_weights_invalid', '400 — вариант A/B: weight < 1, пустое имя/текст или текст > 4096'),
    ('reward_invalid',          '400 — gift_tier не из списка или отрицательный срок'),
    ('segment_not_found',       '404 — нет такого RF-сегмента'),
    ('use_activate',            '400 — пользователь CheckUp включает правило только через activate/'),
    ('archived',                '409 — правило в архиве'),
    ('already_active',          '409 — правило уже включено'),
    ('expected_count_required', '400 — нет expected_count из предпросмотра'),
    ('confirm_required',        '400 — нет confirm=true'),
    ('audience_empty',          '400 — правило сейчас никому не отправит'),
    ('audience_changed',        '409 — аудитория разошлась с показанной (+expected, +actual)'),
    ('preview_failed',          '409 — расчёт аудитории упал (включать тоже нельзя)'),
    ('has_sends',               '409 — у варианта есть отправки, удалять нельзя (выключите)'),
    ('guest_not_found',         '400 — гость с таким vk_id не найден в доступных точках'),
    ('not_subscribed',          '400 — гость не разрешил сообщения сообщества'),
    ('no_vk_token',             '409 — у точки гостя нет активного сообщества ВК'),
    ('rate_limited',            '409 — тест-отправка не чаще раза в минуту на правило'),
    ('vk_error',                '502 — ВК не принял сообщение, текст ответа в detail'),
]


def _error(name: str = 'AutoBroadcastApiError', **extra):
    fields = {'code': s.ChoiceField(choices=ERROR_CODES), 'detail': s.CharField()}
    fields.update(extra)
    return inline_serializer(name, fields=fields)


ERROR = _error()
AUDIENCE_CHANGED = _error(
    'AutoBroadcastAudienceChanged',
    expected=s.IntegerField(help_text='сколько показал предпросмотр'),
    actual=s.IntegerField(help_text='сколько сейчас'),
)


def _err(description: str) -> OpenApiResponse:
    return OpenApiResponse(response=ERROR, description=description)


# ── Компоненты ────────────────────────────────────────────────────────────────

def _segment(**kw):
    return inline_serializer('AutoBroadcastSegment', fields={
        'id':    s.IntegerField(),
        'code':  s.CharField(),
        'name':  s.CharField(),
        'emoji': s.CharField(allow_blank=True),
    }, **kw)


def _audience(**kw):
    return inline_serializer('AutoBroadcastAudience', fields={
        'branch_ids':    s.ListField(child=s.IntegerField(),
                                     help_text='ВНУТРЕННИЕ Branch.id; [] = все точки сети'),
        'gender_filter': s.ChoiceField(choices=GenderFilter.choices),
        'rf_segments':   _segment(many=True),
    }, **kw)


def _reward(**kw):
    return inline_serializer('AutoBroadcastReward', fields={
        'gift_tier':          s.ChoiceField(choices=[(c, c or 'без подарка') for c in GIFT_TIER_CODES]),
        'gift_lifetime_days': s.IntegerField(min_value=0, help_text='0 — срок позиции каталога'),
        'gift_fallback_text': s.CharField(allow_blank=True, help_text='текст, если подарок выдать нельзя'),
    }, **kw)


def _follow_up(**kw):
    return inline_serializer('AutoBroadcastFollowUp', fields={
        'parent_rule_id':   s.IntegerField(),
        'parent_rule_name': s.CharField(allow_blank=True),
        'condition':        s.ChoiceField(choices=FollowUpCondition.choices),
    }, **kw)


def _variant(**kw):
    return inline_serializer('AutoBroadcastVariant', fields={
        'id':           s.IntegerField(),
        'name':         s.CharField(allow_blank=True),
        'message_text': s.CharField(allow_blank=True),
        'weight':       s.IntegerField(min_value=1),
        'is_active':    s.BooleanField(),
        'sent':         s.IntegerField(),
        'read':         s.IntegerField(),
        'failed':       s.IntegerField(),
        'open_rate':    s.FloatField(),
    }, **kw)


def _stats(**kw):
    return inline_serializer('AutoBroadcastStats', fields={
        'sent':        s.IntegerField(),
        'read':        s.IntegerField(),
        'failed':      s.IntegerField(),
        'open_rate':   s.FloatField(),
        'sent_30d':    s.IntegerField(),
        'last_run_at': s.DateTimeField(allow_null=True),
        'variants':    _variant(many=True, required=False,
                                help_text='есть в ответе …/stats/; в карточке — ключ variants'),
    }, **kw)


def _rule(**kw):
    return inline_serializer('AutoBroadcastRule', fields={
        'id':                 s.IntegerField(),
        'name':               s.CharField(),
        'event':              s.ChoiceField(choices=AutoBroadcastType.choices),
        'event_label':        s.CharField(),
        'is_active':          s.BooleanField(),
        'is_archived':        s.BooleanField(),
        'priority':           s.IntegerField(),
        'delay_days':         s.IntegerField(allow_null=True),
        'default_delay_days': s.IntegerField(allow_null=True, help_text='из справочника событий'),
        'send_hour_start':    s.IntegerField(),
        'send_hour_end':      s.IntegerField(),
        'active_from':        s.DateField(allow_null=True),
        'active_to':          s.DateField(allow_null=True),
        'audience':           _audience(),
        'audience_summary':   s.CharField(),
        'message_text':       s.CharField(),
        'image':              s.CharField(allow_null=True, help_text='всегда null — картинки в v1.5 не поддержаны'),
        'reward':             _reward(),
        'reward_summary':     s.CharField(),
        'follow_up':          _follow_up(allow_null=True),
        'variants':           _variant(many=True),
        'stats':              _stats(),
        'created_at':         s.DateTimeField(),
        'updated_at':         s.DateTimeField(),
        # ── старые плоские ключи мобилки (надмножество, удалять нельзя) ───────
        'branches_count':     s.IntegerField(help_text='0 = все точки'),
        'gender_filter':      s.ChoiceField(choices=GenderFilter.choices),
        'segments_count':     s.IntegerField(),
        'sent_total':         s.IntegerField(help_text='записей в общем дедуп-логе'),
        'sent':               s.IntegerField(),
        'read':               s.IntegerField(),
        'failed':             s.IntegerField(),
        'open_rate':          s.FloatField(),
        'parent_rule_name':   s.CharField(allow_null=True),
    }, **kw)


def _audience_write(**kw):
    return inline_serializer('AutoBroadcastAudienceWrite', fields={
        'branch_ids':     s.ListField(child=s.IntegerField(), required=False,
                                      help_text='внутренние Branch.id; [] = все точки. '
                                                'Роль с ограничением по точкам обязана прислать непустой список'),
        'gender_filter':  s.ChoiceField(choices=GenderFilter.choices, required=False),
        'rf_segment_ids': s.ListField(child=s.IntegerField(), required=False),
    }, **kw)


def _variant_write(name: str, create: bool, **kw):
    return inline_serializer(name, fields={
        'name':         s.CharField(required=create, max_length=MAX_VARIANT_NAME_LEN),
        'message_text': s.CharField(required=create, max_length=MAX_TEXT_LEN),
        'weight':       s.IntegerField(required=False, min_value=1, help_text='по умолчанию 1'),
        'is_active':    s.BooleanField(required=False),
    }, **kw)


def _rule_write(name: str, create: bool):
    return inline_serializer(name, fields={
        'name':            s.CharField(required=create, max_length=MAX_NAME_LEN),
        'event':           s.ChoiceField(choices=AutoBroadcastType.choices, required=create),
        'message_text':    s.CharField(required=create, max_length=MAX_TEXT_LEN),
        'delay_days':      s.IntegerField(required=False, allow_null=True, min_value=0,
                                          help_text='обязателен у событий с delay_required'),
        'send_hour_start': s.IntegerField(required=False, min_value=0, max_value=23),
        'send_hour_end':   s.IntegerField(required=False, min_value=0, max_value=23),
        'active_from':     s.DateField(required=False, allow_null=True),
        'active_to':       s.DateField(required=False, allow_null=True),
        'priority':        s.IntegerField(required=False, min_value=0),
        'is_active':       s.BooleanField(required=False,
                                          help_text='для мобилки LoyalUP; из кабинета CheckUp '
                                                    'true → 400 use_activate (только activate/)'),
        'audience':        _audience_write(required=False, help_text='заменяется целиком'),
        'reward':          _reward(required=False, help_text='заменяется целиком'),
        'follow_up':       inline_serializer('AutoBroadcastFollowUpWrite', fields={
            'parent_rule_id': s.IntegerField(),
            'condition':      s.ChoiceField(choices=FollowUpCondition.choices),
        }, required=False, allow_null=True, help_text='null — убрать связку'),
        'variants':        _variant_write('AutoBroadcastVariantWrite', create=True,
                                          many=True, required=False,
                                          help_text='заменяются целиком; варианты с отправками выключаются'),
    })


RULE = _rule()
RULE_LIST = inline_serializer('AutoBroadcastRuleList', fields={
    'rules':  _rule(many=True),
    'total':  s.IntegerField(),
    'limit':  s.IntegerField(help_text='без параметра limit равен total — отдан полный список'),
    'offset': s.IntegerField(),
})
RULE_CREATE_BODY = _rule_write('AutoBroadcastRuleCreate', create=True)
RULE_PATCH_BODY = _rule_write('AutoBroadcastRulePatch', create=False)

EVENTS = inline_serializer('AutoBroadcastEvents', fields={
    'events': inline_serializer('AutoBroadcastEvent', fields={
        'code':               s.ChoiceField(choices=AutoBroadcastType.choices),
        'label':              s.CharField(),
        'description':        s.CharField(),
        'dedup':              s.ChoiceField(choices=[('year', 'раз в год на гостя'),
                                                     ('day', 'раз в сутки на гостя'),
                                                     ('entity', 'раз на объект (подарок)')]),
        'delay_unit':         s.CharField(help_text="всегда 'days'"),
        'default_delay_days': s.IntegerField(allow_null=True),
        'delay_required':     s.BooleanField(help_text='true — delay_days обязателен'),
        'placeholders':       s.ListField(child=s.CharField()),
    }, many=True),
    'gender_filters':       inline_serializer('AutoBroadcastGenderFilter', fields={
        'code': s.ChoiceField(choices=GenderFilter.choices), 'label': s.CharField()}, many=True),
    'follow_up_conditions': inline_serializer('AutoBroadcastFollowUpCondition', fields={
        'code': s.ChoiceField(choices=FollowUpCondition.choices), 'label': s.CharField()}, many=True),
    'gift_tiers':           inline_serializer('AutoBroadcastGiftTier', fields={
        'code': s.CharField(allow_blank=True), 'label': s.CharField()}, many=True),
})

PREVIEW = inline_serializer('AutoBroadcastPreview', fields={
    'recipients':   s.IntegerField(help_text='старое поле мобилки'),
    'due_now':      s.BooleanField(),
    'reason':       s.CharField(allow_blank=True,
                                help_text='inactive | not_started | finished | outside_send_window'),
    'sample_text':  s.CharField(),
    'sample_names': s.ListField(child=s.CharField()),
    'count':        s.IntegerField(help_text='= recipients; вернуть как expected_count в activate/'),
    'by_branch':    inline_serializer('AutoBroadcastPreviewBranch', fields={
        'branch_id': s.IntegerField(help_text='внутренний Branch.id'),
        'name':      s.CharField(),
        'count':     s.IntegerField(),
    }, many=True),
    'sample_texts': inline_serializer('AutoBroadcastPreviewText', fields={
        'variant_id': s.IntegerField(allow_null=True, help_text='null — основной текст правила'),
        'name':       s.CharField(),
        'text':       s.CharField(),
    }, many=True),
})

ACTIVATE_BODY = inline_serializer('AutoBroadcastActivateRequest', fields={
    'expected_count': s.IntegerField(help_text='count из предпросмотра — цифра, которую видел человек'),
    'confirm':        s.BooleanField(),
})

LOG = inline_serializer('AutoBroadcastLog', fields={
    'total':   s.IntegerField(),
    'limit':   s.IntegerField(),
    'offset':  s.IntegerField(),
    'results': inline_serializer('AutoBroadcastLogRow', fields={
        'sent_at': s.DateTimeField(allow_null=True),
        'vk_id':   s.CharField(help_text='строкой'),
        'name':    s.CharField(allow_blank=True),
        'variant': inline_serializer('AutoBroadcastLogVariant', fields={
            'id': s.IntegerField(), 'name': s.CharField(allow_blank=True)}, allow_null=True),
        'status':  s.ChoiceField(choices=RecipientStatus.choices),
        'read_at': s.DateTimeField(allow_null=True),
        'error':   s.CharField(allow_blank=True),
    }, many=True),
})

STATS = _stats()
VARIANT = _variant()
VARIANT_CREATE_BODY = _variant_write('AutoBroadcastVariantCreate', create=True)
VARIANT_PATCH_BODY = _variant_write('AutoBroadcastVariantPatch', create=False)

TEST_SEND_BODY = inline_serializer('AutoBroadcastTestSendRequest', fields={
    'vk_id': s.IntegerField(help_text='VK ID гостя сети (обычно сам сотрудник)'),
})
TEST_SEND_RESULT = inline_serializer('AutoBroadcastTestSendResult', fields={
    'ok':         s.BooleanField(),
    'message_id': s.IntegerField(allow_null=True, help_text='id сообщения в ВК'),
})

_TAG = ['auto-broadcasts']
_PAGE_PARAMS = [
    OpenApiParameter('limit', int, description=f'1..{MAX_LIMIT}, по умолчанию {DEFAULT_LIMIT}'),
    OpenApiParameter('offset', int, description='с нуля'),
]


# ── Декораторы для auto_broadcasts.py (по одному на HTTP-метод) ──────────────
events = extend_schema(
    tags=_TAG, summary='Справочник событий, полов, условий догона и тиров подарка',
    responses={200: EVENTS},
)
rule_list = extend_schema(
    tags=_TAG, summary='Правила авторассылок (ключ rules; без limit — полный список)',
    parameters=_PAGE_PARAMS + [
        OpenApiParameter('event', str, enum=EVENT_CHOICES, description='фильтр по событию'),
        OpenApiParameter('is_active', bool, description='фильтр по включённости'),
        OpenApiParameter('q', str, description='поиск по названию и тексту'),
        OpenApiParameter('include_archived', bool, description='1 — показать архивные'),
    ],
    responses={200: RULE_LIST, 400: _err('invalid_payload')},
)
rule_create = extend_schema(
    tags=_TAG, summary='Создать правило (всегда выключенным)', request=RULE_CREATE_BODY,
    responses={201: RULE,
               400: _err('invalid_payload / event_unknown / delay_required / '
                         'variant_weights_invalid / reward_invalid'),
               404: _err('not_found — точка недоступна / segment_not_found')},
)
rule_get = extend_schema(
    tags=_TAG, summary='Карточка правила',
    responses={200: RULE, 404: _err('not_found — чужое, архивное сетевое или несуществующее')},
)
rule_patch = extend_schema(
    tags=_TAG, summary='Изменить правило (audience/reward/follow_up/variants — целиком)',
    request=RULE_PATCH_BODY,
    responses={200: RULE,
               400: _err('invalid_payload / event_unknown / delay_required / '
                         'variant_weights_invalid / reward_invalid / use_activate'),
               404: _err('not_found / segment_not_found'),
               409: _err('archived')},
)
rule_delete = extend_schema(
    tags=_TAG, summary='Архивировать правило (физически не удаляется)',
    responses={204: OpenApiResponse(description='правило в архиве и выключено'),
               404: _err('not_found')},
)
preview = extend_schema(
    tags=_TAG, summary='Предпросмотр: count → expected_count для activate/',
    responses={200: PREVIEW, 404: _err('not_found'), 409: _err('preview_failed')},
)
activate = extend_schema(
    tags=_TAG, summary='Включить правило (expected_count + confirm обязательны)',
    request=ACTIVATE_BODY,
    responses={200: RULE,
               400: _err('expected_count_required / confirm_required / audience_empty / invalid_payload'),
               404: _err('not_found'),
               409: OpenApiResponse(response=AUDIENCE_CHANGED,
                                    description='audience_changed (+expected, +actual) / '
                                                'already_active / archived / preview_failed')},
)
deactivate = extend_schema(
    tags=_TAG, summary='Выключить правило', request=None,
    responses={200: RULE, 404: _err('not_found')},
)
log = extend_schema(
    tags=_TAG, summary='Лог получателей всех запусков правила',
    parameters=_PAGE_PARAMS + [
        OpenApiParameter('status', str, enum=[v for v, _ in RecipientStatus.choices],
                         description='фильтр по статусу получателя'),
    ],
    responses={200: LOG, 404: _err('not_found')},
)
stats = extend_schema(
    tags=_TAG, summary='Статистика правила и вариантов',
    responses={200: STATS, 404: _err('not_found')},
)
variant_create = extend_schema(
    tags=_TAG, summary='Добавить вариант A/B', request=VARIANT_CREATE_BODY,
    responses={201: VARIANT, 400: _err('variant_weights_invalid'),
               404: _err('not_found'), 409: _err('archived')},
)
variant_patch = extend_schema(
    tags=_TAG, summary='Изменить вариант A/B', request=VARIANT_PATCH_BODY,
    responses={200: VARIANT, 400: _err('variant_weights_invalid'), 404: _err('not_found')},
)
variant_delete = extend_schema(
    tags=_TAG, summary='Удалить вариант A/B (с отправками — нельзя)',
    responses={204: OpenApiResponse(description='вариант удалён'),
               404: _err('not_found'), 409: _err('has_sends')},
)
test_send = extend_schema(
    tags=_TAG, summary='Тест-отправка одному гостю (без записи в лог и статистику)',
    request=TEST_SEND_BODY,
    responses={200: TEST_SEND_RESULT,
               400: _err('invalid_payload / guest_not_found / not_subscribed'),
               404: _err('not_found'),
               409: _err('no_vk_token / rate_limited'),
               502: _err('vk_error')},
)
