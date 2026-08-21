# Каталог наград (фаза 4): справочник призов с тирами G1/G2/G3, весами
# и лимитами выдачи для RFM-наград и RF-авторассылок.
# Аддитивно: только новая таблица, существующие модели не затрагиваются.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0006_storygiftentry_claim_expires_at_and_more'),
        ('catalog', '0006_product_cost_price_rub'),
        ('branch', '0027_alter_qrcode_mode'),
    ]

    operations = [
        migrations.CreateModel(
            name='RewardCatalogItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('name', models.CharField(blank=True, help_text='Пусто — берётся название выбранного подарка.', max_length=255, verbose_name='Название')),
                ('internal_code', models.CharField(blank=True, help_text='Идентификатор для сотрудника и журнала выдачи. Необязателен.', max_length=64, verbose_name='Внутренний код')),
                ('image', models.ImageField(blank=True, help_text='Пусто — берётся изображение выбранного подарка.', null=True, upload_to='reward_catalog/', verbose_name='Изображение')),
                ('description', models.TextField(blank=True, help_text='Пусто — берётся описание выбранного подарка.', verbose_name='Описание')),
                ('cost_price', models.DecimalField(decimal_places=2, default=0, help_text='0 — берётся себестоимость выбранного подарка. Используется для бюджета кампаний и потолка по тиру. Фактические затраты по-прежнему считаются снимком на момент активации («Затраты на подарки»).', max_digits=10, verbose_name='Себестоимость, ₽')),
                ('min_order_amount', models.DecimalField(decimal_places=2, default=0, help_text='0 — без ограничения. Показывается гостю в сообщении и на экране подарка.', max_digits=10, verbose_name='Минимальная сумма заказа, ₽')),
                ('tier', models.CharField(choices=[('G1', 'G1 · Лёгкий (низкая себестоимость)'), ('G2', 'G2 · Средний'), ('G3', 'G3 · Ценный (VIP)')], default='G1', help_text='Сценарий рассылки задаёт тир, а система сама выбирает позицию внутри него.', max_length=2, verbose_name='Категория (тир)')),
                ('weight', models.PositiveIntegerField(default=1, help_text='Вероятность выпадения внутри тира: чем больше вес, тем чаще позиция достаётся гостю. По умолчанию более дешёвым позициям ставят больший вес. 0 — позиция участвует только если других не осталось.', verbose_name='Вес выбора')),
                ('default_lifetime_days', models.PositiveSmallIntegerField(default=10, help_text='Сколько дней у гостя есть на активацию, если сценарий не задал свой срок. Ориентиры ТЗ: G1 — 7 дней, G2 — 10-14, G3 — 14-21.', verbose_name='Срок действия по умолчанию, дней')),
                ('activation_limit', models.PositiveIntegerField(blank=True, help_text='Пусто — без лимита. Когда счётчик выдач достигает лимита, позиция перестаёт выпадать.', null=True, verbose_name='Лимит выдач')),
                ('issued_count', models.PositiveIntegerField(default=0, help_text='Счётчик выданных наград. Меняется системой, вручную не редактируется.', verbose_name='Выдано')),
                ('available_from', models.DateTimeField(blank=True, help_text='Пусто — без ограничения снизу.', null=True, verbose_name='Доступна с')),
                ('available_to', models.DateTimeField(blank=True, help_text='Пусто — без ограничения сверху.', null=True, verbose_name='Доступна до')),
                ('is_active', models.BooleanField(default=True, help_text='Снимите галочку, чтобы временно убрать позицию из выдачи, не удаляя её.', verbose_name='Активна')),
                ('is_archived', models.BooleanField(default=False, help_text='Скрытая позиция: не выпадает и не показывается в рабочих списках. История сохраняется.', verbose_name='Архивная')),
                ('available_for_rfm', models.BooleanField(default=True, help_text='Позицию можно назначить как награду RFM-сегменту и использовать в RF-авторассылках.', verbose_name='Доступна для RFM')),
                ('branch', models.ForeignKey(blank=True, help_text='Оставьте пустым, чтобы позиция была доступна всей сети.', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='reward_catalog_items', to='branch.branch', verbose_name='Торговая точка')),
                ('product', models.ForeignKey(blank=True, help_text='Необязательно. Если выбран — название, изображение, описание и себестоимость подтягиваются из карточки подарка, когда поля ниже оставлены пустыми. Один подарок может участвовать в нескольких позициях каталога наград (например в разных тирах или точках).', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='reward_catalog_entries', to='catalog.product', verbose_name='Подарок из каталога')),
            ],
            options={
                'verbose_name': 'Награда из каталога',
                'verbose_name_plural': 'Каталог наград',
                'ordering': ['tier', 'name', 'pk'],
            },
        ),
        migrations.AddIndex(
            model_name='rewardcatalogitem',
            index=models.Index(fields=['tier', 'is_active'], name='rewardcat_tier_active_idx'),
        ),
        migrations.AddIndex(
            model_name='rewardcatalogitem',
            index=models.Index(fields=['available_for_rfm'], name='rewardcat_rfm_idx'),
        ),
        migrations.AddIndex(
            model_name='rewardcatalogitem',
            index=models.Index(fields=['is_archived'], name='rewardcat_archived_idx'),
        ),
    ]
