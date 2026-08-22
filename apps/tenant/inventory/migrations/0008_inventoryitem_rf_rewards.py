# Фаза 5 (RF/RFM-награды): InventoryItem получает срок забора (claim_expires_at,
# сгорание ленивое — как у StoryGiftEntry), связь с позицией каталога наград
# (лимиты/компенсации) и снимок мин. суммы заказа. Новые источники получения:
# rfm (RFM-кампания) и rf_auto (RF-авторассылка). Аддитивно: существующие
# записи не затрагиваются (новые поля NULL/0).

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0007_rewardcatalogitem'),
    ]

    operations = [
        migrations.AddField(
            model_name='inventoryitem',
            name='catalog_item',
            field=models.ForeignKey(
                blank=True,
                help_text='Заполнена у наград, выданных из «Каталога наград» (RFM-кампании, RF-авторассылки). Нужна для лимитов, компенсаций и правила «не повторять три последних подарка».',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='issued_items',
                to='inventory.rewardcatalogitem',
                verbose_name='Позиция каталога наград',
            ),
        ),
        migrations.AddField(
            model_name='inventoryitem',
            name='claim_expires_at',
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                help_text='Срок, до которого подарок нужно активировать. Не активировали — сгорает (статус «Истёк» без активации). Пусто — бессрочно; у подарков, выданных до появления поля, всегда пусто.',
                null=True,
                verbose_name='Забрать до',
            ),
        ),
        migrations.AddField(
            model_name='inventoryitem',
            name='min_order_amount',
            field=models.PositiveIntegerField(
                default=0,
                help_text='Снимок условия из каталога наград на момент выдачи. 0 — без ограничения.',
                verbose_name='Мин. сумма заказа, ₽',
            ),
        ),
        migrations.AlterField(
            model_name='inventoryitem',
            name='acquired_from',
            field=models.CharField(
                choices=[
                    ('purchase', 'Покупка за баллы'),
                    ('super_prize', 'Суперприз'),
                    ('birthday', 'Подарок на ДР'),
                    ('manual', 'Выдано вручную'),
                    ('rfm', 'RFM-кампания'),
                    ('rf_auto', 'RF-авторассылка'),
                ],
                max_length=20,
                verbose_name='Способ получения',
            ),
        ),
    ]
