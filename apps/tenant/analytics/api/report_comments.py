"""
Комментарии к отчёту по лояльности и печатная версия — контракт 3б.4 (№28).

Что было. Цифры отчёта считает `LoyaltyReportAPIView` (веб-страница
`LoyaltyReportView` считает их теми же сервисами), а комментарии менеджера
лежали в `localStorage` браузера: на другом устройстве их нет, в JSON отчёта
они не попадают, в PDF уезжает то, что набрал именно этот браузер. Печать шла
через `?format=pdf` с `?token=<JWT>` в адресе — ровно то, что контракт
запрещает (токен в адресной строке попадает в историю и в логи).

Что здесь:
  • `GET  /api/v1/analytics/report/sections/` — список 11 секций;
  • `GET  /api/v1/analytics/report/comments/` — комментарии периода и набора
    точек; `PUT` — сохранить (пустой текст = удалить, `updated_at` в элементе
    = защита от переписывания чужой правки → `409 conflict`);
  • `POST /api/v1/analytics/report/comments/generate/` — тот же вызов Claude,
    что у живой `generate-comment/`, но с сохранением в базу. Живую ручку не
    трогаем: на ней веб-страница;
  • `GET  /api/v1/analytics/report/print/` — самодостаточный HTML под JWT в
    ЗАГОЛОВКЕ: без скриптов (кроме кнопки «Печать»), комментарии из базы,
    цифры — из `LoyaltyReportAPIView`. BFF CheckUp забирает его сервер-сервер
    и отдаёт со своего домена; PDF делает браузер, как и раньше.

Ключ комментария — период И набор точек: один раздел за сентябрь по всей сети
и по одной точке это разные комментарии (`period_key`, `branch_key`).

Цифры здесь НЕ считаются: печатная версия и `comments` в JSON-отчёте зовут
`LoyaltyReportAPIView`, чтобы значения совпадали с мобилкой до копейки.
"""
from __future__ import annotations

import json
import logging

from django.db import transaction
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers as drf_serializers
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.users.access import current_schema_name, effective_branch_ids
from apps.tenant.analytics.api.serializers import StatsQuerySerializer
from apps.tenant.analytics.models import LoyaltyReportComment

log = logging.getLogger(__name__)

MAX_TEXT_LEN = 4000
AI_MODEL = 'claude-haiku-4-5-20251001'
AI_MAX_TOKENS = 512

# Те же 11 секций и в том же порядке, что на веб-странице отчёта
# (analytics/views.py, `section_choices`). Совпадение названий держит тест
# `SectionsMatchWebPageTest`: разъедутся — кабинет CheckUp начнёт подписывать
# комментарии не теми разделами.
SECTIONS = (
    (1,  'Ключевые метрики',                     ['stats']),
    (2,  'Рост клиентской базы',                 ['stats']),
    (3,  'Индекс оцифровки',                     ['stats']),
    (4,  'Источники подключения (cafe / доставка)', ['sources', 'stats']),
    (5,  'Вовлечение гостей',                    ['stats']),
    (6,  'Коммуникации с гостями',               ['stats']),
    (7,  'Отзывы гостей',                        ['reviews']),
    (8,  'RF-матрица',                           ['rf_summary', 'segment_counts']),
    (9,  'Миграция сегментов',                   ['migration']),
    (10, 'Итоговые выводы',                      ['stats', 'reviews', 'rf_summary']),
    (11, 'Рекомендации',                         ['stats', 'reviews', 'migration']),
)
# Что печатаем в каждой секции: путь в ответе JSON-ручки → подпись. Это
# ПРЕДСТАВЛЕНИЕ, а не расчёт: значения берутся из `LoyaltyReportAPIView` как
# есть. Секции 10 и 11 — только текст выводов, цифр в них нет.
SECTION_METRICS = {
    1: [('stats.total_scans', 'Сканирований QR'),
        ('stats.game_reached', 'Дошли до игры'),
        ('stats.unique_digitized_guests', 'Оцифрованных гостей'),
        ('stats.new_community_subscribers', 'Новых подписчиков сообщества'),
        ('stats.new_newsletter_subscribers', 'Новых подписчиков рассылки')],
    2: [('stats.new_group_with_gift', 'Новички с подарком'),
        ('stats.first_gift_receivers', 'Получили первый подарок'),
        ('stats.gift_activators', 'Активировали подарок'),
        ('stats.gift_cost_rub', 'Себестоимость подарков, ₽')],
    3: [('stats.unique_digitized_guests', 'Оцифрованных гостей'),
        ('stats.pos_guests', 'Гостей по кассе'),
        ('stats.scan_index', 'Индекс сканирования, %')],
    4: [('sources.from_cafe', 'Из кафе'),
        ('sources.from_delivery', 'Из доставки'),
        ('stats.cafe_scans', 'Сканирований в кафе'),
        ('stats.delivery_scans', 'Сканирований в доставке')],
    5: [('stats.repeat_game_players', 'Играли повторно'),
        ('stats.coin_purchasers', 'Покупали за баллы'),
        ('stats.vk_stories_publishers', 'Публиковали сториз'),
        ('stats.stories_referrals', 'Пришли по сториз')],
    6: [('stats.message_total_sent', 'Отправлено сообщений'),
        ('stats.message_total_read', 'Прочитано'),
        ('stats.message_open_rate', 'Открываемость, %'),
        ('stats.birthday_greetings_sent', 'Поздравлений с ДР')],
    7: [('reviews.positive', 'Позитивные'),
        ('reviews.negative', 'Негативные'),
        ('reviews.partially_negative', 'Частично негативные'),
        ('reviews.neutral', 'Нейтральные')],
    8: [('rf_summary.total', 'Гостей в матрице'),
        ('rf_summary.loyal', 'Лояльные'),
        ('rf_summary.at_risk', 'В зоне риска'),
        ('rf_summary.lost', 'Потерянные')],
    9: [('migration.improved', 'Улучшили сегмент'),
        ('migration.worsened', 'Ухудшили сегмент'),
        ('migration.stable', 'Без изменений'),
        ('migration.lost_users', 'Ушли в потерянные')],
    10: [],
    11: [],
}


def _dig(payload: dict, path: str):
    """`stats.total_scans` → значение или None, без падений на нехватке ключа."""
    node = payload
    for part in path.split('.'):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _fmt(value):
    if value is None:
        return '—'
    if isinstance(value, float):
        return f'{value:.2f}'.replace('.', ',')
    if isinstance(value, bool):
        return 'да' if value else 'нет'
    if isinstance(value, (list, dict)):
        return str(len(value))
    return str(value)


def metric_rows(payload: dict, section_num: int) -> list[dict]:
    """Строки «подпись — значение» для печатной версии секции."""
    rows = []
    for path, label in SECTION_METRICS.get(section_num, []):
        value = _dig(payload, path)
        if value is None:
            continue  # метрики может не быть (например, медленные POS-показатели)
        rows.append({'label': label, 'value': _fmt(value)})
    return rows


SECTION_NUMS = {num for num, _, _ in SECTIONS}
SECTION_TITLES = {num: title for num, title, _ in SECTIONS}


# ── общие помощники (форма как в senler/api/broadcasts.py) ────────────────────

def _atomic():
    """transaction.atomic() отдельной функцией — тесты на моках подменяют её пустым контекстом."""
    return transaction.atomic()


def _error(code: str, detail: str, status_code: int, **extra):
    payload = {'code': code, 'detail': detail}
    payload.update(extra)
    return Response(payload, status=status_code)


def _iso(value):
    return value.isoformat() if value else None


def _scope(request):
    """Период и точки запроса. Ошибка → (None, Response)."""
    ser = StatsQuerySerializer(data=request.query_params)
    if not ser.is_valid():
        return None, _error('invalid_payload', f'параметры периода: {ser.errors}',
                            http_status.HTTP_400_BAD_REQUEST)
    branch_ids = effective_branch_ids(request.user, current_schema_name(),
                                      ser.validated_data.get('branch_ids'))
    return {'start': ser.validated_data['start'], 'end': ser.validated_data['end'],
            'branch_ids': branch_ids or []}, None


def period_key(start, end, branch_ids) -> str:
    """`<start>_<end>_<точки по возрастанию|all>` — одна строка на ячейку отчёта."""
    return f'{start}_{end}_{LoyaltyReportComment.make_branch_key(branch_ids)}'


def _comment_dict(row) -> dict:
    return {
        'section_num': row.section_num,
        'section_title': SECTION_TITLES.get(row.section_num, ''),
        'text': row.text,
        'is_ai': row.is_ai,
        'author': row.author,
        'updated_at': _iso(row.updated_at),
    }


def comments_for(start, end, branch_ids) -> list[dict]:
    """
    Комментарии одной ячейки отчёта (период + набор точек).

    Зовётся и из JSON-отчёта (`LoyaltyReportAPIView`), и из печатной версии:
    один источник, чтобы в мобилке, в кабинете и в печати был один текст.
    """
    rows = (LoyaltyReportComment.objects
            .filter(period_start=start, period_end=end,
                    branch_key=LoyaltyReportComment.make_branch_key(branch_ids))
            .order_by('section_num'))
    return [_comment_dict(r) for r in rows]


def _parse_items(data):
    """Тело PUT → список правок. Ошибка формы — ValueError с текстом для detail."""
    items = data.get('comments')
    if not isinstance(items, list) or not items:
        raise ValueError('comments: список правок, хотя бы одна')
    if len(items) > len(SECTIONS):
        raise ValueError(f'comments: не больше {len(SECTIONS)} секций за раз')
    out = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('comments: элементы — объекты {section_num, text}')
        try:
            num = int(item.get('section_num'))
        except (TypeError, ValueError):
            raise ValueError('section_num: номер секции 1..11')
        if num not in SECTION_NUMS:
            raise ValueError(f'section_num {num}: секции только {sorted(SECTION_NUMS)}')
        text = str(item.get('text') or '').strip()
        if len(text) > MAX_TEXT_LEN:
            raise ValueError(f'text секции {num}: не длиннее {MAX_TEXT_LEN} символов')
        out.append({'section_num': num, 'text': text,
                    'expected_updated_at': item.get('updated_at') or None,
                    'is_ai': bool(item.get('is_ai', False))})
    nums = [i['section_num'] for i in out]
    if len(set(nums)) != len(nums):
        raise ValueError('comments: одна секция дважды в одном запросе')
    return out


def _stale(row, expected) -> bool:
    """Правку переписывают поверх чужой? Сравниваем по `updated_at` секунды в ISO."""
    if not expected or row is None:
        return False
    current = _iso(row.updated_at) or ''
    return str(expected)[:19] != current[:19]


# ── генерация комментария ИИ ─────────────────────────────────────────────────

class AIUnavailable(Exception):
    """Ключ не настроен, сеть или Claude недоступны."""

    def __init__(self, detail: str, status_code: int):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def generate_comment_text(section_num, section_title: str, metrics_json: str,
                          draft: str = '', company_name: str = 'кафе') -> str:
    """
    Тот же промпт и та же модель, что у живой `generate-comment/`.

    Вынесено функцией, чтобы (а) её можно было подменить в тестах, (б) новая
    ручка не копировала промпт: разъехавшиеся промпты дали бы разный стиль
    комментариев в вебе и в кабинете.
    """
    import os

    from django.conf import settings as dj_settings

    api_key = getattr(dj_settings, 'ANTHROPIC_API_KEY', None)
    if not api_key:
        raise AIUnavailable('ANTHROPIC_API_KEY не настроен',
                            http_status.HTTP_503_SERVICE_UNAVAILABLE)

    system_prompt = (
        'Ты — менеджер системы лояльности ресторана/кафе. Пишешь короткий '
        'аналитический комментарий к разделу отчёта.\n'
        'Правила:\n'
        '- По умолчанию пиши коротко (2-3 предложения), профессионально, на русском.\n'
        '- Опирайся на переданные ниже числа этого раздела. Не выдумывай '
        'данные и цифры, которых нет.\n'
        '- Сделай конкретный вывод именно по этим числам: что выросло или '
        'упало, что это значит и что стоит предпринять.\n'
        '- Если пользователь оставил пожелания к комментарию — обязательно '
        'выполни их (например, добавь нужные рекомендации). Его пожелания '
        'важнее правила про длину.\n'
        '- Не используй markdown, HTML.\n'
        '- Для секций 10 и 11 пиши 3 пункта через символ новой строки, каждый начиная с «•».\n'
        '- Верни ТОЛЬКО текст комментария, без пояснений.'
    )
    user_message = (
        f'Кафе: {company_name}\n'
        f'Раздел отчёта #{section_num}: {section_title}\n'
        f'Данные раздела: {metrics_json}\n\n'
        f'Напиши короткий аналитический комментарий менеджера для этого раздела отчёта.'
    )
    if draft:
        user_message += ('\n\nПожелания/указания пользователя к комментарию — '
                         f'обязательно учти их:\n{draft}')

    try:
        import anthropic
        proxy_url = os.getenv('AI_PROXY_URL', '')
        client = (anthropic.Anthropic(api_key=api_key, base_url=proxy_url)
                  if proxy_url else anthropic.Anthropic(api_key=api_key))
        message = client.messages.create(
            model=AI_MODEL, max_tokens=AI_MAX_TOKENS, system=system_prompt,
            messages=[{'role': 'user', 'content': user_message}],
        )
        return message.content[0].text.strip()
    except AIUnavailable:
        raise
    except Exception as exc:
        # Кредиты Anthropic на этом проекте кончались четыре раза — для
        # кабинета это должен быть понятный код, а не 500.
        raise AIUnavailable(f'Claude недоступен: {exc}', http_status.HTTP_502_BAD_GATEWAY)


def _company_name() -> str:
    from django.db import connection
    return getattr(getattr(connection, 'tenant', None), 'name', '') or 'кафе'


# ── цифры отчёта ─────────────────────────────────────────────────────────────

def report_payload(request) -> dict:
    """
    Цифры отчёта — вызовом живой JSON-ручки, без копии расчётов.

    `LoyaltyReportAPIView.get` берёт из запроса только `query_params` и
    `user`, поэтому её можно позвать напрямую: значения в печатной версии
    гарантированно те же, что в мобилке и в кабинете.
    """
    from apps.tenant.analytics.api.views import LoyaltyReportAPIView

    view = LoyaltyReportAPIView()
    view.request = request
    view.format_kwarg = None
    return view.get(request).data


# ── OpenAPI ──────────────────────────────────────────────────────────────────

_SECTIONS_OUT = inline_serializer(name='ReportSections', fields={
    'sections': drf_serializers.ListField(
        child=drf_serializers.DictField(), help_text='[{num, title, metric_keys}]'),
})
_COMMENTS_OUT = inline_serializer(name='ReportComments', fields={
    'period_key': drf_serializers.CharField(),
    'comments': drf_serializers.ListField(child=drf_serializers.DictField()),
    'meta': drf_serializers.DictField(),
})
_GENERATE_IN = inline_serializer(name='ReportCommentGenerateIn', fields={
    'section_num': drf_serializers.IntegerField(),
    'section_title': drf_serializers.CharField(required=False),
    'metrics_json': drf_serializers.CharField(required=False, help_text='JSON-строка цифр раздела'),
    'draft': drf_serializers.CharField(required=False, help_text='пожелания менеджера к тексту'),
    'save': drf_serializers.BooleanField(required=False, help_text='сохранить в базу как комментарий ИИ'),
})
_GENERATE_OUT = inline_serializer(name='ReportCommentGenerateOut', fields={
    'text': drf_serializers.CharField(),
    'saved': drf_serializers.BooleanField(),
    'period_key': drf_serializers.CharField(),
})
_ERR = OpenApiResponse(inline_serializer(name='ReportError', fields={
    'code': drf_serializers.CharField(),
    'detail': drf_serializers.CharField(),
}), description='invalid_payload (400) · conflict (409) · ai_unavailable (502/503)')


# ── ручки ────────────────────────────────────────────────────────────────────

class ReportSectionsAPIView(APIView):
    """GET /api/v1/analytics/report/sections/ — 11 секций отчёта."""
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: _SECTIONS_OUT}, tags=['v1'])
    def get(self, request):
        return Response({'sections': [{'num': num, 'title': title, 'metric_keys': keys}
                                      for num, title, keys in SECTIONS]})


class ReportCommentsAPIView(APIView):
    """
    GET /api/v1/analytics/report/comments/?period|start&end&branch_ids
    PUT /api/v1/analytics/report/comments/?…  {comments: [{section_num, text, updated_at?}]}

    PUT пишет ТОЛЬКО присланные секции: кабинет может сохранять по одной, не
    перетирая остальные. Пустой `text` удаляет комментарий. Если в элементе
    пришёл `updated_at` и он не совпал с тем, что в базе, — `409 conflict` со
    списком разошедшихся секций: значит кто-то сохранил раньше вас.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(parameters=[StatsQuerySerializer], responses={200: _COMMENTS_OUT, 400: _ERR},
                   tags=['v1'])
    def get(self, request):
        scope, err = _scope(request)
        if err is not None:
            return err
        return Response(self._payload(scope))

    @extend_schema(parameters=[StatsQuerySerializer], request=_COMMENTS_OUT,
                   responses={200: _COMMENTS_OUT, 400: _ERR, 409: _ERR}, tags=['v1'])
    def put(self, request):
        scope, err = _scope(request)
        if err is not None:
            return err
        try:
            items = _parse_items(request.data or {})
        except ValueError as exc:
            return _error('invalid_payload', str(exc), http_status.HTTP_400_BAD_REQUEST)

        branch_key = LoyaltyReportComment.make_branch_key(scope['branch_ids'])
        author = (getattr(request.user, 'username', '') or '')[:150]
        with _atomic():
            existing = {r.section_num: r for r in LoyaltyReportComment.objects
                        .select_for_update()
                        .filter(period_start=scope['start'], period_end=scope['end'],
                                branch_key=branch_key)}
            conflicts = [{'section_num': i['section_num'],
                          'updated_at': _iso(existing[i['section_num']].updated_at)}
                         for i in items
                         if _stale(existing.get(i['section_num']), i['expected_updated_at'])]
            if conflicts:
                return _error('conflict',
                              'эти разделы кто-то сохранил раньше вас — перечитайте и повторите',
                              http_status.HTTP_409_CONFLICT, conflicts=conflicts)

            for item in items:
                row = existing.get(item['section_num'])
                if not item['text']:
                    if row is not None:
                        row.delete()
                    continue
                if row is None:
                    LoyaltyReportComment.objects.create(
                        period_start=scope['start'], period_end=scope['end'],
                        branch_ids=sorted(scope['branch_ids']), branch_key=branch_key,
                        section_num=item['section_num'], text=item['text'],
                        is_ai=item['is_ai'], author=author)
                else:
                    row.text = item['text']
                    row.is_ai = item['is_ai']
                    row.author = author
                    row.save(update_fields=['text', 'is_ai', 'author', 'updated_at'])
        log.info('report comments saved: %s period=%s..%s branches=%s sections=%s by=%s',
                 current_schema_name(), scope['start'], scope['end'], branch_key,
                 [i['section_num'] for i in items], author)
        return Response(self._payload(scope))

    @staticmethod
    def _payload(scope) -> dict:
        return {
            'period_key': period_key(scope['start'], scope['end'], scope['branch_ids']),
            'comments': comments_for(scope['start'], scope['end'], scope['branch_ids']),
            'meta': {'start': str(scope['start']), 'end': str(scope['end']),
                     'branch_ids': scope['branch_ids']},
        }


class GenerateReportCommentSaveAPIView(APIView):
    """
    POST /api/v1/analytics/report/comments/generate/?period|start&end&branch_ids

    Тело: `section_num`, `section_title`, `metrics_json`, `draft`, `save`.
    Промпт и модель — те же, что у живой `generate-comment/` (общая функция).
    `save: true` сохраняет текст в базу как комментарий ИИ.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(request=_GENERATE_IN, responses={200: _GENERATE_OUT, 400: _ERR,
                                                    502: _ERR, 503: _ERR}, tags=['v1'])
    def post(self, request):
        scope, err = _scope(request)
        if err is not None:
            return err
        data = request.data or {}
        try:
            section_num = int(data.get('section_num'))
        except (TypeError, ValueError):
            return _error('invalid_payload', 'section_num: номер секции 1..11',
                          http_status.HTTP_400_BAD_REQUEST)
        if section_num not in SECTION_NUMS:
            return _error('invalid_payload', f'section_num: только {sorted(SECTION_NUMS)}',
                          http_status.HTTP_400_BAD_REQUEST)
        section_title = str(data.get('section_title') or SECTION_TITLES[section_num])
        metrics = data.get('metrics_json') or '{}'
        if not isinstance(metrics, str):
            metrics = json.dumps(metrics, ensure_ascii=False)
        draft = str(data.get('draft') or '').strip()

        try:
            text = generate_comment_text(section_num, section_title, metrics, draft,
                                         company_name=_company_name())
        except AIUnavailable as exc:
            log.warning('report comment generate: %s', exc.detail)
            return _error('ai_unavailable', exc.detail, exc.status_code)

        saved = False
        if str(data.get('save', '')).lower() in ('1', 'true', 'yes') or data.get('save') is True:
            branch_key = LoyaltyReportComment.make_branch_key(scope['branch_ids'])
            LoyaltyReportComment.objects.update_or_create(
                period_start=scope['start'], period_end=scope['end'],
                branch_key=branch_key, section_num=section_num,
                defaults={'text': text[:MAX_TEXT_LEN], 'is_ai': True,
                          'author': (getattr(request.user, 'username', '') or '')[:150],
                          'branch_ids': sorted(scope['branch_ids'])})
            saved = True
        return Response({'text': text, 'saved': saved,
                         'period_key': period_key(scope['start'], scope['end'],
                                                  scope['branch_ids'])})


class ReportPrintView(APIView):
    """
    GET /api/v1/analytics/report/print/?period|start&end&branch_ids

    Самодостаточный HTML для печати: инлайн-стили, без скриптов кроме кнопки
    «Печать», комментарии из базы, цифры — из живой JSON-ручки. Авторизация
    только заголовком (JWT): `?token=` в адресе контракт запрещает — он
    остаётся в истории браузера и в логах nginx.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(parameters=[StatsQuerySerializer],
                   responses={200: OpenApiResponse(description='text/html'), 400: _ERR},
                   tags=['v1'])
    def get(self, request):
        scope, err = _scope(request)
        if err is not None:
            return err
        data = report_payload(request)
        comments = {c['section_num']: c for c in comments_for(
            scope['start'], scope['end'], scope['branch_ids'])}
        html = render_to_string('analytics/loyalty_report_print.html', {
            'company_name': _company_name(),
            'start': scope['start'], 'end': scope['end'],
            'period_label': f"{scope['start']:%d.%m.%Y} — {scope['end']:%d.%m.%Y}",
            'branch_ids': scope['branch_ids'],
            'generated_at': timezone.localtime().strftime('%d.%m.%Y %H:%M'),
            'sections': [{'num': num, 'title': title, 'comment': comments.get(num),
                           'rows': metric_rows(data, num)}
                         for num, title, _ in SECTIONS],
            'report': data,
        })
        return HttpResponse(html, content_type='text/html; charset=utf-8')
