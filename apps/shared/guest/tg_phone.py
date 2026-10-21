"""
Привязка Телеграма к гостю по номеру телефона (21.10.2026).

Владелец: «не могу привязать свой ВК — вход в VK ID по электронному ключу».
Внутри Телеграма электронный ключ VK ID не работает (браузер Телеграма не умеет
такие ключи), и единственный путь «Привязать ВКонтакте» закрыт.

Второй путь — номер. В Телеграм-мини-аппе гость жмёт «Поделиться номером»
(`Telegram.WebApp.requestContact`); Телеграм отдаёт контакт строкой, подписанной
токеном бота так же, как initData. Номер в ней — номер самого аккаунта
Телеграма (`contact.user_id` = id того, кто поделился). Если ровно у одного
гостя LoyalUP этот номер подтверждён подписью ВК (`phone_source='vk'`) — это
он: Телеграм привязывается к нему, служебная карточка гостя из Телеграма
вливается в настоящую (`влить_телеграм`, баллы и визиты переезжают, отмена есть).

Номер без подписи ВК, служебные и влитые карточки, двое с одним номером —
не привязываем: склейка по неподтверждённому номеру отдала бы баллы чужому.
"""
from __future__ import annotations

import json
import logging

from .tg_init import proven_tg_user_id, verify_init_data
from .vk_phone import normalize_phone

logger = logging.getLogger(__name__)


class Отказ(Exception):
    def __init__(self, code: str, detail: str, status: int = 400):
        super().__init__(detail)
        self.code, self.detail, self.status = code, detail, status


def номер_из_контакта(raw: str, tg_id: int) -> str:
    """Подписанный ответ `requestContact` → номер в E.164. Чужой или поддельный — Отказ."""
    fields = verify_init_data(raw or '')
    if fields is None:
        raise Отказ('contact_invalid', 'Телеграм не подтвердил номер — попробуйте ещё раз.')
    try:
        contact = json.loads(fields.get('contact') or '')
    except ValueError:
        raise Отказ('contact_invalid', 'Телеграм не подтвердил номер — попробуйте ещё раз.')
    if not isinstance(contact, dict) or str(contact.get('user_id') or '') != str(tg_id):
        raise Отказ('contact_foreign', 'Поделиться можно только своим номером.')
    номер = normalize_phone(contact.get('phone_number'))
    if not номер:
        raise Отказ('contact_invalid', 'Не разобрал номер.')
    return номер


def привязать(request, raw_contact: str):
    """Телеграм запроса → гость с тем же подтверждённым номером. → Client."""
    from .merge import влить_телеграм
    from .models import Client, TelegramLink

    tg_id = proven_tg_user_id(request)
    if not tg_id:
        raise Отказ('tg_required', 'Откройте приложение из Телеграма.', 403)
    номер = номер_из_контакта(raw_contact, tg_id)
    гости = list(Client.objects.filter(phone=номер, phone_source='vk', is_synthetic_vk=False,
                                       merged_into__isnull=True)[:2])
    if not гости:
        raise Отказ('phone_not_found',
                    'Гостя с этим номером нет. Откройте LoyalUP во ВКонтакте и поделитесь там номером — '
                    'потом привяжите снова.', 404)
    if len(гости) > 1:
        logger.warning('guest.tg_phone: номер %s…%s у двух гостей — не привязываем', номер[:5], номер[-2:])
        raise Отказ('phone_ambiguous', 'Этот номер у нескольких гостей — напишите нам, привяжем вручную.', 409)
    гость = гости[0]
    if getattr(гость, 'is_blocked', False):
        raise Отказ('client_blocked', 'Аккаунт заблокирован.', 403)

    прежняя = TelegramLink.objects.select_related('client').filter(tg_user_id=tg_id).first()
    TelegramLink.objects.update_or_create(tg_user_id=tg_id, defaults={'client': гость})
    if прежняя is not None and прежняя.client_id != гость.pk and прежняя.client.is_synthetic_vk:
        влить_телеграм(прежняя.client, гость, by='tg-phone')
    logger.info('guest.tg_phone: tg %s → гость %s по номеру', tg_id, гость.pk)
    return гость
