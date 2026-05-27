from django.db import migrations, models
import django.db.models.deletion


def copy_fk_to_m2m(apps, schema_editor):
    """Перенос Quest.branch (FK) → QuestBranch (через-таблица M2M)."""
    Quest = apps.get_model('quest', 'Quest')
    QuestBranch = apps.get_model('quest', 'QuestBranch')
    for q in Quest.objects.all():
        if q.branch_id:
            QuestBranch.objects.get_or_create(
                quest=q,
                branch_id=q.branch_id,
                defaults={'ordering': q.ordering, 'is_active': q.is_active},
            )


def reverse_copy(apps, schema_editor):
    """Откат: удаляем все QuestBranch (старый FK сохранился, данные не теряем)."""
    QuestBranch = apps.get_model('quest', 'QuestBranch')
    QuestBranch.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('quest', '0001_initial'),
        ('branch', '0013_branchconfig_code_prompt_message_and_more'),
    ]

    operations = [
        # 1. Удаляем legacy-индекс по branch (новое имя для нового индекса будет на QuestBranch)
        migrations.RemoveIndex(
            model_name='quest',
            name='quest_branch_active_idx',
        ),
        # 2. Создаём through-модель QuestBranch
        migrations.CreateModel(
            name='QuestBranch',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('ordering', models.PositiveIntegerField(db_index=True, default=0, help_text='Меньшее значение отображается выше в списке гостей.', verbose_name='Порядок')),
                ('is_active', models.BooleanField(default=True, help_text='Неактивный квест не виден гостям этой точки.', verbose_name='Активен на этой точке')),
                ('branch', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='quest_assignments', to='branch.branch', verbose_name='Торговая точка')),
                ('quest', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='branch_assignments', to='quest.quest', verbose_name='Квест')),
            ],
            options={
                'verbose_name': 'Назначение квеста в точку',
                'verbose_name_plural': 'Назначения квестов в точки',
                'ordering': ['ordering'],
                'unique_together': {('quest', 'branch')},
            },
        ),
        migrations.AddIndex(
            model_name='questbranch',
            index=models.Index(fields=['branch', 'is_active', 'ordering'], name='qb_branch_active_idx'),
        ),
        # 3. Меняем Quest.branch на nullable legacy с другим related_name
        migrations.AlterField(
            model_name='quest',
            name='branch',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='quests_legacy',
                to='branch.branch',
                verbose_name='Торговая точка (legacy)',
                help_text='Устарело — точки задаются через M2M «Торговые точки».',
            ),
        ),
        # 4. Добавляем новое M2M-поле branches через QuestBranch
        migrations.AddField(
            model_name='quest',
            name='branches',
            field=models.ManyToManyField(
                blank=True,
                help_text='Один квест может работать сразу на нескольких точках.',
                related_name='quests',
                through='quest.QuestBranch',
                to='branch.branch',
                verbose_name='Торговые точки',
            ),
        ),
        # 5. Копируем существующие FK → M2M
        migrations.RunPython(copy_fk_to_m2m, reverse_copy),
    ]
