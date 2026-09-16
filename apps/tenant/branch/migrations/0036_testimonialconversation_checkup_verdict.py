from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0035_testimonialmessage_branch'),
    ]

    operations = [
        migrations.AddField(
            model_name='testimonialconversation',
            name='checkup_status',
            field=models.CharField(blank=True, choices=[('in_progress', 'В работе в CheckUp'), ('resolved', 'Решено в CheckUp'), ('rejected', 'Отклонено в CheckUp')], db_default='', default='', max_length=20, verbose_name='Статус жалобы в CheckUp'),
        ),
        migrations.AddField(
            model_name='testimonialconversation',
            name='checkup_verdict',
            field=models.TextField(blank=True, db_default='', default='', verbose_name='Вердикт CheckUp'),
        ),
        migrations.AddField(
            model_name='testimonialconversation',
            name='checkup_manager',
            field=models.CharField(blank=True, db_default='', default='', max_length=120, verbose_name='Менеджер CheckUp'),
        ),
        migrations.AddField(
            model_name='testimonialconversation',
            name='checkup_complaint_id',
            field=models.CharField(blank=True, db_default='', default='', max_length=40, verbose_name='ID жалобы в CheckUp'),
        ),
        migrations.AddField(
            model_name='testimonialconversation',
            name='checkup_verdict_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Вердикт получен'),
        ),
        migrations.AddField(
            model_name='testimonialconversation',
            name='checkup_resolved_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Жалоба закрыта в CheckUp'),
        ),
    ]
