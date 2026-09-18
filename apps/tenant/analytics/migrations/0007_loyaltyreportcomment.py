"""
Комментарии к отчёту по лояльности — новая таблица (контракт 3б.4, №28).

Миграция написана руками (makemigrations не запускался): таблица новая, поля
с `default` добавляются вместе с ней, поэтому грабли 17.09 про `db_default` на
существующих таблицах здесь не применимы — ALTER TABLE ... SET DEFAULT по живым
строкам не идёт.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('analytics', '0006_rfmcampaign'),
    ]

    operations = [
        migrations.CreateModel(
            name='LoyaltyReportComment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('period_start', models.DateField(verbose_name='Начало периода')),
                ('period_end', models.DateField(verbose_name='Конец периода')),
                ('branch_ids', models.JSONField(
                    blank=True, default=list,
                    help_text='Пустой список = отчёт по всей сети.',
                    verbose_name='Точки (внутренние id)')),
                ('branch_key', models.CharField(
                    db_index=True, max_length=255,
                    help_text='Строка для уникальности: «1,3» или «all».',
                    verbose_name='Ключ набора точек')),
                ('section_num', models.PositiveSmallIntegerField(verbose_name='Номер секции')),
                ('text', models.TextField(blank=True, verbose_name='Текст комментария')),
                ('is_ai', models.BooleanField(
                    default=False,
                    help_text='Комментарий написан ИИ (менеджер мог его потом поправить).',
                    verbose_name='Сгенерирован ИИ')),
                ('author', models.CharField(
                    blank=True, max_length=150,
                    help_text='Кто сохранил последнюю версию (username).',
                    verbose_name='Автор')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создан')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлён')),
            ],
            options={
                'verbose_name': 'Комментарий к отчёту',
                'verbose_name_plural': 'Комментарии к отчёту',
                'ordering': ['section_num'],
            },
        ),
        migrations.AddIndex(
            model_name='loyaltyreportcomment',
            index=models.Index(fields=['period_start', 'period_end', 'branch_key'],
                               name='report_comment_period_idx'),
        ),
        migrations.AlterUniqueTogether(
            name='loyaltyreportcomment',
            unique_together={('period_start', 'period_end', 'branch_key', 'section_num')},
        ),
    ]
