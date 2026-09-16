# Черновики рассылок для внешнего кабинета CheckUp (контракт платформы №11/№13).
# Аддитивно: одна новая таблица, существующие модели рассылок не меняются.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('senler', '0011_alter_autobroadcastlog_vk_id_and_more'),
        ('analytics', '0006_rfmcampaign'),
    ]

    operations = [
        migrations.CreateModel(
            name='BroadcastDraft',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('name', models.CharField(blank=True, help_text='Только для кабинета — гости его не видят.', max_length=200, verbose_name='Название')),
                ('message_text', models.TextField(blank=True, help_text='Лимит VK: 4096 символов.', verbose_name='Текст сообщения')),
                ('image', models.ImageField(blank=True, help_text='Зарезервировано: в первой версии API картинка не поддерживается.', null=True, upload_to='broadcasts/', verbose_name='Изображение')),
                ('mode', models.CharField(choices=[('restaurant', 'Кафе (визиты)'), ('delivery', 'Доставка')], default='restaurant', help_text='restaurant — визиты в кафе, delivery — активации доставки.', max_length=20, verbose_name='Матрица')),
                ('r_score', models.PositiveSmallIntegerField(blank=True, help_text='Координата ячейки матрицы, которую видел пользователь.', null=True, verbose_name='R (давность)')),
                ('f_score', models.PositiveSmallIntegerField(blank=True, help_text='Координата ячейки матрицы, которую видел пользователь.', null=True, verbose_name='F (частота)')),
                ('start', models.DateField(blank=True, help_text='Пусто — окно матрицы по умолчанию (последние 30 дней).', null=True, verbose_name='Период с')),
                ('end', models.DateField(blank=True, null=True, verbose_name='Период по')),
                ('branch_ids', models.JSONField(default=list, help_text='Список PK торговых точек, по которым уйдёт рассылка.', verbose_name='Точки (PK LoyalUP)')),
                ('gender_filter', models.CharField(choices=[('all', 'Все'), ('m', 'Мужчины'), ('f', 'Женщины')], default='all', max_length=3, verbose_name='Пол')),
                ('variants', models.JSONField(default=list, help_text='[{"percent": 50, "message_text": "..."}] — сумма процентов 100.', verbose_name='Варианты текста (A/B)')),
                ('status', models.CharField(choices=[('draft', 'Черновик'), ('sent', 'Отправлен'), ('archived', 'В архиве')], db_index=True, default='draft', max_length=20, verbose_name='Статус')),
                ('created_by', models.CharField(blank=True, help_text='Логин пользователя на момент создания черновика.', max_length=150, verbose_name='Кто создал')),
                ('sent_at', models.DateTimeField(blank=True, null=True, verbose_name='Отправлен')),
                ('last_send', models.ForeignKey(blank=True, help_text='Первый из созданных запусков пачки — ссылка на историю.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='senler.broadcastsend', verbose_name='Запуск')),
                ('segment', models.ForeignKey(blank=True, help_text='Пусто — рассылка всем оцифрованным гостям выбранных точек.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='broadcast_drafts', to='analytics.rfsegment', verbose_name='RF-сегмент')),
            ],
            options={
                'verbose_name': 'Черновик рассылки (внешний кабинет)',
                'verbose_name_plural': 'Черновики рассылок (внешний кабинет)',
                'ordering': ['-updated_at'],
            },
        ),
    ]
