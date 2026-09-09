# Предполагаемая точка для тредов отзывов из ВК-группы (09.09.2026).
# Аддитивно: 5 nullable/дефолтных полей на TestimonialConversation.
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0033_auto_ack_negative'),
    ]

    operations = [
        migrations.AddField(
            model_name='testimonialconversation',
            name='inferred_branch',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='inferred_testimonials',
                to='branch.branch',
                verbose_name='Предполагаемая точка (по скану)',
                help_text='Для тредов из ВК-группы (branch пуст): точка последнего скана гостя перед сообщением. Это подсказка, а не выбор гостя.',
            ),
        ),
        migrations.AddField(
            model_name='testimonialconversation',
            name='inferred_table_number',
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name='Стол (по скану)'),
        ),
        migrations.AddField(
            model_name='testimonialconversation',
            name='inferred_scan_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Время скана'),
        ),
        migrations.AddField(
            model_name='testimonialconversation',
            name='inferred_source',
            field=models.CharField(
                blank=True, default='', max_length=12,
                choices=[('qr_scan', 'скан QR'), ('visit', 'визит')],
                verbose_name='Источник подсказки',
            ),
        ),
        migrations.AddField(
            model_name='testimonialconversation',
            name='inferred_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Когда определили'),
        ),
    ]
