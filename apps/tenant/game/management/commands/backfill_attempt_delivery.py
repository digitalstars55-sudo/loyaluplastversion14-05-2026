"""
Бэкафилл ClientAttempt.delivery=True для СТАРЫХ игр (до миграции флага), которые
были сыграны в активной доставочной сессии. Эвристика: на момент игры у гостя была
активная доставка (Delivery: activated_at <= created_at < expires_at). Это исключает
доставочные игры из метрики «сканирования в кафе» (они не кафе-сканы; доставочный
скан — сама активация кода).

Запуск:
  docker exec web python manage.py backfill_attempt_delivery            # все тенанты
  docker exec web python manage.py backfill_attempt_delivery --schema asap_orel
  docker exec web python manage.py backfill_attempt_delivery --dry-run
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django_tenants.utils import get_tenant_model, schema_context


class Command(BaseCommand):
    help = 'Проставляет ClientAttempt.delivery=True для старых игр в активной доставке.'

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
                self._backfill(tenant.schema_name, dry)

    def _backfill(self, schema_name: str, dry: bool):
        from django.db.models import Exists, OuterRef
        from apps.tenant.game.models import ClientAttempt
        from apps.tenant.delivery.models import Delivery

        qs = ClientAttempt.objects.filter(delivery=False).filter(
            Exists(Delivery.objects.filter(
                activated_by=OuterRef('client'),
                activated_at__lte=OuterRef('created_at'),
                expires_at__gt=OuterRef('created_at'),
            ))
        )
        n = qs.count()
        if n and not dry:
            qs.update(delivery=True)

        tag = '[dry-run] ' if dry else ''
        self.stdout.write(f'{tag}{schema_name}: ClientAttempt.delivery → True: {n}')
