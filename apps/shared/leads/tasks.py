"""
Pack F5 — Celery tasks для понедельного дайджеста активности.

Регистрируется в `main.celery.beat_schedule` как
'weekly-client-digest' каждый понедельник в 9:00 МСК.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives

from apps.shared.clients.models import Company

from .digest import collect_metrics, render_digest_email

logger = logging.getLogger(__name__)
User = get_user_model()


@shared_task(name='apps.shared.leads.tasks.weekly_client_digest', bind=True, max_retries=2)
def weekly_client_digest_task(self):
    """
    Раз в неделю (понедельник 9:00 МСК): для каждого активного тенанта
    собрать метрики, рассчитать скоринг, отправить email клиенту + копию
    супер-админу если status=red.
    """
    companies = Company.objects.exclude(schema_name='public').filter(is_active=True)

    sent_to_clients = 0
    sent_alerts = 0
    failures = 0

    for company in companies:
        try:
            metrics = collect_metrics(company)
            subject, text_body, html_body = render_digest_email(metrics)

            # ── Адрес клиента (network_admin компании) ─────────────────
            recipients = list(
                User.objects
                .filter(companies=company, role='network_admin', email__isnull=False)
                .exclude(email='')
                .values_list('email', flat=True)
            )

            if recipients:
                msg = EmailMultiAlternatives(
                    subject=subject,
                    body=text_body,
                    from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@levone.ru'),
                    to=recipients,
                )
                msg.attach_alternative(html_body, 'text/html')
                msg.send(fail_silently=False)
                sent_to_clients += 1

            # ── Алерт супер-админу при красном статусе ─────────────────
            super_admin_emails = getattr(settings, 'SUPER_ADMIN_EMAILS', [])
            if metrics.status == 'red' and super_admin_emails:
                alert = EmailMultiAlternatives(
                    subject=f'[ЛоялUP ALERT 🔴] «{company.name}» — скоринг {metrics.score}/100',
                    body=(
                        f'Клиент в красной зоне:\n\n'
                        f'{text_body}\n\n'
                        f'— автоматический алерт от Pack F5'
                    ),
                    from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@levone.ru'),
                    to=super_admin_emails,
                )
                alert.send(fail_silently=True)
                sent_alerts += 1

        except Exception as e:
            failures += 1
            logger.exception(
                'weekly_client_digest: failed for company %s: %s',
                company.schema_name, e,
            )

    return {
        'companies_total': companies.count(),
        'sent_to_clients': sent_to_clients,
        'sent_alerts': sent_alerts,
        'failures': failures,
    }
