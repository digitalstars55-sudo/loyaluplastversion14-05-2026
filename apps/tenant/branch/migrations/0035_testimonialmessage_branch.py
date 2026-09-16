# Точка у самого сообщения отзыва (16.09.2026).
#
# Раньше точка жила только на треде (TestimonialConversation.branch), а у
# сообщения был голый номер стола. Столы на разных точках повторяются, поэтому
# по «Стол 7» точку не отличить. Аддитивно: одно nullable-поле + разовый досыл
# точки треда в старые APP-сообщения (форвард-онли, назад просто снимаем поле).
from django.db import migrations, models
from django.db.models import OuterRef, Subquery
import django.db.models.deletion


def fill_branch_from_conversation(apps, schema_editor):
    """Старым отзывам из приложения проставляем точку их треда.

    Тред заводится на пару (точка, гость), поэтому у APP-сообщения точка треда
    и есть та, что прислал мини-апп. ВК-сообщения и ответы админа не трогаем:
    у ВК-тредов точки нет вообще, а ответ админа — не про место визита.
    """
    TestimonialMessage = apps.get_model('branch', 'TestimonialMessage')
    TestimonialConversation = apps.get_model('branch', 'TestimonialConversation')
    # update() не умеет F() через join — берём точку треда подзапросом.
    thread_branch = Subquery(
        TestimonialConversation.objects
        .filter(pk=OuterRef('conversation_id'))
        .values('branch_id')[:1]
    )
    (TestimonialMessage.objects
     .filter(source='APP', branch__isnull=True, conversation__branch__isnull=False)
     .update(branch=thread_branch))


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0034_testimonial_inferred_branch'),
    ]

    operations = [
        migrations.AddField(
            model_name='testimonialmessage',
            name='branch',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='testimonial_messages',
                to='branch.branch',
                verbose_name='Точка (из ссылки отзыва)',
            ),
        ),
        migrations.RunPython(fill_branch_from_conversation, noop_reverse),
    ]
