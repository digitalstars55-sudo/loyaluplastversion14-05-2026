"""
Сигналы аудита: вход/выход через сессию Django-админки.

Мобильный вход (JWT) логируется явно в LoginAPIView — там нет сессионного
сигнала. Здесь ловим веб-вход в админку (public/tenant) и выход.
"""
from django.contrib.auth.signals import (
    user_logged_in, user_logged_out, user_login_failed,
)
from django.dispatch import receiver

from .services import record_event


@receiver(user_logged_in)
def _on_login(sender, request, user, **kwargs):
    record_event(
        action='login', request=request, actor=user,
        target='Вход в админку', meta={'via': 'session'},
    )


@receiver(user_logged_out)
def _on_logout(sender, request, user, **kwargs):
    if user is None:
        return
    record_event(
        action='logout', request=request, actor=user,
        target='Выход из админки', meta={'via': 'session'},
    )


@receiver(user_login_failed)
def _on_login_failed(sender, credentials, request=None, **kwargs):
    if request is None:
        return
    record_event(
        action='login_failed', request=request,
        actor_username=(credentials or {}).get('username', '') or '',
        target='Неудачный вход в админку', meta={'via': 'session'},
    )
