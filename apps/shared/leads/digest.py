"""
Pack F5 — еженедельный понедельный дайджест активности тенанта.

Собирает метрики за прошлую неделю, скорит «активность ↔ неактивность»,
рендерит HTML-письмо. Шлётся каждый понедельник в 9:00 МСК через Celery beat.

Какие метрики собираем (внутри tenant schema):
  - branches_count: сколько активных точек
  - reviews_unanswered_negative: непрочитанные/неотвеченные негативные отзывы (старше 24ч)
  - reviews_total_week: сколько отзывов за неделю
  - broadcasts_sent_week: сколько рассылок отправлено
  - last_login_days_ago: сколько дней назад заходил админ (по User.last_login)

Скоринг неактивности (max 100):
  - Не отвечено >5 негативов          → +30
  - 0 рассылок за неделю              → +20
  - Login >7 дней назад               → +25
  - Просроченные негативы (>72ч)      → +25
  Сумма ≥50 → status='red' (красный alert)
  30–49     → status='yellow'
  <30       → status='green'
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import List, Optional

from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.shared.clients.models import Company

logger = logging.getLogger(__name__)
User = get_user_model()


@dataclass
class DigestMetrics:
    company_id: int
    company_name: str
    schema_name: str
    week_start: str             # ISO date
    week_end: str               # ISO date

    branches_count: int = 0
    reviews_unanswered_negative: int = 0
    reviews_overdue_negative: int = 0
    reviews_total_week: int = 0
    broadcasts_sent_week: int = 0
    last_login_days_ago: Optional[int] = None
    last_login_user_email: str = ''

    score: int = 0
    status: str = 'green'        # 'green' | 'yellow' | 'red'
    recommendations: List[str] = None  # type: ignore

    def __post_init__(self):
        if self.recommendations is None:
            self.recommendations = []


def collect_metrics(company: Company) -> DigestMetrics:
    """Собрать метрики тенанта внутри его schema."""
    from django_tenants.utils import schema_context

    now = timezone.now()
    week_start = now - timedelta(days=7)
    metrics = DigestMetrics(
        company_id=company.pk,
        company_name=company.name,
        schema_name=company.schema_name,
        week_start=week_start.date().isoformat(),
        week_end=now.date().isoformat(),
    )

    # ── Метрики из tenant-схемы ──────────────────────────────────────
    try:
        with schema_context(company.schema_name):
            from apps.tenant.branch.models import Branch, TestimonialConversation
            metrics.branches_count = Branch.objects.filter(is_active=True).count()

            # Непрочитанные/неотвеченные негативы старше 24 часов
            cutoff_24h = now - timedelta(hours=24)
            cutoff_72h = now - timedelta(hours=72)
            negatives = TestimonialConversation.objects.filter(
                is_replied=False,
                sentiment__in=['NEGATIVE', 'PARTIALLY_NEGATIVE'],
            )
            metrics.reviews_unanswered_negative = negatives.filter(
                last_message_at__lte=cutoff_24h,
            ).count()
            metrics.reviews_overdue_negative = negatives.filter(
                last_message_at__lte=cutoff_72h,
            ).count()

            # Все отзывы за неделю
            metrics.reviews_total_week = TestimonialConversation.objects.filter(
                last_message_at__gte=week_start,
            ).count()

            # Рассылки за неделю — пытаемся посчитать через senler.BroadcastRecipient,
            # но если его нет в этой версии — игнорим.
            try:
                from apps.tenant.senler.models import BroadcastRecipient
                metrics.broadcasts_sent_week = (
                    BroadcastRecipient.objects.filter(created_at__gte=week_start).count()
                )
            except Exception:
                metrics.broadcasts_sent_week = 0
    except Exception as e:
        logger.warning('digest: tenant metrics for %s failed: %s', company.schema_name, e)

    # ── Last login админа (User в shared schema) ─────────────────────
    try:
        admin = (
            User.objects
            .filter(companies=company, role='network_admin')
            .order_by('-last_login')
            .first()
        )
        if admin:
            metrics.last_login_user_email = admin.email or ''
            if admin.last_login:
                delta = now - admin.last_login
                metrics.last_login_days_ago = max(0, delta.days)
    except Exception as e:
        logger.warning('digest: last_login lookup failed for %s: %s', company.schema_name, e)

    # ── Скоринг неактивности ─────────────────────────────────────────
    score = 0
    recs = []

    if metrics.reviews_unanswered_negative > 5:
        score += 30
        recs.append(
            f'⚠ {metrics.reviews_unanswered_negative} негативных отзывов без ответа — '
            f'это критично для удержания.'
        )
    elif metrics.reviews_unanswered_negative > 0:
        recs.append(f'• {metrics.reviews_unanswered_negative} негативов ждут ответа.')

    if metrics.reviews_overdue_negative > 0:
        score += 25
        recs.append(
            f'⚠ {metrics.reviews_overdue_negative} негативов старше 72 часов — '
            f'гость уже считает что вы его игнорируете.'
        )

    if metrics.broadcasts_sent_week == 0:
        score += 20
        recs.append(
            '• Не было рассылок за неделю. Запустите хотя бы одну сегментную — '
            'AI напишет текст за вас.'
        )

    if metrics.last_login_days_ago is not None and metrics.last_login_days_ago > 7:
        score += 25
        recs.append(
            f'⚠ Админ не заходил {metrics.last_login_days_ago} дней — '
            f'данные из системы не используются.'
        )

    metrics.score = min(score, 100)
    if metrics.score >= 50:
        metrics.status = 'red'
    elif metrics.score >= 30:
        metrics.status = 'yellow'
    else:
        metrics.status = 'green'
        if not recs:
            recs.append('✓ Всё хорошо. Так держать!')

    metrics.recommendations = recs
    return metrics


def render_digest_email(metrics: DigestMetrics) -> tuple[str, str, str]:
    """
    Возвращает (subject, text_body, html_body) для отправки.
    """
    from django.template.loader import render_to_string

    status_emoji = {'green': '🟢', 'yellow': '🟡', 'red': '🔴'}[metrics.status]
    subject = f'ЛоялUP · Понедельная сводка {status_emoji} «{metrics.company_name}»'

    text_body = (
        f'Понедельный дайджест ЛоялUP\n'
        f'Компания: {metrics.company_name}\n'
        f'Период: {metrics.week_start} — {metrics.week_end}\n\n'
        f'Скоринг активности: {metrics.score}/100 ({metrics.status})\n\n'
        f'Метрики:\n'
        f'  Активных точек: {metrics.branches_count}\n'
        f'  Отзывов за неделю: {metrics.reviews_total_week}\n'
        f'  Негативов без ответа (>24ч): {metrics.reviews_unanswered_negative}\n'
        f'  Просроченных негативов (>72ч): {metrics.reviews_overdue_negative}\n'
        f'  Рассылок отправлено: {metrics.broadcasts_sent_week}\n'
        f'  Админ заходил: '
        f'{metrics.last_login_days_ago if metrics.last_login_days_ago is not None else "—"} дн. назад\n\n'
        f'Рекомендации:\n'
        + '\n'.join(f'  {r}' for r in metrics.recommendations)
    )

    try:
        html_body = render_to_string('leads/email_digest.html', {
            'm': metrics,
            'status_emoji': status_emoji,
        })
    except Exception:
        # Если шаблон не найден — просто <pre>текст</pre>
        html_body = f'<pre style="font-family:monospace">{text_body}</pre>'

    return subject, text_body, html_body
