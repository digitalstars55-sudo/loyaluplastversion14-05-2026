"""Каналы гостя по уже известным данным: ВК у каждого гостя, телефон с согласием, Телеграм из привязок.

Только добавляет строки (ignore_conflicts): повтор безопасен, данные гостя не меняются.
"""
from django.db import migrations
from django.utils import timezone


def заполнить(apps, schema_editor):
    Client = apps.get_model('guest', 'Client')
    TelegramLink = apps.get_model('guest', 'TelegramLink')
    GuestChannel = apps.get_model('guest', 'GuestChannel')
    now = timezone.now()
    пачка = []

    def сбросить():
        if пачка:
            GuestChannel.objects.bulk_create(пачка, ignore_conflicts=True, batch_size=2000)
            пачка.clear()

    for cid, vk_id, phone, src, created in (Client.objects.filter(is_synthetic_vk=False)
                                            .values_list('id', 'vk_id', 'phone', 'phone_source', 'created_at')
                                            .iterator(chunk_size=5000)):
        пачка.append(GuestChannel(client_id=cid, kind='vk', external_id=str(vk_id), verified=True,
                                  first_seen=created, last_seen=created, created_at=now, updated_at=now))
        if phone:
            пачка.append(GuestChannel(client_id=cid, kind='phone', external_id=phone[:64], verified=src == 'vk',
                                      first_seen=created, last_seen=created, created_at=now, updated_at=now))
        if len(пачка) >= 4000:
            сбросить()
    for tg_id, cid, created in TelegramLink.objects.values_list('tg_user_id', 'client_id', 'created_at'):
        пачка.append(GuestChannel(client_id=cid, kind='tg', external_id=str(tg_id), verified=True,
                                  first_seen=created, last_seen=created, created_at=now, updated_at=now))
    сбросить()


class Migration(migrations.Migration):

    dependencies = [('guest', '0011_guest_channels')]

    operations = [migrations.RunPython(заполнить, migrations.RunPython.noop)]
