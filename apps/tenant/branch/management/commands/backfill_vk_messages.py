"""
Полная реконсиляция VK-историй (self-heal). Для КАЖДОГО диалога сканирует
историю до дна (игнорируя текущий курсор), сохраняет все пропущенные реальные
сообщения, выставляет курсор в истинный max.

Чинит «отравленные» курсоры (LU-35): когда курсор стоял на свежей рассылке,
а реальные сообщения гостя НИЖЕ него остались несохранёнными.

Та же логика что у еженедельной reconcile_all_vk_messages_task — команда для
ручного/разового прогона.

Usage:
    docker exec web python manage.py backfill_vk_messages                  # все тенанты
    docker exec web python manage.py backfill_vk_messages --schema asap_orel
    docker exec web python manage.py backfill_vk_messages --max-pages 60   # глубже
"""
from __future__ import annotations

import logging

from django.core.management.base import BaseCommand
from django_tenants.utils import get_tenant_model, schema_context

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Полная реконсиляция VK-историй (чинит отравленные курсоры, тянет пропущенное).'

    def add_arguments(self, parser):
        parser.add_argument('--schema', help='Только указанный schema_name (иначе все non-public).')
        parser.add_argument('--max-pages', type=int, default=None,
            help='Глубина скана в страницах по 200 (по умолчанию MAX_PAGES_FULL=40).')

    def handle(self, *args, **opts):
        from apps.tenant.branch.tasks import reconcile_branch_messages, MAX_PAGES_FULL
        from apps.tenant.senler.models import SenlerConfig

        target_schema = opts.get('schema')
        max_pages = opts.get('max_pages') or MAX_PAGES_FULL

        TenantModel = get_tenant_model()
        qs = TenantModel.objects.exclude(schema_name='public')
        if target_schema:
            qs = qs.filter(schema_name=target_schema)

        total_new = 0
        total_convs = 0
        for tenant in qs:
            with schema_context(tenant.schema_name):
                seen_groups: set[int] = set()
                t_new = t_convs = 0
                for cfg in SenlerConfig.objects.filter(is_active=True).select_related('branch'):
                    if not cfg.vk_community_token or cfg.vk_group_id in seen_groups:
                        continue
                    seen_groups.add(cfg.vk_group_id)
                    res = reconcile_branch_messages(cfg.branch_id, max_pages=max_pages)
                    t_new += res['new_messages']
                    t_convs += res.get('reconciled_convs', 0)
                    for e in res['errors'][:10]:
                        self.stderr.write(f'  [{tenant.schema_name}] {e}')
            total_new += t_new
            total_convs += t_convs
            self.stdout.write(f'[{tenant.schema_name}] reconciled {t_convs} convs, +{t_new} msgs')

        self.stdout.write(self.style.SUCCESS(
            f'DONE. Reconciled {total_convs} convs across tenants, +{total_new} new messages.'
        ))
