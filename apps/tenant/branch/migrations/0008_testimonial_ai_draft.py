"""
Migration: добавляет поля ai_draft + ai_draft_rejected в TestimonialConversation
для мобильных эндпоинтов /reviews/{id}/regenerate-draft/ и /reject-draft/.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0007_reviewautoreplyconfig'),
    ]

    operations = [
        migrations.AddField(
            model_name='testimonialconversation',
            name='ai_draft',
            field=models.TextField(
                blank=True,
                help_text='Сгенерированный AI вариант ответа на отзыв (для одобрения админом).',
                verbose_name='AI-черновик ответа',
            ),
        ),
        migrations.AddField(
            model_name='testimonialconversation',
            name='ai_draft_rejected',
            field=models.BooleanField(
                default=False,
                help_text='Админ отклонил черновик — больше не предлагать.',
                verbose_name='AI-черновик отклонён',
            ),
        ),
    ]
