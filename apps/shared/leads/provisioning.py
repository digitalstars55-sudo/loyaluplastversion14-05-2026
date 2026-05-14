"""
Создание тенанта из заявки клиента.

Pack F4. Самый рискованный модуль — создаёт schema в Postgres, применяет
миграции, создаёт User, рассылает email. Любая ошибка → откат:
SQL-объекты внутри `transaction.atomic()` откатятся автоматически;
Postgres-схема (создаётся django-tenants отдельным DDL) сносится явно
через `company.auto_drop_schema = True; company.delete()`.

Использование:
    from apps.shared.leads.provisioning import create_tenant_from_lead
    company = create_tenant_from_lead(lead, by_user=request.user)
"""

from __future__ import annotations

import logging
import re
import secrets
import string
from datetime import timedelta
from typing import Tuple

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.db import models as dj_models
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from apps.shared.clients.models import Company, Domain

from .models import CompanySecret, Lead, LeadStatus

logger = logging.getLogger(__name__)
User = get_user_model()


class ProvisioningError(Exception):
    pass


# ════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════
def _generate_password(length: int = 12) -> str:
    """Случайный пароль из букв (без I/l/O/0 — путаются) + цифр."""
    alphabet = ''.join(c for c in string.ascii_letters + string.digits if c not in 'IlO0')
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def _next_client_id() -> int:
    """Следующий свободный client_id (max + 1, минимум 1)."""
    last = Company.objects.aggregate(m=dj_models.Max('client_id'))['m'] or 0
    return int(last) + 1


def _validate_schema_name(slug: str) -> str:
    """
    Postgres schema name требования: 1–63 символа, начинается с буквы,
    допустимо [a-z0-9_]. Также не может пересекаться с reserved-словами.

    Возвращает «чистый» slug. Бросает ProvisioningError, если совсем плохо.
    """
    if not slug:
        raise ProvisioningError('Пустой domain_slug — не могу создать схему')
    s = slug.lower()
    s = re.sub(r'[^a-z0-9_-]', '', s)
    s = s.replace('-', '_')
    if not s:
        raise ProvisioningError(f'После очистки domain_slug пустой: {slug!r}')
    if not s[0].isalpha():
        s = 'c_' + s
    s = s[:50]
    # Не пересекаемся с public, шаблоном или другой существующей схемой
    if s in {'public', 'pg_temp', 'pg_toast', 'information_schema'}:
        raise ProvisioningError(f'Запрещённое имя схемы: {s}')
    return s


def _ensure_unique_schema(base: str) -> str:
    """Добавляет _2, _3 к base пока не найдём свободный schema_name."""
    candidate = base
    suffix = 1
    while Company.objects.filter(schema_name=candidate).exists():
        suffix += 1
        candidate = f'{base}_{suffix}'
        if suffix > 99:
            raise ProvisioningError(f'Не могу подобрать уникальный schema_name для {base}')
    return candidate


def _ensure_unique_username(base: str) -> str:
    """Если такой username занят — добавляем суффикс."""
    candidate = base
    suffix = 1
    while User.objects.filter(username=candidate).exists():
        suffix += 1
        candidate = f'{base}{suffix}'
        if suffix > 999:
            raise ProvisioningError(f'Не могу подобрать уникальный username для {base}')
    return candidate


def _split_full_name(full_name: str) -> Tuple[str, str]:
    """ФИО → (first_name, last_name). last_name = 'Фамилия Отчество'."""
    parts = (full_name or '').strip().split()
    if not parts:
        return '', ''
    if len(parts) == 1:
        return parts[0], ''
    return parts[0], ' '.join(parts[1:])


def _send_credentials_email(
    lead: Lead, *, username: str, password: str, login_url: str,
) -> bool:
    """
    Отправляет письмо клиенту с логином/паролем. Возвращает True если ОК.
    Не бросает — провижионинг не должен падать из-за SMTP.
    """
    try:
        first_name, _ = _split_full_name(lead.full_name)
        ctx = {
            'lead': lead,
            'first_name': first_name or 'клиент',
            'cafe_name': lead.cafe_name,
            'username': username,
            'password': password,
            'login_url': login_url,
            'package': lead.package_suggested or 'Start',
            'trial_days': 14,
        }
        subject = f'ЛоялUP — кабинет «{lead.cafe_name}» создан'
        text_body = (
            f'Здравствуйте, {ctx["first_name"]}!\n\n'
            f'Кабинет ЛоялUP для «{lead.cafe_name}» готов.\n\n'
            f'Адрес кабинета: {login_url}\n'
            f'Логин: {username}\n'
            f'Пароль: {password}\n\n'
            f'Ваш пакет: {ctx["package"]} (триал на 14 дней).\n\n'
            'Войдите по ссылке выше и пройдите короткий тур по приложению.\n'
            'Если возникнут вопросы — менеджер на связи в чате внутри кабинета.\n\n'
            '— Команда ЛоялUP'
        )
        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@levone.ru'),
            to=[lead.email],
        )
        # HTML версию рендерим из шаблона — если шаблона нет, шлём только text.
        try:
            html_body = render_to_string('leads/email_credentials.html', ctx)
            msg.attach_alternative(html_body, 'text/html')
        except Exception:
            pass
        msg.send(fail_silently=False)
        return True
    except Exception as e:
        logger.exception('Failed to send credentials email to %s: %s', lead.email, e)
        return False


def _notify_super_admin(lead: Lead, company: Company, login_url: str) -> None:
    """Шлёт письмо супер-админу что новый тенант создан. Не валит на ошибке."""
    emails = getattr(settings, 'SUPER_ADMIN_EMAILS', [])
    if not emails:
        return
    try:
        body = (
            f'Создан новый тенант «{lead.cafe_name}»\n\n'
            f'Email клиента: {lead.email}\n'
            f'ФИО: {lead.full_name}\n'
            f'Точек: {lead.cafe_count}\n'
            f'Пакет: {lead.package_suggested}\n'
            f'Domain: {login_url}\n'
            f'Schema: {company.schema_name}\n'
            f'Lead #{lead.pk} → Company #{company.pk}'
        )
        EmailMultiAlternatives(
            subject=f'[ЛоялUP] Новый тенант: {lead.cafe_name}',
            body=body,
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@levone.ru'),
            to=emails,
        ).send(fail_silently=True)
    except Exception:
        logger.exception('Failed to notify super-admin')


# ════════════════════════════════════════════════════════════════════
# Main entry point
# ════════════════════════════════════════════════════════════════════
@transaction.atomic
def create_tenant_from_lead(lead: Lead, by_user=None) -> Company:
    """
    Создать новый тенант из подтверждённой заявки.

    Шаги:
      1. Сгенерировать schema_name из lead.domain_slug, проверить уникальность.
      2. Создать Company (триггерит auto-create-schema + миграции).
      3. Создать Domain.
      4. Создать User (network_admin), привязать к Company.
      5. Создать CompanySecret с vk_group_token.
      6. Сохранить creds в lead, обновить статус.
      7. Отправить email с creds клиенту, уведомление супер-админу.

    На любой ошибке между 2 и 6 — Company.delete() с auto_drop_schema=True
    (django-tenants дропает Postgres schema). Email/уведомление отправляются
    только после успешного 6 — на их ошибку откат не делаем (тенант создан).
    """
    if lead.status not in (LeadStatus.SUBMITTED, LeadStatus.DRAFT):
        raise ProvisioningError(
            f'Нельзя создать тенант из заявки в статусе {lead.get_status_display()}',
        )
    if not lead.is_complete:
        raise ProvisioningError(
            'Заявка неполная — не хватает обязательных полей',
        )

    # 1. Schema name
    base_schema = _validate_schema_name(lead.domain_slug or '')
    schema_name = _ensure_unique_schema(base_schema)

    domain_root = getattr(settings, 'TENANT_DOMAIN_ROOT', 'levone.ru')
    full_domain = f'{schema_name}.{domain_root}'
    login_url = f'https://{full_domain}/admin/'

    company = None
    try:
        # 2. Company (auto-creates schema + migrates tenant apps)
        client_id = _next_client_id()
        company = Company.objects.create(
            client_id=client_id,
            name=lead.cafe_name,
            schema_name=schema_name,
            paid_until=timezone.localdate() + timedelta(days=14),  # 14-day trial
            is_active=True,
        )

        # 3. Domain
        Domain.objects.create(
            domain=full_domain,
            tenant=company,
            is_primary=True,
        )

        # 4. User (in shared schema)
        first_name, last_name = _split_full_name(lead.full_name)
        username_base = re.sub(r'[^a-z0-9._-]', '', (lead.email or '').lower().split('@')[0]) or schema_name
        username = _ensure_unique_username(username_base)
        password = _generate_password()
        user = User.objects.create(
            username=username,
            email=lead.email,
            first_name=first_name,
            last_name=last_name,
            role=User.Role.NETWORK_ADMIN if hasattr(User.Role, 'NETWORK_ADMIN') else 'network_admin',
        )
        user.set_password(password)
        user.save()
        user.companies.add(company)

        # 5. CompanySecret (VK group token)
        CompanySecret.objects.create(
            company=company,
            vk_group_token=lead.vk_token or '',
            created_from_lead=lead,
        )

        # 6. Update Lead state
        lead.status = LeadStatus.CONFIRMED
        lead.confirmed_at = timezone.now()
        lead.confirmed_by = by_user
        lead.company = company
        lead.initial_password_hint = password  # будет очищен после email_sent_at
        lead.save(update_fields=[
            'status', 'confirmed_at', 'confirmed_by', 'company',
            'initial_password_hint', 'updated_at',
        ])

    except Exception as e:
        logger.exception('Tenant provisioning failed for lead #%s: %s', lead.pk, e)
        # Откат: удаляем Company с auto_drop_schema чтобы убрать Postgres schema.
        if company is not None and company.pk:
            try:
                company.auto_drop_schema = True
                company.delete()
            except Exception:
                logger.exception('Tenant rollback also failed — manual cleanup required')
        raise ProvisioningError(f'Не удалось создать тенант: {e}') from e

    # ── Email после транзакции (вне atomic, чтобы не блочить тенант) ──
    sent = _send_credentials_email(
        lead, username=user.username, password=password, login_url=login_url,
    )
    if sent:
        lead.email_sent_at = timezone.now()
        # Очищаем пароль из БД после успешной отправки email
        lead.initial_password_hint = ''
        lead.save(update_fields=['email_sent_at', 'initial_password_hint', 'updated_at'])

    _notify_super_admin(lead, company, login_url)
    return company
