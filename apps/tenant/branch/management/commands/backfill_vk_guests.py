"""
Догнать имена/фото для conv'ов с vk_sender_id, но без привязки vk_guest.
До этого conv создавался только если гость регистрировался в миниаппе;
кто писал в группу но не играл — оставался anon, в UI «Гость ВК».

Сейчас новые сообщения автоматически создают guest.Client (см. ensure_vk_guest
в branch/api/services.py). Эта команда — одноразовый догон по уже накопленным.

Usage:
    docker exec web python manage.py backfill_vk_guests              # все тенанты
    docker exec web python manage.py backfill_vk_guests --schema asap_orel
    docker exec web python manage.py backfill_vk_guests --dry-run
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django_tenants.utils import get_tenant_model, schema_context


class Command(BaseCommand):
    help = 'Создать недостающие guest.Client для conv с vk_sender_id (anon → имя/фото).'

    def add_arguments(self, parser):
        parser.add_argument('--schema', help='Только указанный schema_name (иначе все).')
        parser.add_argument('--dry-run', action='store_true', help='Не сохранять, только лог.')
        parser.add_argument('--batch', type=int, default=1000,
            help='Размер батча vk.users.get (макс 1000, рекомендуем оставить).')

    def handle(self, *args, **opts):
        target_schema = opts.get('schema')
        dry_run = bool(opts.get('dry_run'))
        batch_size = min(int(opts.get('batch') or 1000), 1000)

        # 1) Собираем уникальные vk_sender_id со ВСЕХ тенантов (anon conv'ы).
        TenantModel = get_tenant_model()
        qs = TenantModel.objects.exclude(schema_name='public')
        if target_schema:
            qs = qs.filter(schema_name=target_schema)

        all_anon_ids: set[int] = set()
        per_tenant_ids: dict[str, set[int]] = {}
        for t in qs:
            with schema_context(t.schema_name):
                from apps.tenant.branch.models import TestimonialConversation
                ids = set()
                for vk_sender in (
                    TestimonialConversation.objects
                    .filter(vk_guest__isnull=True, client__isnull=True)
                    .exclude(vk_sender_id='')
                    .values_list('vk_sender_id', flat=True)
                    .distinct()
                ):
                    try:
                        v = int(vk_sender)
                        if v > 0:
                            ids.add(v)
                    except (ValueError, TypeError):
                        continue
                per_tenant_ids[t.schema_name] = ids
                all_anon_ids.update(ids)
                self.stdout.write(f'  [{t.schema_name}] anon vk_sender_id: {len(ids)}')

        if not all_anon_ids:
            self.stdout.write(self.style.SUCCESS('Ничего догонять не нужно.'))
            return

        # 2) Берём СПИСОК ID, которых ещё нет в guest.Client → нужно создать.
        from apps.shared.guest.models import Client as GuestClient
        existing_ids = set(
            GuestClient.objects.filter(vk_id__in=all_anon_ids).values_list('vk_id', flat=True)
        )
        missing_ids = sorted(all_anon_ids - existing_ids)
        self.stdout.write(
            f'Уникальных anon vk_id всего: {len(all_anon_ids)}. '
            f'Уже есть guest.Client: {len(existing_ids)}. '
            f'Создать через VK: {len(missing_ids)}.'
        )

        # 3) vk.users.get batch'ами по 1000 — экономия запросов.
        if missing_ids:
            from apps.tenant.senler.models import SenlerConfig
            # Любой тенант с токеном подойдёт — vk.users.get работает с любого community token.
            token = None
            for t in qs:
                with schema_context(t.schema_name):
                    cfg = SenlerConfig.objects.exclude(vk_community_token='').first()
                    if cfg and cfg.vk_community_token:
                        token = cfg.vk_community_token
                        break
            if not token:
                self.stderr.write('Не нашёл vk_community_token — создаю записи только с vk_id (без имён).')
                self._create_empty(missing_ids, dry_run)
            else:
                self._fetch_and_create(missing_ids, token, batch_size, dry_run)
                # VK silently skips deleted/banned/private accounts → для НЕ
                # созданных создаём пустые записи. UI покажет «vk{id}» вместо
                # «Гость ВК» — хоть какая-то идентификация.
                if not dry_run:
                    still_missing = set(missing_ids) - set(
                        GuestClient.objects.filter(vk_id__in=missing_ids).values_list('vk_id', flat=True)
                    )
                    if still_missing:
                        self.stdout.write(
                            f'  VK не вернул {len(still_missing)} аккаунтов (удалены/баннены) → создаю пустые.'
                        )
                        self._create_empty(sorted(still_missing), False)

        # 4) Перепривязка conv'ов: для каждого тенанта проставляем vk_guest.
        if dry_run:
            self.stdout.write(self.style.SUCCESS(f'DRY-RUN: создал бы {len(missing_ids)} guest.Client, привязал бы conv\'ы.'))
            return

        total_relinked = 0
        for schema, ids in per_tenant_ids.items():
            if not ids:
                continue
            with schema_context(schema):
                from apps.tenant.branch.models import TestimonialConversation
                # Маппим vk_id → guest pk
                guests = {g.vk_id: g.pk for g in GuestClient.objects.filter(vk_id__in=ids)}
                count = 0
                for conv in TestimonialConversation.objects.filter(
                    vk_guest__isnull=True,
                    client__isnull=True,
                    vk_sender_id__in=[str(i) for i in ids],
                ):
                    try:
                        v = int(conv.vk_sender_id)
                    except (ValueError, TypeError):
                        continue
                    gpk = guests.get(v)
                    if gpk:
                        conv.vk_guest_id = gpk
                        conv.save(update_fields=['vk_guest'])
                        count += 1
                total_relinked += count
                self.stdout.write(f'  [{schema}] relinked: {count}')

        self.stdout.write(self.style.SUCCESS(
            f'DONE. guest.Client создано/найдено: {len(missing_ids)}. Conv\'ов привязано: {total_relinked}.'
        ))

    def _create_empty(self, ids: list[int], dry_run: bool) -> None:
        from apps.shared.guest.models import Client as GuestClient
        if dry_run:
            self.stdout.write(f'  DRY: создал бы {len(ids)} пустых guest.Client')
            return
        objs = [GuestClient(vk_id=i, first_name='', last_name='', photo_url='') for i in ids]
        GuestClient.objects.bulk_create(objs, ignore_conflicts=True)

    def _fetch_and_create(self, ids: list[int], token: str, batch_size: int, dry_run: bool) -> None:
        import json
        import urllib.parse, urllib.request
        from apps.shared.guest.models import Client as GuestClient

        created = 0
        for i in range(0, len(ids), batch_size):
            batch = ids[i:i + batch_size]
            url = (
                'https://api.vk.com/method/users.get?'
                + urllib.parse.urlencode({
                    'user_ids': ','.join(map(str, batch)),
                    'fields':   'first_name,last_name,photo_100,sex',
                    'access_token': token,
                    'v': '5.131',
                })
            )
            try:
                with urllib.request.urlopen(url, timeout=20) as resp:
                    data = json.loads(resp.read())
            except Exception as e:
                self.stderr.write(f'  VK error batch starting at {batch[0]}: {e}')
                if not dry_run:
                    # Fallback — пустые записи для этого батча
                    self._create_empty(batch, False)
                continue

            if 'error' in data:
                self.stderr.write(f'  VK error: {data["error"]}')
                if not dry_run:
                    self._create_empty(batch, False)
                continue

            objs = []
            for it in data.get('response', []):
                vk_id = int(it.get('id', 0))
                if vk_id <= 0:
                    continue
                sex = it.get('sex')
                gender = 'f' if sex == 1 else ('m' if sex == 2 else None)
                objs.append(GuestClient(
                    vk_id=vk_id,
                    first_name=(it.get('first_name') or '')[:255],
                    last_name=(it.get('last_name') or '')[:255],
                    photo_url=(it.get('photo_100') or '')[:500],
                    gender=gender,
                ))

            if dry_run:
                for o in objs[:5]:
                    self.stdout.write(f'  DRY: vk={o.vk_id} {o.first_name} {o.last_name}')
                self.stdout.write(f'  DRY: батч {len(objs)} (из {len(batch)})')
            else:
                GuestClient.objects.bulk_create(objs, ignore_conflicts=True)
                created += len(objs)

        self.stdout.write(f'  Создано guest.Client: {created} (из {len(ids)} запрошенных).')
