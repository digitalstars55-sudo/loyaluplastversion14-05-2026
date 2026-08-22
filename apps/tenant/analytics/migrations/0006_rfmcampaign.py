# Фаза 5 (RFM-награды): RFM-кампания (snapshot аудитории ячейки + массовое
# начисление подарков/баллов) и её получатели. Аддитивно: только новые таблицы.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('analytics', '0005_dailyorderstat'),
        ('inventory', '0008_inventoryitem_rf_rewards'),
        ('branch', '0030_alter_cointransaction_source'),
        ('guest', '0003_alter_client_gender'),
    ]

    operations = [
        migrations.CreateModel(
            name='RFMCampaign',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('name', models.CharField(help_text='Авто: «RFM / Сегмент / Награда / дата». Можно править для истории.', max_length=255, verbose_name='Название')),
                ('comment', models.TextField(blank=True, help_text='Внутреннее поле, гостям не показывается.', verbose_name='Комментарий')),
                ('created_by', models.CharField(blank=True, help_text='Снимок имени пользователя на момент запуска.', max_length=150, verbose_name='Кто запустил')),
                ('mode', models.CharField(default='restaurant', help_text='restaurant — визиты в кафе, delivery — активации доставки.', max_length=16, verbose_name='Матрица')),
                ('segment_label', models.CharField(blank=True, help_text='Снимок подписи, например «Засыпающие · R1F1».', max_length=64, verbose_name='Ячейка')),
                ('r_score', models.PositiveSmallIntegerField(blank=True, null=True, verbose_name='R')),
                ('f_score', models.PositiveSmallIntegerField(blank=True, null=True, verbose_name='F')),
                ('branch_ids', models.JSONField(blank=True, default=list, help_text='Список id точек, по которым была построена ячейка. Пусто — все.', verbose_name='Точки')),
                ('period_start', models.DateField(blank=True, null=True, verbose_name='Период с')),
                ('period_end', models.DateField(blank=True, null=True, verbose_name='Период по')),
                ('reward_type', models.CharField(choices=[('gift', 'Подарок'), ('points', 'Баллы')], max_length=8, verbose_name='Тип награды')),
                ('points_amount', models.PositiveIntegerField(default=0, help_text='Для reward_type=points. Начисляются на «домашнюю точку» гостя.', verbose_name='Баллов')),
                ('lifetime_days', models.PositiveSmallIntegerField(default=0, help_text='Сколько дней есть на активацию подарка. 0 — срок позиции каталога.', verbose_name='Срок забора, дней')),
                ('attribution_window_days', models.PositiveSmallIntegerField(default=30, help_text='В этом окне визит после назначения считается возвратом кампании.', verbose_name='Окно атрибуции, дней')),
                ('holdout_percent', models.PositiveSmallIntegerField(default=10, help_text='Доля аудитории, которой награда НЕ начисляется (для честной оценки эффекта). 0 — без контрольной группы.', verbose_name='Контрольная группа, %')),
                ('status', models.CharField(choices=[('processing', 'Идёт начисление'), ('completed', 'Завершена'), ('partially_failed', 'Завершена с ошибками'), ('cancelled', 'Отменена')], default='processing', max_length=20, verbose_name='Статус')),
                ('audience_total', models.PositiveIntegerField(default=0, verbose_name='Аудитория (snapshot)')),
                ('assigned_count', models.PositiveIntegerField(default=0, verbose_name='Назначено')),
                ('skipped_count', models.PositiveIntegerField(default=0, verbose_name='Пропущено')),
                ('failed_count', models.PositiveIntegerField(default=0, verbose_name='Ошибок')),
                ('control_count', models.PositiveIntegerField(default=0, verbose_name='Контрольная группа')),
                ('started_at', models.DateTimeField(blank=True, null=True, verbose_name='Начало начисления')),
                ('finished_at', models.DateTimeField(blank=True, null=True, verbose_name='Конец начисления')),
                ('catalog_item', models.ForeignKey(blank=True, help_text='Для reward_type=gift. У позиции должен быть привязан подарок.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='rfm_campaigns', to='inventory.rewardcatalogitem', verbose_name='Позиция каталога наград')),
                ('segment', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='rfm_campaigns', to='analytics.rfsegment', verbose_name='Сегмент')),
            ],
            options={
                'verbose_name': 'RFM-кампания',
                'verbose_name_plural': 'RFM-кампании',
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='RFMCampaignMember',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('is_control', models.BooleanField(default=False, verbose_name='Контрольная группа')),
                ('status', models.CharField(choices=[('pending', 'Ожидает начисления'), ('assigned', 'Назначено'), ('control', 'Контрольная группа'), ('skipped', 'Пропущен'), ('failed', 'Ошибка'), ('cancelled', 'Отменено')], default='pending', max_length=12, verbose_name='Статус')),
                ('reason', models.CharField(blank=True, help_text='Машинная причина пропуска/ошибки: duplicate_active_gift, limit_exhausted, no_branch, error:…', max_length=64, verbose_name='Причина')),
                ('assigned_at', models.DateTimeField(blank=True, null=True, verbose_name='Начислено')),
                ('first_return_at', models.DateTimeField(blank=True, null=True, help_text='Первый визит/активация доставки после назначения в окне атрибуции.', verbose_name='Первый возврат')),
                ('segment_after', models.CharField(blank=True, help_text='Код ячейки (например R2F1) после первого возврата.', max_length=16, verbose_name='Сегмент после возврата')),
                ('campaign', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='members', to='analytics.rfmcampaign', verbose_name='Кампания')),
                ('client', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='rfm_campaign_memberships', to='guest.client', verbose_name='Гость')),
                ('client_branch', models.ForeignKey(blank=True, help_text='Профиль гость×точка, куда начислена награда (последний визит).', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='rfm_campaign_memberships', to='branch.clientbranch', verbose_name='Домашняя точка')),
                ('coin_tx', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='rfm_memberships', to='branch.cointransaction', verbose_name='Транзакция баллов')),
                ('inventory_item', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='rfm_memberships', to='inventory.inventoryitem', verbose_name='Выданный подарок')),
            ],
            options={
                'verbose_name': 'Получатель RFM-кампании',
                'verbose_name_plural': 'Получатели RFM-кампаний',
                'unique_together': {('campaign', 'client')},
            },
        ),
        migrations.AddIndex(
            model_name='rfmcampaign',
            index=models.Index(fields=['status', 'created_at'], name='rfmcamp_status_created_idx'),
        ),
        migrations.AddIndex(
            model_name='rfmcampaignmember',
            index=models.Index(fields=['campaign', 'status'], name='rfmmember_camp_status_idx'),
        ),
    ]
