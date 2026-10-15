from celery import shared_task


@shared_task(name='apps.shared.guest.tasks.check_guest_channels_task', ignore_result=True)
def check_guest_channels_task():
    """Доступность площадок входа гостя для распределителя /go/ (channel_health.py)."""
    from .channel_health import run_checks
    run_checks()
