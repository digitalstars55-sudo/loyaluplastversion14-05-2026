"""
Калькулятор стоимости поста (волна 4 ТЗ маркетолога, 14.10.2026).

Владелец: «калькулятор онлайн, который интерактивно ЗАРАНЕЕ скажет, сколько
тебе обойдётся этот пост». Считаем ДО вызова ИИ, по средним объёмам:
  • текст — средний вход (цифры сети + заметки) и выход (пост) × цена модели;
  • картинка — один кадр 1536×1024 medium (проверено в CheckUp 12.10.2026:
    1 568 выходных токенов ≈ 0,063 $);
  • видео с ИИ-аватаром — ₽ за секунду из настройки; сервис ещё не выбран —
    `video_rub=None` и честная подпись, а не ноль.
Цены и курс — в настройках (`MARKETER_*`), меняются без выкладки.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings

AVG_TEXT_IN_TOKENS = 6000
AVG_TEXT_OUT_TOKENS = 700
IMAGE_OUT_TOKENS = 1568
IMAGE_IN_TOKENS = 120


def _d(name, default):
    try:
        return Decimal(str(getattr(settings, name, default)))
    except Exception:
        return Decimal(str(default))


def _rub(usd: Decimal) -> Decimal:
    return (usd * _d('MARKETER_USD_RUB', 95)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def text_rub() -> Decimal:
    usd = (AVG_TEXT_IN_TOKENS * _d('MARKETER_TEXT_USD_PER_M_IN', 3)
           + AVG_TEXT_OUT_TOKENS * _d('MARKETER_TEXT_USD_PER_M_OUT', 15)) / Decimal(1_000_000)
    return _rub(usd)


def image_rub() -> Decimal:
    usd = (IMAGE_IN_TOKENS * _d('MARKETER_IMAGE_USD_PER_M_IN', 5)
           + IMAGE_OUT_TOKENS * _d('MARKETER_IMAGE_USD_PER_M_OUT', 40)) / Decimal(1_000_000)
    return _rub(usd)


def video_rub_per_sec():
    raw = getattr(settings, 'MARKETER_VIDEO_RUB_PER_SEC', '')
    if raw in (None, ''):
        return None
    try:
        return Decimal(str(raw))
    except Exception:
        return None


def estimate(*, count: int = 1, image: bool = False, video_seconds: int = 0) -> dict:
    t = text_rub()
    i = image_rub() if image else Decimal('0')
    per_sec = video_rub_per_sec()
    v = None
    if video_seconds:
        v = (per_sec * video_seconds).quantize(Decimal('0.01')) if per_sec is not None else None
    per_post = t + i + (v or Decimal('0'))
    notes = []
    if video_seconds and v is None:
        notes.append('Видео с ИИ-аватаром: сервис ещё не выбран — цену покажем после подключения.')
    return {
        'count': count,
        'text_rub': float(t),
        'image_rub': float(i),
        'video_rub': float(v) if v is not None else None,
        'per_post_rub': float(per_post),
        'total_rub': float((per_post * count).quantize(Decimal('0.01'))),
        'usd_rub': float(_d('MARKETER_USD_RUB', 95)),
        'notes': notes,
    }
