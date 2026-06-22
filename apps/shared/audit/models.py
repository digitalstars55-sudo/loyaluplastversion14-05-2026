from django.db import models


class AuditEvent(models.Model):
    """
    Одна запись активности участника системы (как строка в гугл-таблице).

    Денормализовано намеренно: actor_username/role/tenant_name копируются в
    момент события, чтобы журнал оставался читаемым, даже если пользователя
    или клиента позже переименуют/удалят. actor (FK) — для удобной фильтрации,
    может быть NULL для системных/сессионных событий без пользователя в БД.
    """

    class Action(models.TextChoices):
        LOGIN        = 'login',        'Вход'
        LOGIN_FAILED = 'login_failed', 'Неудачный вход'
        LOGOUT       = 'logout',       'Выход'
        VIEW         = 'view',         'Просмотр'
        CREATE       = 'create',       'Создание'
        UPDATE       = 'update',       'Изменение'
        DELETE       = 'delete',       'Удаление'

    created_at = models.DateTimeField('Время', auto_now_add=True, db_index=True)

    actor = models.ForeignKey(
        'users.User',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='audit_events',
        verbose_name='Пользователь',
    )
    actor_username = models.CharField('Ник', max_length=150, blank=True, db_index=True)
    actor_role     = models.CharField('Роль', max_length=20, blank=True)

    tenant_schema = models.CharField('Схема клиента', max_length=63, blank=True, db_index=True)
    tenant_name   = models.CharField('Клиент', max_length=255, blank=True)

    action = models.CharField('Действие', max_length=16, choices=Action.choices, db_index=True)
    target = models.CharField('Раздел / объект', max_length=255, blank=True)

    method = models.CharField('Метод', max_length=8, blank=True)
    path   = models.CharField('Путь', max_length=512, blank=True)
    status_code = models.PositiveIntegerField('Код', null=True, blank=True)

    ip         = models.GenericIPAddressField('IP', null=True, blank=True)
    user_agent = models.CharField('User-Agent', max_length=300, blank=True)

    meta = models.JSONField('Доп. данные', default=dict, blank=True)

    def __str__(self):
        who = self.actor_username or '—'
        return f'[{self.created_at:%Y-%m-%d %H:%M}] {who} · {self.get_action_display()} · {self.target or self.path}'

    class Meta:
        verbose_name = 'Запись журнала'
        verbose_name_plural = 'Журнал действий'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at'],                  name='auditevt_created_idx'),
            models.Index(fields=['actor', '-created_at'],         name='auditevt_actor_created_idx'),
            models.Index(fields=['tenant_schema', '-created_at'], name='auditevt_tenant_created_idx'),
            models.Index(fields=['action', '-created_at'],        name='auditevt_action_created_idx'),
        ]
