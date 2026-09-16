"""
Соответствие «пользователь CheckUp → пользователь LoyalUP».

Зачем отдельная таблица: сотрудники CheckUp не логинятся в LoyalUP паролем —
BFF CheckUp меняет свою личность сотрудника на короткий JWT LoyalUP
(services.perform_exchange). Чтобы у такого сотрудника были свои права по
точкам и своё имя в ответах на отзывы, ему заводится обычный User LoyalUP,
а эта таблица помнит, чей он.

Одна личность — на ПАРУ (пользователь CheckUp, сеть). Роль у User LoyalUP
одна на всех сетях (User.role), а в CheckUp права выдаются per-клиент:
если бы один User отвечал за две сети, роль «админ» в первой протекала бы
во вторую. Поэтому username = checkup-<id>-<schema>, companies = ровно одна.
"""
from django.conf import settings
from django.db import models


class CheckUpIdentity(models.Model):
    checkup_user_id = models.CharField('ID пользователя CheckUp', max_length=64, db_index=True)
    tenant_schema = models.CharField('Сеть (schema_name)', max_length=63)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='checkup_identity', verbose_name='Пользователь LoyalUP',
    )
    display_name = models.CharField('Имя из CheckUp', max_length=150, blank=True)
    # Email храним ТОЛЬКО здесь, не в User.email: вход мобилки ищет пользователя
    # по email через .get(), и второй User с тем же адресом уронил бы вход
    # «родному» сотруднику. Автосклейки по email нет по контракту.
    email = models.CharField('Email из CheckUp (только показ)', max_length=254, blank=True)
    last_role = models.CharField('Роль в последнем обмене', max_length=20, blank=True)
    last_branch_ids = models.JSONField('Публичные branch_id в последнем обмене', default=list, blank=True)
    exchanges_count = models.PositiveIntegerField('Обменов', default=0)
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    last_exchanged_at = models.DateTimeField('Последний обмен', null=True, blank=True)

    class Meta:
        verbose_name = 'Личность CheckUp'
        verbose_name_plural = 'Личности CheckUp'
        unique_together = (('checkup_user_id', 'tenant_schema'),)

    def __str__(self):
        return f'checkup:{self.checkup_user_id} → {self.tenant_schema} ({self.user_id})'
