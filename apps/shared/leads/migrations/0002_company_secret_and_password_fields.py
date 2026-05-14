"""
Pack F4: добавляет
  - Lead.initial_password_hint, Lead.email_sent_at
  - Новую модель CompanySecret для хранения VK group token

Аддитивно. Существующие записи Lead получат пустые значения новых полей.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('clients', '0001_initial'),
        ('leads', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='lead',
            name='initial_password_hint',
            field=models.CharField(
                blank=True, max_length=64,
                verbose_name='Сгенерированный пароль (временно)',
            ),
        ),
        migrations.AddField(
            model_name='lead',
            name='email_sent_at',
            field=models.DateTimeField(
                blank=True, null=True,
                verbose_name='Email с creds отправлен',
            ),
        ),
        migrations.CreateModel(
            name='CompanySecret',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('vk_group_token', models.TextField(blank=True, verbose_name='VK group access token')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('company', models.OneToOneField(
                    on_delete=models.deletion.CASCADE,
                    related_name='secret',
                    to='clients.company',
                    verbose_name='Компания',
                )),
                ('created_from_lead', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=models.deletion.SET_NULL,
                    related_name='created_secrets',
                    to='leads.lead',
                    verbose_name='Из заявки',
                )),
            ],
            options={
                'verbose_name': 'Секреты компании',
                'verbose_name_plural': 'Секреты компаний',
            },
        ),
    ]
