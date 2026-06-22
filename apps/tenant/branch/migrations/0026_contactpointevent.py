import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0025_qrcode_qrscan_contact_points'),
    ]

    operations = [
        migrations.CreateModel(
            name='ContactPointEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('stage', models.CharField(choices=[('subscribe', 'Подписался'), ('play', 'Сыграл'), ('activate', 'Активировал подарок')], max_length=16, verbose_name='Стадия воронки')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Время события')),
                ('client', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='cp_events', to='branch.clientbranch', verbose_name='Гость')),
                ('qr', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='events', to='branch.qrcode', verbose_name='Точка контакта')),
            ],
            options={
                'verbose_name': 'Событие точки контакта',
                'verbose_name_plural': 'События точек контакта',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='contactpointevent',
            index=models.Index(fields=['qr', 'stage', 'created_at'], name='cpevent_qr_stage_idx'),
        ),
        migrations.AddIndex(
            model_name='contactpointevent',
            index=models.Index(fields=['created_at'], name='cpevent_time_idx'),
        ),
        migrations.AlterUniqueTogether(
            name='contactpointevent',
            unique_together={('qr', 'client', 'stage')},
        ),
    ]
