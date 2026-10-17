"""Рассылка в живой канал гостя (склейка гостя, шаг В — 17.10.2026).

Все рассылки LoyalUP (ручные, авто, ДР, после игры, напоминания о подарке)
уходят через `senler.services.send_vk_message`. Здесь — куда на самом деле
слать, если ВК не годится:
  · служебная карточка (гость пришёл из Телеграма без ВК, vk_id ≥ 9·10¹²) —
    только Телеграм, ВК такого человека не знает;
  · настоящий ВК, но гость не разрешил сообщения сообществу — Телеграм, если
    гость открывал нашего бота (привязка `TelegramLink`) и не остановил его.
Шлёт бот гостей (`TELEGRAM_GUEST_BOT_TOKEN`); бот общий на все сети, поэтому
первой строкой — название сети. Картинка ВК (`photo-…`) в Телеграм не
переносится — уходит текст и кнопки-ссылки. Выключатель запасного пути для
настоящих ВК-гостей — `GUEST_LIVE_CHANNEL=off`; служебным карточкам Телеграм
нужен всегда.
"""
import logging

from django.conf import settings

from .channels import СЛУЖЕБНЫЙ_VK_ОТ

logger = logging.getLogger(__name__)

СТОП = 'Пропущено: гость остановил бота в Телеграме'


def служебный(vk_id) -> bool:
    try:
        return int(vk_id) >= СЛУЖЕБНЫЙ_VK_ОТ
    except (TypeError, ValueError):
        return False


def запасной_включён() -> bool:
    return (str(getattr(settings, 'GUEST_LIVE_CHANNEL', 'on')).strip().lower()
            not in ('off', '0', 'false', 'no'))


def _токен() -> str:
    return (getattr(settings, 'TELEGRAM_GUEST_BOT_TOKEN', '') or '').strip()


def телеграм_гостя(vk_id) -> int | None:
    """Телеграм-id гостя с этим vk_id, если ему можно писать, иначе None."""
    from .models import GuestChannel, TelegramLink

    try:
        vk_id = int(vk_id)
    except (TypeError, ValueError):
        return None
    ссылки = list(TelegramLink.objects.filter(client__vk_id=vk_id).order_by('-updated_at')
                  .values_list('tg_user_id', flat=True)[:3])
    if not ссылки and служебный(vk_id):
        ссылки = [vk_id - СЛУЖЕБНЫЙ_VK_ОТ]
    закрыты = set(GuestChannel.objects.filter(kind='tg', external_id__in=[str(t) for t in ссылки],
                                              can_message=False).values_list('external_id', flat=True))
    for tg in ссылки:
        if str(tg) not in закрыты:
            return int(tg)
    return None


def _кнопки(keyboard: dict | None) -> list | None:
    """Кнопки ВК → кнопки Телеграма; переносятся только ссылки."""
    if not isinstance(keyboard, dict):
        return None
    ряды = []
    for ряд in keyboard.get('buttons') or []:
        кн = []
        for b in ряд or []:
            a = (b or {}).get('action') or {}
            if a.get('type') == 'open_link' and a.get('link'):
                кн.append({'text': str(a.get('label') or 'Открыть')[:64], 'url': a['link']})
        if кн:
            ряды.append(кн)
    return ряды or None


def _сеть() -> str:
    try:
        from django.db import connection
        t = getattr(connection, 'tenant', None)
        return (getattr(t, 'name', '') or '').strip() if getattr(t, 'schema_name', 'public') != 'public' else ''
    except Exception:  # noqa: BLE001 — подпись не повод не отправить
        return ''


def отправить(vk_id, message: str, keyboard: dict | None = None) -> tuple[bool, str, None]:
    """Отправить в Телеграм гостю с этим vk_id. → (ok, ошибка, None).

    Номер сообщения не возвращаем: его ждут правка/удаление и «прочитано»
    через ВК, а это сообщение Телеграма.
    """
    tg = телеграм_гостя(vk_id)
    if not tg:
        return False, 'Пропущено: нет Телеграма, куда писать', None
    токен = _токен()
    if not токен:
        return False, 'Пропущено: бот гостей не настроен', None
    import requests

    сеть = _сеть()
    текст = f'{сеть}\n\n{message}' if сеть else message
    payload = {'chat_id': tg, 'text': текст[:4096], 'disable_web_page_preview': False}
    кн = _кнопки(keyboard)
    if кн:
        payload['reply_markup'] = {'inline_keyboard': кн}
    try:
        r = requests.post(f'https://api.telegram.org/bot{токен}/sendMessage', json=payload, timeout=10)
        data = r.json()
    except Exception as exc:  # noqa: BLE001 — ошибка получателя, не рассылки
        if exc.__class__.__name__ == 'SoftTimeLimitExceeded':
            raise
        return False, f'Телеграм: {exc}', None
    if data.get('ok'):
        _отметить(tg, True)
        return True, '', None
    код, описание = data.get('error_code'), data.get('description') or 'ошибка Телеграма'
    if код == 403:
        _отметить(tg, False)
        return False, СТОП, None
    return False, f'Телеграм: {описание}', None


def _отметить(tg, можно: bool):
    from .models import GuestChannel
    try:
        GuestChannel.objects.filter(kind='tg', external_id=str(tg)).exclude(can_message=можно).update(can_message=можно)
    except Exception:  # noqa: BLE001
        logger.exception('live_channel: не отметил канал tg %s', tg)
