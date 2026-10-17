"""Склейка двух карточек одного гостя — шаг Б (17.10.2026).

«Одна карточка гостя, в ней все каналы» (владелец). Шаг А собрал каналы; здесь
две карточки одного человека становятся одной:

  склеить(source, target)   — всё, что ссылается на `source`, переезжает на
                              `target` во ВСЕХ сетях: профили точек, визиты,
                              баллы, сканы, анкеты… Каждое перенесённое поле
                              пишется в журнал `GuestMerge.moves`;
  отменить(merge)           — ставит каждое поле обратно;
  возможно_один(...)        — пары «возможно, один человек» (тот же номер).

Профиль точки (`ClientBranch`, одна строка на гостя и точку): если у цели в той
же точке профиль уже есть, переезжает не сам профиль, а всё, что к нему
привязано (визиты, баллы…), а профиль источника уходит в архив с пометкой
«склейка». Строка, которая не может переехать (уникальность у цели), остаётся
на месте и пишется в `conflicts` — молча ничего не пропадает.

Ссылки находим по моделям Django (`_meta`), а не списком руками: новая таблица
с полем на гостя попадает в склейку сама. Поля с VK ID числом (журналы рассылок)
не трогаем: у служебной карточки (гость из Телеграма) рассылок ВК не бывает.
"""
from __future__ import annotations

import logging

from django.apps import apps as django_apps
from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

ПУСТЫЕ_ПОЛЯ = ('first_name', 'last_name', 'photo_url', 'gender', 'phone', 'phone_source', 'phone_consent_at')


class Отказ(Exception):
    def __init__(self, detail, status=400):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def _ссылки(на_модель, только_сети: bool):
    """[(модель, поле)] — FK/O2O на `на_модель` в моделях сетей (или общих)."""
    from django.conf import settings
    сетевые = {a.split('.apps.')[0] for a in getattr(settings, 'TENANT_APPS', [])}
    out = []
    for m in django_apps.get_models():
        mod = m.__module__.rsplit('.models', 1)[0]
        в_сети = any(mod.startswith(x) for x in сетевые if x.startswith('apps.'))
        if в_сети != только_сети or m._meta.proxy or not m._meta.managed:
            continue
        for f in m._meta.concrete_fields:
            if f.is_relation and (f.many_to_one or f.one_to_one) and f.related_model is на_модель:
                out.append((m, f))
    return out


def _перенести(model, field, старое, новое, schema, moves, conflicts, кроме_pk=()):
    """Перенести строки model.field: старое → новое по одной (savepoint на строку)."""
    n = 0
    for pk in list(model.objects.filter(**{field.attname: старое}).exclude(pk__in=кроме_pk)
                   .values_list('pk', flat=True)):
        try:
            with transaction.atomic():
                model.objects.filter(pk=pk).update(**{field.attname: новое})
            moves.append({'schema': schema, 'model': model._meta.label, 'pk': pk, 'field': field.attname,
                          'old': старое})
            n += 1
        except IntegrityError:
            conflicts.append({'schema': schema, 'model': model._meta.label, 'pk': pk, 'field': field.attname,
                              'why': 'у цели уже есть такая строка'})
    return n


def _в_сети(source, target, schema, moves, conflicts):
    from apps.tenant.branch.models import ClientBranch
    счёт = {}
    ссылки_на_профиль = _ссылки(ClientBranch, только_сети=True)
    for cb in list(ClientBranch.objects.filter(client=source)):
        у_цели = ClientBranch.objects.filter(client=target, branch_id=cb.branch_id).first()
        if у_цели is None:
            ClientBranch.objects.filter(pk=cb.pk).update(client=target)
            moves.append({'schema': schema, 'model': ClientBranch._meta.label, 'pk': cb.pk,
                          'field': 'client_id', 'old': source.pk})
            счёт['profiles'] = счёт.get('profiles', 0) + 1
            continue
        # Профиль в этой точке у цели уже есть — переносим всё, что к профилю привязано.
        for m, f in ссылки_на_профиль:
            n = _перенести(m, f, cb.pk, у_цели.pk, schema, moves, conflicts)
            if n:
                счёт[m._meta.label] = счёт.get(m._meta.label, 0) + n
        дозаполнить = {}
        for поле in ('birth_date', 'birth_date_set_at', 'invited_by_id'):
            if getattr(у_цели, поле, None) in (None, '') and getattr(cb, поле, None) not in (None, ''):
                дозаполнить[поле] = getattr(cb, поле)
        if дозаполнить:
            moves.append({'schema': schema, 'model': ClientBranch._meta.label, 'pk': у_цели.pk,
                          'field': '__fill__', 'old': {k: None for k in дозаполнить}})
            ClientBranch.objects.filter(pk=у_цели.pk).update(**дозаполнить)
        if not cb.archived_at:
            ClientBranch.objects.filter(pk=cb.pk).update(archived_at=timezone.now(), archived_by='склейка')
            moves.append({'schema': schema, 'model': ClientBranch._meta.label, 'pk': cb.pk,
                          'field': '__archive__', 'old': None})
        счёт['profiles_joined'] = счёт.get('profiles_joined', 0) + 1
    for m, f in _ссылки(type(source), только_сети=True):
        if m is ClientBranch:
            continue
        n = _перенести(m, f, source.pk, target.pk, schema, moves, conflicts)
        if n:
            счёт[m._meta.label] = счёт.get(m._meta.label, 0) + n
    return счёт


def склеить(source, target, *, reason='manual', by=''):
    """Влить карточку `source` в `target` во всех сетях. → GuestMerge."""
    from django_tenants.utils import get_tenant_model, schema_context

    from .models import Client, GuestMerge
    if source.pk == target.pk:
        raise Отказ('Это одна и та же карточка.')
    source.refresh_from_db()
    target.refresh_from_db()
    if source.merged_into_id or target.merged_into_id:
        raise Отказ('Одна из карточек уже влита в другую — сначала отмените ту склейку.', 409)
    if not source.is_synthetic_vk and not target.is_synthetic_vk:
        # Две настоящие страницы ВК — это два аккаунта ВК; склеиваем только руками и
        # только если человек сам подтвердил. Сценарий «TG/телефон» сюда не попадает.
        if reason != 'manual':
            raise Отказ('Обе карточки — настоящие страницы ВК; автоматически не склеиваем.', 409)
    moves, conflicts, counts = [], [], {}
    for tenant in get_tenant_model().objects.exclude(schema_name='public'):
        with schema_context(tenant.schema_name), transaction.atomic():
            сеть = _в_сети(source, target, tenant.schema_name, moves, conflicts)
        if сеть:
            counts[tenant.schema_name] = сеть
    with transaction.atomic():
        for m, f in _ссылки(Client, только_сети=False):
            if m is Client or m is GuestMerge:
                continue
            n = _перенести(m, f, source.pk, target.pk, 'public', moves, conflicts)
            if n:
                counts.setdefault('public', {})[m._meta.label] = n
        дозаполнить = {p: getattr(source, p) for p in ПУСТЫЕ_ПОЛЯ
                       if hasattr(target, p) and getattr(target, p) in (None, '')
                       and getattr(source, p, None) not in (None, '')}
        if дозаполнить:
            moves.append({'schema': 'public', 'model': Client._meta.label, 'pk': target.pk, 'field': '__fill__',
                          'old': {k: getattr(target, k) for k in дозаполнить}})
            Client.objects.filter(pk=target.pk).update(**дозаполнить)
        Client.objects.filter(pk=source.pk).update(merged_into=target, is_active=False)
        g = GuestMerge.objects.create(source=source, target=target, reason=reason, by=str(by)[:128],
                                      moves=moves, conflicts=conflicts, counts=counts)
    logger.warning('guest.merge: #%s → #%s (%s), строк %s, конфликтов %s',
                   source.pk, target.pk, reason, len(moves), len(conflicts))
    return g


def отменить(merge, *, by=''):
    """Вернуть всё, что перенесла склейка. → число возвращённых полей."""
    from django_tenants.utils import schema_context

    from .models import Client
    if merge.undone_at:
        raise Отказ('Склейка уже отменена.', 409)
    по_схемам = {}
    for mv in merge.moves:
        по_схемам.setdefault(mv['schema'], []).append(mv)
    n = 0
    for schema, ходы in по_схемам.items():
        with schema_context(schema), transaction.atomic():
            for mv in reversed(ходы):
                m = django_apps.get_model(mv['model'])
                if mv['field'] == '__archive__':
                    m.objects.filter(pk=mv['pk'], archived_by='склейка').update(archived_at=None, archived_by='')
                elif mv['field'] == '__fill__':
                    m.objects.filter(pk=mv['pk']).update(**mv['old'])
                else:
                    m.objects.filter(pk=mv['pk']).update(**{mv['field']: mv['old']})
                n += 1
    with transaction.atomic():
        Client.objects.filter(pk=merge.source_id).update(merged_into=None, is_active=True)
        merge.undone_at = timezone.now()
        merge.undone_by = str(by)[:128]
        merge.save(update_fields=['undone_at', 'undone_by', 'updated_at'])
    logger.warning('guest.merge: отменена склейка #%s (%s полей)', merge.pk, n)
    return n


def из_телеграма(tg_user_id, user=None):
    """Гость пришёл из Телеграма без ВК — служебная карточка + привязка. → Client.

    Номер карточки — из служебного диапазона (`СЛУЖЕБНЫЙ_VK_ОТ + id Телеграма`):
    выше любого настоящего VK ID, в ВК не пишется (`is_synthetic_vk`).
    """
    from .channels import СЛУЖЕБНЫЙ_VK_ОТ
    from .models import Client, TelegramLink
    user = user or {}
    vk = СЛУЖЕБНЫЙ_VK_ОТ + int(tg_user_id)
    with transaction.atomic():
        client, _ = Client.objects.get_or_create(
            vk_id=vk, defaults={'is_synthetic_vk': True, 'first_name': str(user.get('first_name') or '')[:255],
                                'last_name': str(user.get('last_name') or '')[:255],
                                'photo_url': str(user.get('photo_url') or '')[:500]})
        TelegramLink.objects.get_or_create(tg_user_id=int(tg_user_id), defaults={'client': client})
    return client


def влить_телеграм(link_client, vk_client, *, by='vk-id'):
    """Гость из Телеграма потом вошёл через VK ID — служебная карточка вливается в настоящую."""
    if link_client is None or vk_client is None or link_client.pk == vk_client.pk:
        return None
    if not link_client.is_synthetic_vk or link_client.merged_into_id:
        return None
    try:
        return склеить(link_client, vk_client, reason='tg', by=by)
    except Отказ as e:
        logger.warning('guest.merge: телеграм не влит: %s', e.detail)
        return None


def возможно_один(лимит=200):
    """Пары «возможно, один человек»: тот же телефон у двух живых карточек.

    Только подсказка: склеивает человек (сетевой админ) кнопкой, с отменой.
    → [{'a': id, 'b': id, 'phone', 'why'}].
    """
    from django.db.models import Count

    from .models import Client
    живые = Client.objects.filter(merged_into__isnull=True).exclude(phone='').exclude(phone__isnull=True)
    номера = (живые.values('phone').annotate(n=Count('id')).filter(n__gt=1).order_by('-n')
              .values_list('phone', flat=True)[:лимит])
    out = []
    for phone in номера:
        ids = list(живые.filter(phone=phone).order_by('id').values_list('id', 'is_synthetic_vk', 'phone_source'))
        a = ids[0]
        for b in ids[1:]:
            доказан = {a[2], b[2]} & {'vk'}
            out.append({'a': a[0], 'b': b[0], 'phone': phone,
                        'why': 'тот же номер' + (', у одной доказан ВК' if доказан else ', не доказан')})
    return out[:лимит]
