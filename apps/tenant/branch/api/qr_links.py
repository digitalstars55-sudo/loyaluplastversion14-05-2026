"""
Ссылка отслеживаемого QR («точки контакта»).

Зачем отдельный модуль. До 18.09.2026 ссылка собиралась РОВНО в одном месте —
в админке (`QRCodeAdmin.change_view`, apps/tenant/branch/admin.py:1245), — и по
ней уже напечатаны десятки тысяч наклеек, флаеров и коробок доставки. Ручки
волны 2 обязаны отдавать байт в байт ту же ссылку: разъедется формат — и
напечатанный QR либо перестанет попадать в воронку (метка `src`), либо уведёт
гостя в чужую точку.

Поэтому формат живёт здесь, а `QrLinkTableTest` фиксирует его таблицей по всем
пяти режимам. Админку этот коммит не трогает: её `change_view` продолжает
собирать ссылку сам (правка существующего файла в эту ветку не входит), но
формат один и проверен тестом с обеих сторон.

Правила — целиком из админки, ничего не придумано:
  • путь `#/review?` у режима «Отзыв со стола», иначе `#/?`;
  • `company` — всегда `Company.client_id` тенанта;
  • `branch` — ПУБЛИЧНЫЙ `Branch.branch_id`; у сетевого QR доставки его НЕТ
    вовсе: точку там определяет введённый гостем код (один QR на всю сеть);
  • `delivery=true` у обоих режимов доставки, `web=<key>` у режима «с сайта»,
    `table=<N>` у «отзыва со стола»;
  • `src=<key>` — всегда последним: по нему пишется скан (`QRScan`).
"""
from __future__ import annotations

from django.conf import settings
from django.db import connection


def current_company_id() -> str:
    """`client_id` тенанта текущего запроса (в ссылке — параметр `company`)."""
    tenant = getattr(connection, 'tenant', None)
    return str(getattr(tenant, 'client_id', '') or '')


def build_qr_link(qr, company_id: str, vk_app_id: str | int | None = None) -> str:
    """
    Ссылка для печати по объекту `QRCode`.

    `qr` нужен «утиный»: `mode`, `key`, `table_number`, `branch.branch_id`.
    """
    if vk_app_id is None:
        vk_app_id = getattr(settings, 'VK_MINI_APP_ID', '')

    mode = qr.mode
    # «Отзыв со стола» ведёт сразу на форму отзыва, остальные — на главную.
    path = '#/review?' if mode == 'review' else '#/?'
    network_delivery = mode == 'delivery_network'

    suffix = ''
    if mode in ('delivery', 'delivery_network'):
        suffix += '&delivery=true'
    elif mode == 'website':
        suffix += f'&web={qr.key}'
    elif mode == 'review' and qr.table_number:
        suffix += f'&table={qr.table_number}'
    suffix += f'&src={qr.key}'

    branch_part = '' if network_delivery else f'&branch={qr.branch.branch_id}'
    return (f'https://vk.com/app{vk_app_id}/{path}company={company_id}'
            f'{branch_part}{suffix}')


def build_branch_link(branch, company_id: str, *, delivery: bool = False,
                      vk_app_id: str | int | None = None) -> str:
    """
    Ссылка на вход в точку БЕЗ метки `src` — для раздела «Материалы».

    ⚠️ Такая ссылка не попадает в воронку точек контакта: если её напечатать,
    сканы и конверсии по этому размещению считаться не будут. Для печати
    заводят QR-точку контакта и берут её `url`.
    """
    if vk_app_id is None:
        vk_app_id = getattr(settings, 'VK_MINI_APP_ID', '')
    suffix = '&delivery=true' if delivery else ''
    return (f'https://vk.com/app{vk_app_id}/#/?company={company_id}'
            f'&branch={branch.branch_id}{suffix}')
