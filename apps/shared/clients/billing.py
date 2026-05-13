"""
Расчёт статуса оплаты подписки.

Используется в обеих админках (public/tenant) для отображения плашки
«скоро оплачивать / просрочено». Логика собрана в одном месте, чтобы
шапки, карточки и колонки списка показывали одинаковые цвета и тексты.

Состояния:
    expired — paid_until < сегодня (подписка истекла)
    urgent  — осталось ≤ 3 дней
    warning — осталось ≤ 10 дней
    ok      — больше 10 дней
    none    — paid_until не задан
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from django.utils import timezone


URGENT_DAYS  = 3
WARNING_DAYS = 10


def payment_status(paid_until: Optional[date]) -> dict:
    """
    Возвращает словарь с признаками для UI:
        state            — 'expired' | 'urgent' | 'warning' | 'ok' | 'none'
        days_left        — int (отрицательное = просрочено) или None
        needs_attention  — bool: показывать ли плашку (state in expired/urgent/warning)
        label            — короткий текст для бейджа
        long_label       — расширенный текст для плашки
        color            — основной цвет (#hex)
        bg               — фоновой цвет плашки
        border           — цвет рамки плашки
        icon             — emoji
    """
    if paid_until is None:
        return {
            'state':           'none',
            'days_left':       None,
            'needs_attention': False,
            'label':           '—',
            'long_label':      'Срок оплаты не задан',
            'color':           '#6b7280',
            'bg':              '#f3f4f6',
            'border':          '#e5e7eb',
            'icon':            '⚪',
        }

    today = timezone.localdate()
    days_left = (paid_until - today).days

    if days_left < 0:
        overdue = -days_left
        return {
            'state':           'expired',
            'days_left':       days_left,
            'needs_attention': True,
            'label':           f'Просрочено на {overdue} дн.',
            'long_label':      (
                f'Подписка истекла {paid_until.strftime("%d.%m.%Y")} '
                f'— {overdue} дн. назад. Приложение остановлено.'
            ),
            'color':           '#b91c1c',
            'bg':              '#fee2e2',
            'border':          '#fca5a5',
            'icon':            '🛑',
        }

    if days_left <= URGENT_DAYS:
        return {
            'state':           'urgent',
            'days_left':       days_left,
            'needs_attention': True,
            'label':           (
                'Истекает сегодня' if days_left == 0
                else f'Осталось {days_left} дн.'
            ),
            'long_label':      (
                f'Подписка истекает {paid_until.strftime("%d.%m.%Y")} — '
                f'осталось {days_left} дн. Срочно оплатите.'
                if days_left > 0 else
                f'Подписка истекает сегодня ({paid_until.strftime("%d.%m.%Y")}). '
                f'Срочно оплатите, иначе приложение остановится.'
            ),
            'color':           '#c2410c',
            'bg':              '#ffedd5',
            'border':          '#fdba74',
            'icon':            '🔥',
        }

    if days_left <= WARNING_DAYS:
        return {
            'state':           'warning',
            'days_left':       days_left,
            'needs_attention': True,
            'label':           f'Осталось {days_left} дн.',
            'long_label':      (
                f'Подписка истекает {paid_until.strftime("%d.%m.%Y")} — '
                f'через {days_left} дн. Скоро потребуется оплата.'
            ),
            'color':           '#a16207',
            'bg':              '#fef9c3',
            'border':          '#fde68a',
            'icon':            '⏰',
        }

    return {
        'state':           'ok',
        'days_left':       days_left,
        'needs_attention': False,
        'label':           f'Оплачено до {paid_until.strftime("%d.%m.%Y")}',
        'long_label':      f'Подписка активна до {paid_until.strftime("%d.%m.%Y")}.',
        'color':           '#15803d',
        'bg':              '#dcfce7',
        'border':          '#bbf7d0',
        'icon':            '✅',
    }
