from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0046_guest_note'),
    ]

    operations = [
        migrations.AlterField(
            model_name='guestnote',
            name='kind',
            field=models.CharField(choices=[('note', 'Заметка'), ('allergy', 'Аллергия / нельзя'), ('occasion', 'Повод с датой'), ('message', 'Сообщение гостю'), ('not_same', 'Не тот же человек'), ('greeted', 'Поздравили с поводом')], default='note', max_length=12, verbose_name='Вид'),
        ),
    ]
