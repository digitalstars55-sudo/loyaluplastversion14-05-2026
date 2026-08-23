# Только help_text (схема БД не меняется): community-токен с правом «Стена»
# постить умеет — проверено вживую на LevOne 23.08.2026.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('marketer', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='marketersettings',
            name='vk_wall_token',
            field=models.CharField(blank=True, help_text='Токен с правом «Стена»: токен сообщества (Управление → Работа с API → Ключи доступа, галочка «Стена») или пользовательский токен админа. НЕ senler-токен (у того «Сообщения» — каналы изолированы). Пусто — посты остаются черновиками, публикация недоступна.', max_length=512, verbose_name='Токен для стены'),
        ),
    ]
