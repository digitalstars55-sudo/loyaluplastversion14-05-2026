"""Каналы гостя пишутся сами — по сохранению гостя и привязки Телеграма.

Сигналы, а не вставки по месту: гостя заводят и правят десяток путей (запуск
мини-аппа, VK ID, импорт, телефон, забыть данные), и каждый пропущенный путь
означал бы карточку без канала. Сбой здесь вход не ломает — только журнал.
"""
import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender='guest.Client')
def _гость_сохранён(sender, instance, **kwargs):
    from .channels import отметить, убрать
    try:
        if instance.vk_id and not getattr(instance, 'is_synthetic_vk', False):
            отметить(instance, 'vk', instance.vk_id, verified=True)
        if instance.phone:
            отметить(instance, 'phone', instance.phone, verified=instance.phone_source == 'vk')
        else:
            убрать(instance, 'phone')
    except Exception:  # noqa: BLE001
        logger.exception('guest.channels: канал гостя %s не записан', instance.pk)


@receiver(post_save, sender='guest.TelegramLink')
def _телеграм_привязан(sender, instance, **kwargs):
    from .models import GuestChannel
    from .channels import отметить
    try:
        # Привязка — источник правды: Телеграм перепривязали к другой странице
        # ВК (гость сменил аккаунт) — канал переезжает вместе с ней.
        GuestChannel.objects.filter(kind='tg', external_id=str(instance.tg_user_id)).exclude(
            client_id=instance.client_id).update(client_id=instance.client_id)
        отметить(instance.client, 'tg', instance.tg_user_id, verified=True)
    except Exception:  # noqa: BLE001
        logger.exception('guest.channels: Телеграм %s не записан', instance.tg_user_id)
