# Рукописная initial-миграция AI-маркетолога (marketer).
# Аддитивно: две новые таблицы в схеме тенанта, чужих таблиц не трогает.
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='MarketerSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('is_enabled', models.BooleanField(default=False, help_text='Мастер-флаг: выключено — ни одна задача маркетолога не работает.', verbose_name='Включён')),
                ('vk_group_id', models.PositiveBigIntegerField(blank=True, help_text='Числовой ID сообщества (без минуса). Пусто — берётся из SenlerConfig первой точки.', null=True, verbose_name='ID группы VK')),
                ('vk_wall_token', models.CharField(blank=True, help_text='ПОЛЬЗОВАТЕЛЬСКИЙ токен админа сообщества с правами wall,photos,offline. НЕ senler-токен и НЕ community-токен (тот стену постить не умеет). Пусто — посты остаются черновиками, публикация недоступна.', max_length=512, verbose_name='Токен для стены')),
                ('autopost_enabled', models.BooleanField(default=False, help_text='Выключено (по умолчанию) — маркетолог только готовит черновики, публикация вручную из админки. Включено — сгенерированный дайджест публикуется на стену сразу.', verbose_name='Автопубликация')),
                ('digest_enabled', models.BooleanField(default=True, help_text='Еженедельный пост-дайджест из данных лояльности за неделю.', verbose_name='Дайджест «Что нового?»')),
                ('digest_weekday', models.PositiveSmallIntegerField(choices=[(0, 'Понедельник'), (1, 'Вторник'), (2, 'Среда'), (3, 'Четверг'), (4, 'Пятница'), (5, 'Суббота'), (6, 'Воскресенье')], default=0, verbose_name='День дайджеста')),
                ('digest_hour', models.PositiveSmallIntegerField(default=12, help_text='0–23. Черновик готовится в этот час (тик раз в час).', verbose_name='Час дайджеста (МСК)')),
                ('last_digest_at', models.DateTimeField(blank=True, editable=False, null=True, verbose_name='Последний дайджест')),
                ('brand_voice', models.TextField(blank=True, help_text='Как писать: голос бренда, обращение к гостям, что нельзя упоминать. Передаётся ИИ при каждой генерации.', verbose_name='Тон бренда')),
                ('extra_facts', models.TextField(blank=True, help_text='Актуальные акции, новинки меню, события — всё, чего нет в данных системы. ИИ использует ТОЛЬКО факты отсюда и из данных лояльности.', verbose_name='Факты от владельца')),
            ],
            options={
                'verbose_name': 'Настройки AI-маркетолога',
                'verbose_name_plural': 'Настройки AI-маркетолога',
            },
        ),
        migrations.CreateModel(
            name='MarketerPost',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('post_type', models.CharField(choices=[('digest', 'Дайджест «Что нового?»'), ('insight', 'Инсайт лояльности'), ('promo', 'Промо'), ('custom', 'Произвольный')], db_index=True, default='digest', max_length=16, verbose_name='Тип')),
                ('status', models.CharField(choices=[('draft', 'Черновик'), ('published', 'Опубликован'), ('rejected', 'Отклонён'), ('failed', 'Ошибка публикации')], db_index=True, default='draft', max_length=16, verbose_name='Статус')),
                ('text', models.TextField(verbose_name='Текст поста')),
                ('context_snapshot', models.JSONField(blank=True, default=dict, help_text='Knowledge Core на момент генерации — из каких фактов написан пост.', verbose_name='Снимок данных')),
                ('model_used', models.CharField(blank=True, max_length=64, verbose_name='Модель ИИ')),
                ('created_by', models.CharField(blank=True, help_text='ai — сгенерирован задачей; иначе username администратора.', max_length=150, verbose_name='Автор')),
                ('published_at', models.DateTimeField(blank=True, null=True, verbose_name='Опубликован')),
                ('vk_post_id', models.CharField(blank=True, help_text='post_id из ответа wall.post.', max_length=32, verbose_name='ID поста VK')),
                ('error', models.TextField(blank=True, verbose_name='Ошибка')),
            ],
            options={
                'verbose_name': 'Пост маркетолога',
                'verbose_name_plural': 'Посты маркетолога',
                'ordering': ['-created_at'],
            },
        ),
    ]
