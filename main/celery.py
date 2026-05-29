import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'main.settings')

app = Celery('levone')

# Read config from Django settings, namespace CELERY_
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discover tasks in all installed apps
app.autodiscover_tasks()


# ── Periodic schedule ──────────────────────────────────────────────────────────

app.conf.beat_schedule = {
    # Poll VK group messages every 2 minutes (резерв — основной канал теперь
    # VK Callback message_new/message_reply в реальном времени; поллинг лишь
    # подстраховка, чтобы не теребить VK API частыми запросами).
    'poll-vk-messages': {
        'task': 'apps.tenant.branch.tasks.poll_all_vk_messages_task',
        'schedule': 120.0,
    },
    # Classify all WAITING conversations with AI every 30 seconds
    'reclassify-waiting-reviews': {
        'task': 'apps.tenant.analytics.tasks.reclassify_waiting_reviews_task',
        'schedule': 30.0,
    },
    # Calculate RF scores for all tenants daily at 03:00
    'rf-calculate-daily': {
        'task': 'apps.tenant.analytics.tasks.calculate_rf_all_tenants_task',
        'schedule': crontab(hour=3, minute=0),
    },
    # Fetch POS guest counts daily at 02:00 (caches yesterday's data)
    'fetch-pos-data-daily': {
        'task': 'apps.tenant.analytics.tasks.fetch_pos_data_all_tenants_task',
        'schedule': crontab(hour=2, minute=0),
        'kwargs': {'day_offset': 1},
    },
    # Refresh today's POS guest counts every hour so custom-date queries work
    'fetch-pos-data-today-hourly': {
        'task': 'apps.tenant.analytics.tasks.fetch_pos_data_all_tenants_task',
        'schedule': crontab(minute=0),
        'kwargs': {'day_offset': 0},
    },
    # Send birthday VK broadcasts daily at 10:00
    'send-birthday-broadcasts': {
        'task': 'apps.tenant.senler.tasks.send_birthday_broadcasts_task',
        'schedule': crontab(hour=10, minute=0),
    },
    # Send after-game (3h) broadcasts every 15 min (09:00–21:00 window enforced inside task)
    'send-after-game-broadcast': {
        'task': 'apps.tenant.senler.tasks.send_after_game_broadcast_task',
        'schedule': 900.0,
    },
    # Morning dispatch: send after-game messages for yesterday-evening games at 09:00
    'send-after-game-morning': {
        'task': 'apps.tenant.senler.tasks.send_after_game_broadcast_task',
        'schedule': crontab(hour=9, minute=0),
        'kwargs': {'process_evening': True},
    },
    # Generate daily codes (game, quest, birthday) for all branches at 03:00 MSK
    'generate-daily-codes': {
        'task': 'apps.tenant.branch.tasks.generate_daily_codes_task',
        'schedule': crontab(hour=3, minute=0),
    },
    # Push daily-codes summary to admins at 08:00 MSK (codes ready since 03:00)
    'push-daily-codes': {
        'task': 'apps.tenant.branch.tasks.push_daily_codes_task',
        'schedule': crontab(hour=8, minute=0),
    },
    # Purge VK photo attachments older than 90 days (rolling retention) at 04:00 MSK
    'purge-old-vk-attachments': {
        'task': 'apps.tenant.branch.tasks.purge_old_vk_attachments_task',
        'schedule': crontab(hour=4, minute=0),
    },
    # VK membership catchup: catch group_join/leave/message_allow/deny missed while server was down
    'vk-membership-catchup': {
        'task': 'apps.tenant.branch.tasks.vk_membership_catchup_task',
        'schedule': 300.0,  # every 5 minutes
    },
    # Check VK message read status every hour (for open rate analytics)
    'check-vk-read-status': {
        'task': 'apps.tenant.senler.tasks.check_read_status_task',
        'schedule': crontab(minute=30),  # every hour at :30
    },
    # Send reminders every 30 min for unanswered AI drafts (respects per-tenant reminder_minutes)
    'send-draft-reminders': {
        'task': 'apps.tenant.analytics.tasks.send_draft_reminders_task',
        'schedule': 1800.0,  # 30 minutes
    },
}

app.conf.timezone = 'Europe/Moscow'
