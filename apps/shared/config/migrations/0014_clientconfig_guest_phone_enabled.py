# №78 карты переезда: пер-тенантный флаг «Поделиться номером» в мини-аппе.
# Shared-модель (public-схема, одна строка на компанию) → `migrate_schemas --shared`.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0013_clientconfig_admin_brand_enabled'),
    ]

    operations = [
        migrations.AddField(
            model_name='clientconfig',
            name='guest_phone_enabled',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'В профиле мини-приложения появляется кнопка «Поделиться номером»: '
                    'гость даёт телефон через окно согласия ВКонтакте, номер виден в '
                    'карточке гостя и уходит в CheckUp с жалобой. Работает только при '
                    'общем выключателе платформы GUEST_PHONE_ENABLED. Выключено — как раньше.'
                ),
                verbose_name='Телефон гостя с согласия через ВК (№78)',
            ),
        ),
    ]
