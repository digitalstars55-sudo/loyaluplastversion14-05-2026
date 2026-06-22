import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0024_alter_clientvkstatus_community_source_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='QRCode',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('name', models.CharField(help_text='Где размещён QR. Напр.: «Детская зона — листовка», «Флаер у кассы».', max_length=120, verbose_name='Название')),
                ('key', models.CharField(db_index=True, editable=False, help_text='Автогенерируется. Передаётся в ссылке как src=<метка>.', max_length=16, unique=True, verbose_name='Метка (src)')),
                ('mode', models.CharField(choices=[('cafe', 'В кафе (на месте)'), ('delivery', 'Доставка')], default='cafe', help_text='«Доставка» добавляет в ссылку delivery=true.', max_length=16, verbose_name='Тип ссылки')),
                ('is_active', models.BooleanField(default=True, help_text='Выключенный QR не учитывает новые сканы (старая статистика сохраняется).', verbose_name='Активен')),
                ('branch', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='qr_codes', to='branch.branch', verbose_name='Торговая точка')),
            ],
            options={
                'verbose_name': 'Точка контакта (QR)',
                'verbose_name_plural': 'Точки контакта (QR)',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddField(
            model_name='clientbranch',
            name='active_qr',
            field=models.ForeignKey(blank=True, help_text='Последний отслеживаемый QR-код («точка контакта»), по которому гость вошёл. Используется для атрибуции событий воронки (модель last-touch).', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='active_for', to='branch.qrcode', verbose_name='Активная точка контакта'),
        ),
        migrations.AddField(
            model_name='clientbranch',
            name='active_qr_at',
            field=models.DateTimeField(blank=True, editable=False, null=True, verbose_name='Время последнего скана точки контакта'),
        ),
        migrations.CreateModel(
            name='QRScan',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('scanned_at', models.DateTimeField(auto_now_add=True, verbose_name='Время скана')),
                ('client', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='qr_scans', to='branch.clientbranch', verbose_name='Гость')),
                ('qr', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='scans', to='branch.qrcode', verbose_name='Точка контакта')),
            ],
            options={
                'verbose_name': 'Скан точки контакта',
                'verbose_name_plural': 'Сканы точек контакта',
                'ordering': ['-scanned_at'],
                'indexes': [
                    models.Index(fields=['qr', '-scanned_at'], name='qrscan_qr_time_idx'),
                    models.Index(fields=['scanned_at'], name='qrscan_time_idx'),
                ],
            },
        ),
    ]
