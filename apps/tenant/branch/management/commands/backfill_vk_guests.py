"""
Догнать имена/фото для conv'ов с vk_sender_id, но без привязки vk_guest.
До этого conv создавался только если гость регистрировался в миниаппе;
кто писал в группу но не играл — оставался anon, в UI «Гость ВК».

Сейчас новые сообщения автоматически создают guest.Client (см. ensure_vk_guest
в branch/api/services.py). Эта команда — одноразовый догон по уже накопленным.

ОДИН проход с extended fields (VK_USERS_GET_FIELDS) — без двухэтапных запросов.
Все реальные данные из VK API + единый fallback в _vk_user_to_guest_fields.

Также подхватывает уже существующие guest.Client с пустым first_name+last_name
(legacy от старого backfill'а) — повторно запрашивает VK для них.

Usage:
    docker exec web python manage.py backfill_vk_guests              # все тенанты
    docker exec web python manage.py backfill_vk_guests --schema asap_orel
    docker exec web python manage.py backfill_vk_guests --dry-run
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django_tenants.utils import get_tenant_model, schema_context


class Command(BaseCommand):
    help = 'Создать недостающие guest.Client + дозаполнить пустые (через VK users.get extended fields).'

    def add_arguments(self, parser):
        parser.add_argument('--schema', help='Только указанный schema_name (иначе все).')
        parser.add_argument('--dry-run', action='store_true', help='Не сохранять, только лог.')

    def handle(self, *args, **opts):
        target_schema = opts.get('schema')
        dry_run = bool(opts.get('dry_run'))

        from apps.shared.guest.models import Client as GuestClient
        from apps.tenant.branch.api.services import (
            _vk_fetch_users, _vk_user_to_guest_fields,
        )
        from apps.tenant.senler.models import SenlerConfig

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

        # Также: уже существующие guest.Client с пустыми first/last (legacy
        # от старого однократно-проходного backfill'а) — тоже подхватываем,
        # чтобы один заход чинил ВСЁ.
        legacy_empty_ids = set(
            GuestClient.objects.filter(first_name='', last_name='').values_list('vk_id', flat=True)
        )
        if legacy_empty_ids:
            self.stdout.write(f'  Legacy пустых guest.Client (first+last=""): {len(legacy_empty_ids)}')

        # Что надо запросить из VK = (anon без guest.Client вообще) ∪ (legacy пустые)
        existing_for_anon = set(
            GuestClient.objects.filter(vk_id__in=all_anon_ids).values_list('vk_id', flat=True)
        )
        missing_ids = (all_anon_ids - existing_for_anon) | legacy_empty_ids
        missing_ids = sorted(i for i in missing_ids if i > 0)
        self.stdout.write(
            f'Уникальных vk_id для запроса: {len(missing_ids)} '
            f'(новых: {len(all_anon_ids - existing_for_anon)}, обновлений legacy пустых: {len(legacy_empty_ids)})'
        )

        if not missing_ids:
            self.stdout.write(self.style.SUCCESS('Ничего догонять не нужно.'))
            return

        # 2) Берём любой токен.
        token = None
        for t in qs:
            with schema_context(t.schema_name):
                cfg = SenlerConfig.objects.exclude(vk_community_token='').first()
                if cfg and cfg.vk_community_token:
                    token = cfg.vk_community_token
                    break
        if not token:
            self.stderr.write('Нет ни одного vk_community_token — пропускаю.')
            return

        # 3) ОДИН запрос с extended fields (батчами по 1000).
        users = _vk_fetch_users(missing_ids, token)
        self.stdout.write(f'  VK ответил по {len(users)} из {len(missing_ids)} id.')

        # 4) Создаём/обновляем guest.Client через единую функцию маппинга.
        if dry_run:
            sample_n = min(5, len(missing_ids))
            for vk_id in missing_ids[:sample_n]:
                it = users.get(vk_id) or {}
                fields = _vk_user_to_guest_fields(it, vk_id)
                self.stdout.write(
                    f'  DRY vk={vk_id} → "{fields["first_name"]} {fields["last_name"]}".strip()'
                )
        else:
            created = updated = 0
            for vk_id in missing_ids:
                it = users.get(vk_id) or {}
                fields = _vk_user_to_guest_fields(it, vk_id)
                obj, was_created = GuestClient.objects.update_or_create(
                    vk_id=vk_id, defaults=fields,
                )
                if was_created: created += 1
                else: updated += 1
            self.stdout.write(f'  Создано: {created}, обновлено (legacy пустых): {updated}')

        if dry_run:
            self.stdout.write(self.style.SUCCESS(
                f'DRY-RUN: создал бы/обновил бы {len(missing_ids)} guest.Client, привязал бы conv\'ы.'
            ))
            return

        # 5) Перепривязка conv'ов: для каждого тенанта проставляем vk_guest.
        total_relinked = 0
        for schema, ids in per_tenant_ids.items():
            if not ids:
                continue
            with schema_context(schema):
                from apps.tenant.branch.models import TestimonialConversation
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
            f'DONE. Запрошено в VK: {len(missing_ids)}. Conv\'ов привязано: {total_relinked}.'
        ))
