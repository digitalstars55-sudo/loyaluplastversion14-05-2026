"""
Lead — заявка от потенциального клиента, заполненная AI-менеджером
в мобильном приложении до момента подтверждения супер-админом.

При подтверждении супер-админ запускает Lead.confirm(), который создаёт
Company + Domain + User + email с креденшалами. См. apps.shared.leads.tenant.
"""

from __future__ import annotations

import secrets
import string

from django.db import models
from django.utils import timezone


def _gen_token() -> str:
    """Случайный 32-символьный токен (для chat-сессии без авторизации)."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(32))


class LeadStatus(models.TextChoices):
    DRAFT     = 'draft',     'Черновик (AI заполняет)'
    SUBMITTED = 'submitted', 'Отправлен — ждёт подтверждения'
    CONFIRMED = 'confirmed', 'Подтверждён — тенант создан'
    REJECTED  = 'rejected',  'Отклонён'


class Lead(models.Model):
    """
    Заявка через мобильный онбординг. Заполняется AI-менеджером
    в чате, потом просматривается супер-админом.
    """

    # ── Сессия ────────────────────────────────────────────────────────
    # Случайный токен — генерируется при создании, нужен мобайлу для
    # последующих POST /chat/ без авторизации (онбординг идёт ДО логина).
    session_token = models.CharField(
        'Сессионный токен',
        max_length=64,
        unique=True,
        default=_gen_token,
        editable=False,
        db_index=True,
    )

    # ── Заполняемые поля ──────────────────────────────────────────────
    cafe_name = models.CharField('Название кафе', max_length=200, blank=True)
    cafe_count = models.PositiveIntegerField('Количество точек', null=True, blank=True)
    traffic_estimate = models.CharField('Примерный трафик', max_length=200, blank=True)
    package_suggested = models.CharField('Рекомендованный пакет', max_length=50, blank=True)

    full_name = models.CharField('ФИО клиента', max_length=200, blank=True)
    email = models.EmailField('Email', blank=True, db_index=True)
    vk_token = models.TextField('VK API token', blank=True)
    domain_slug = models.CharField(
        'Поддомен (предложенный)',
        max_length=63, blank=True,
        help_text='Без .levone.ru на конце. Можно поменять перед confirm.',
    )

    # ── Статус и аудит ────────────────────────────────────────────────
    status = models.CharField(
        'Статус',
        max_length=20,
        choices=LeadStatus.choices,
        default=LeadStatus.DRAFT,
        db_index=True,
    )
    conversation_history = models.JSONField(
        'История чата с AI',
        default=list, blank=True,
        help_text='Список {role, text, ts} — полный диалог для аудита.',
    )

    # ── Технические таймстемпы ────────────────────────────────────────
    created_at = models.DateTimeField('Создан', auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField('Обновлён', auto_now=True)
    submitted_at = models.DateTimeField('Отправлен на подтверждение', null=True, blank=True)
    confirmed_at = models.DateTimeField('Подтверждён', null=True, blank=True)
    rejected_at = models.DateTimeField('Отклонён', null=True, blank=True)
    rejection_reason = models.TextField('Причина отклонения', blank=True)

    # Кто из супер-админов подтвердил (для аудита)
    confirmed_by = models.ForeignKey(
        'users.User',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='confirmed_leads',
        verbose_name='Кто подтвердил',
    )

    # После подтверждения сохраняем созданного тенанта (Company)
    company = models.ForeignKey(
        'clients.Company',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='leads',
        verbose_name='Созданная компания',
    )

    # ── Mobile push для super-admin (Pack F2 не реализует, готовим поле) ─
    notified_super_admin = models.BooleanField(
        'Уведомление супер-админу отправлено',
        default=False,
    )

    # Сгенерированный пароль (хранится только до отправки email клиенту)
    # после email_sent_at очищается автоматически (см. provisioning.py).
    initial_password_hint = models.CharField(
        'Сгенерированный пароль (временно)',
        max_length=64, blank=True,
    )
    email_sent_at = models.DateTimeField(
        'Email с creds отправлен', null=True, blank=True,
    )

    class Meta:
        verbose_name = 'Заявка (Lead)'
        verbose_name_plural = 'Заявки (Leads)'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at']),
        ]

    def __str__(self):
        if self.cafe_name:
            return f'{self.cafe_name} · {self.get_status_display()}'
        return f'Lead #{self.pk} · {self.get_status_display()}'

    # ── Helpers ──────────────────────────────────────────────────────
    @property
    def is_complete(self) -> bool:
        """Все обязательные поля заполнены — можно отправлять на подтверждение."""
        return bool(
            self.cafe_name and self.cafe_count and self.traffic_estimate
            and self.full_name and self.email and self.vk_token
        )

    def mark_submitted(self):
        if self.status == LeadStatus.DRAFT and self.is_complete:
            self.status = LeadStatus.SUBMITTED
            self.submitted_at = timezone.now()
            self.save(update_fields=['status', 'submitted_at', 'updated_at'])

    def append_chat(self, role: str, text: str):
        """Добавить реплику в conversation_history. role='user'|'assistant'."""
        self.conversation_history = list(self.conversation_history or [])
        self.conversation_history.append({
            'role': role,
            'text': text,
            'ts': timezone.now().isoformat(),
        })
        self.save(update_fields=['conversation_history', 'updated_at'])


class CompanySecret(models.Model):
    """
    Чувствительные данные тенанта, отдельно от ClientConfig.
    Создаётся при провижионинге тенанта из Lead. Хранится в SHARED-схеме,
    чтобы веб-панель тенанта не имела к нему прямого доступа без специального
    запроса.

    Сюда кладём VK group token, полученный от клиента в онбординге. Когда
    компания захочет настраивать ВК сама — этот токен можно скопировать
    в её собственный ClientConfig (отдельная процедура, не в этом pack'е).
    """
    company = models.OneToOneField(
        'clients.Company',
        on_delete=models.CASCADE,
        related_name='secret',
        verbose_name='Компания',
    )
    vk_group_token = models.TextField(
        'VK group access token',
        blank=True,
    )
    created_from_lead = models.ForeignKey(
        Lead,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='created_secrets',
        verbose_name='Из заявки',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Секреты компании'
        verbose_name_plural = 'Секреты компаний'

    def __str__(self):
        return f'Секреты — {self.company.name}'
