"""
Бэкафилл источника подписки (community_source / newsletter_source) для уже
существующих via_app-подписок, у которых источник ещё не проставлен.

Эвристика (согласовано с владельцем): если гость активировал доставку ДО даты
подписки → источник «доставка»; иначе → «кафе». (story-подписки в старых данных
отсутствуют — story-флоу подписку пока не требовал; их проставит сам story-флоу.)

Запуск:
  docker exec web python manage.py backfill_subscription_source            # все тенанты
  docker exec web python manage.py backfill_subscription_source --schema levone
  docker exec web python manage.py backfill_subscription_source --dry-run  # только показать
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django_tenants.utils import get_tenant_model, schema_context


class Command(BaseCommand):
    help = 'Проставляет источник (cafe/delivery) для существующих via_app-подписок.'

    def add_arguments(self, parser):
        parser.add_argument('--schema', help='Только указанный schema_name (иначе все non-public).')
        parser.add_argument('--dry-run', action='store_true', help='Не сохранять, только показать.')

    def handle(self, *args, **opts):
        target = opts.get('schema')
        dry = opts.get('dry_run')

        TenantModel = get_tenant_model()
        schemas = TenantModel.objects.exclude(schema_name='public')
        if target:
            schemas = schemas.filter(schema_name=target)

        for tenant in schemas:
            with schema_context(tenant.schema_name):
                self._backfill_schema(tenant.schema_name, dry)

    def _backfill_schema(self, schema_name: str, dry: bool):
        from apps.tenant.branch.models import ClientVKStatus, SubscriptionSource
        from datetime import timedelta
        from apps.tenant.delivery.models import Delivery

        WINDOW = timedelta(days=1)

        def source_for(cb_id, joined_at):
            # Доставка активирована В ОКНЕ ±1 день от подписки (тот же визит, до ИЛИ после)
            # → delivery; иначе cafe. Окно вместо строгого «до» — ловит типичный флоу:
            # подписался при онбординге → потом ввёл код доставки.
            if joined_at and Delivery.objects.filter(
                activated_by_id=cb_id,
                activated_at__gte=joined_at - WINDOW,
                activated_at__lte=joined_at + WINDOW,
            ).exists():
                return SubscriptionSource.DELIVERY
            return SubscriptionSource.CAFE

        comm = 0
        news = 0
        # Ре-атрибутируем ВСЕ via_app-подписки, КРОМЕ story (её ставит story-флоу).
        # cafe/delivery/null пересчитываем заново (правим прежний строгий гард).
        qs = ClientVKStatus.objects.filter(
            community_via_app=True,
        ).exclude(community_source=SubscriptionSource.STORY) | ClientVKStatus.objects.filter(
            newsletter_via_app=True,
        ).exclude(newsletter_source=SubscriptionSource.STORY)
        for vk in qs.distinct():
            fields = []
            if vk.community_via_app is True and vk.community_source != SubscriptionSource.STORY:
                new_src = source_for(vk.client_id, vk.community_joined_at)
                if new_src != vk.community_source:
                    vk.community_source = new_src
                    fields.append('community_source')
                    comm += 1
            if vk.newsletter_via_app is True and vk.newsletter_source != SubscriptionSource.STORY:
                new_src = source_for(vk.client_id, vk.newsletter_joined_at)
                if new_src != vk.newsletter_source:
                    vk.newsletter_source = new_src
                    fields.append('newsletter_source')
                    news += 1
            if fields and not dry:
                vk.save(update_fields=fields)

        tag = '[dry-run] ' if dry else ''
        self.stdout.write(
            f'{tag}{schema_name}: community_source={comm}, newsletter_source={news}'
        )
