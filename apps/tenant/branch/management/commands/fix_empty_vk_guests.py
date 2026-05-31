"""
Догнать имена/screen_name для guest.Client с пустыми first_name+last_name.
Раньше backfill_vk_guests создавал пустые записи (vk_id only) для аккаунтов,
которых VK не вернул в users.get (deleted/banned/закрытые профили).

Эта команда:
  1. Запрашивает их ещё раз с fields=screen_name,deactivated,first_name,last_name
  2. Заполняет first_name из правил:
     - first_name + last_name (если VK всё-таки отдал — иногда users.get с разными
       fields возвращает разное)
     - screen_name → first_name = "@screen_name"
     - deactivated="deleted" → first_name = "Удалённый профиль"
     - deactivated="banned"  → first_name = "Заблокирован VK"
     - всё пусто             → first_name = f"vk{vk_id}" (чтобы не было пустоты)

Usage:
    docker exec web python manage.py fix_empty_vk_guests
    docker exec web python manage.py fix_empty_vk_guests --dry-run
"""
from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Заполнить first_name/last_name для пустых guest.Client (через VK screen_name/deactivated).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--batch', type=int, default=1000)

    def handle(self, *args, **opts):
        dry_run = bool(opts.get('dry_run'))
        batch_size = min(int(opts.get('batch') or 1000), 1000)

        from django_tenants.utils import get_tenant_model, schema_context
        from apps.shared.guest.models import Client as GuestClient
        from apps.tenant.senler.models import SenlerConfig

        # Берём любой токен из тенантов
        token = None
        for t in get_tenant_model().objects.exclude(schema_name='public'):
            with schema_context(t.schema_name):
                cfg = SenlerConfig.objects.exclude(vk_community_token='').first()
                if cfg and cfg.vk_community_token:
                    token = cfg.vk_community_token
                    break
        if not token:
            self.stderr.write('Нет ни одного vk_community_token — не могу запросить VK.')
            return

        empty_ids = list(
            GuestClient.objects.filter(first_name='', last_name='').values_list('vk_id', flat=True)
        )
        if not empty_ids:
            self.stdout.write(self.style.SUCCESS('Нет пустых guest.Client, всё уже заполнено.'))
            return
        self.stdout.write(f'Пустых guest.Client: {len(empty_ids)}. Запрашиваю VK с extended fields...')

        import json, urllib.parse, urllib.request
        updated = 0
        stats = {'name': 0, 'screen_name': 0, 'deleted': 0, 'banned': 0, 'fallback': 0}

        for i in range(0, len(empty_ids), batch_size):
            batch = empty_ids[i:i + batch_size]
            url = (
                'https://api.vk.com/method/users.get?'
                + urllib.parse.urlencode({
                    'user_ids': ','.join(map(str, batch)),
                    'fields':   'screen_name,deactivated,first_name,last_name,photo_100',
                    'access_token': token,
                    'v': '5.131',
                })
            )
            try:
                with urllib.request.urlopen(url, timeout=20) as resp:
                    data = json.loads(resp.read())
            except Exception as e:
                self.stderr.write(f'VK error batch {batch[0]}: {e}')
                continue
            if 'error' in data:
                self.stderr.write(f'VK API error: {data["error"]}')
                continue

            response_by_id = {int(it.get('id', 0)): it for it in data.get('response', []) if it.get('id')}

            for vk_id in batch:
                first = last = photo = ''
                source = 'fallback'

                it = response_by_id.get(vk_id)
                if it:
                    f = (it.get('first_name') or '').strip()
                    l = (it.get('last_name') or '').strip()
                    deact = it.get('deactivated')
                    sn = (it.get('screen_name') or '').strip()
                    photo = (it.get('photo_100') or '')[:500]

                    if f or l:
                        first, last = f[:255], l[:255]
                        source = 'name'
                    elif sn:
                        first = f'@{sn}'[:255]
                        source = 'screen_name'
                    elif deact == 'deleted':
                        first = 'Удалённый профиль'
                        source = 'deleted'
                    elif deact == 'banned':
                        first = 'Заблокирован VK'
                        source = 'banned'
                    else:
                        first = f'vk{vk_id}'
                else:
                    # VK вообще не вернул запись — точно удалён
                    first = f'vk{vk_id}'

                if source == 'fallback' and not first.startswith('@') and first != 'Удалённый профиль':
                    pass  # already set first=fallback
                stats[source] = stats.get(source, 0) + 1

                if dry_run:
                    if i == 0 and stats[source] <= 3:
                        self.stdout.write(f'  DRY vk={vk_id} → "{first}|{last}" (source={source})')
                else:
                    GuestClient.objects.filter(vk_id=vk_id).update(
                        first_name=first.strip(), last_name=last.strip(), photo_url=photo or '',
                    )
                    updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'DONE. Обновлено {updated} (из {len(empty_ids)}). Источники: {stats}.'
        ))
