"""
Бэкфилл затрат на подарки (GiftCostEvent) для ИСТОРИЧЕСКИХ активаций.

Фича «Экономика клиента» (GiftCostEvent) появилась 2026-06-23 — подарки,
активированные ДО этой даты, не имеют записи затрат, из-за чего «Подарки, ₽»
в сводке занижены для периодов, захватывающих более ранние даты.

Команда создаёт недостающие GiftCostEvent для всех уже активированных подарков
(InventoryItem + StoryGiftEntry), беря ТЕКУЩУЮ себестоимость из карточки товара
(cost_price_rub) — исторический снимок недоступен, текущая цена = лучший прокси.

Идемпотентно: сопоставление с существующими событиями по (client_branch, kind,
activated_at) — повторный запуск не создаёт дублей. По умолчанию dry-run;
для записи — флаг --commit. По умолчанию проходит по всем тенантам; конкретный —
через --schema <name>.

    python manage.py backfill_gift_costs            # dry-run, все тенанты
    python manage.py backfill_gift_costs --commit   # запись, все тенанты
    python manage.py backfill_gift_costs --schema asap-tula --commit
"""

from django.core.management.base import BaseCommand
from django_tenants.utils import tenant_context

from apps.shared.clients.models import Company


class Command(BaseCommand):
    help = 'Создаёт недостающие GiftCostEvent для исторических активаций подарков (по текущей себестоимости).'

    def add_arguments(self, parser):
        parser.add_argument('--commit', action='store_true', help='Записать (иначе dry-run).')
        parser.add_argument('--schema', type=str, default=None, help='Только один тенант (schema_name).')

    def handle(self, *args, **opts):
        commit = opts['commit']
        only_schema = opts['schema']

        companies = Company.objects.all()
        if only_schema:
            companies = companies.filter(schema_name=only_schema)

        grand_inv = grand_story = 0
        for company in companies:
            try:
                inv_n, story_n = self._backfill_tenant(company, commit)
            except Exception as e:  # noqa: BLE001 — один битый тенант не должен ронять остальные
                self.stderr.write(f'  {company.schema_name}: ОШИБКА {e!r}')
                continue
            if inv_n or story_n:
                self.stdout.write(f'  {company.schema_name}: inventory +{inv_n}, story +{story_n}')
            grand_inv += inv_n
            grand_story += story_n

        mode = 'ЗАПИСАНО' if commit else 'DRY-RUN (ничего не записано)'
        self.stdout.write(self.style.SUCCESS(
            f'{mode}: всего inventory +{grand_inv}, story +{grand_story}'
        ))

    def _backfill_tenant(self, company, commit) -> tuple[int, int]:
        from apps.tenant.inventory.models import (
            InventoryItem, StoryGiftEntry, GiftCostEvent,
        )
        with tenant_context(company):
            # существующие события — чтобы не плодить дубли (ключ: cb + kind + activated_at)
            existing_inv = set(
                GiftCostEvent.objects.filter(kind=GiftCostEvent.Kind.INVENTORY)
                .values_list('client_branch_id', 'activated_at')
            )
            existing_story = set(
                GiftCostEvent.objects.filter(kind=GiftCostEvent.Kind.STORY)
                .values_list('client_branch_id', 'activated_at')
            )

            to_create = []

            # ── InventoryItem (супер-призы, ДР, покупки — все активированные) ──
            inv = (
                InventoryItem.objects
                .filter(activated_at__isnull=False)
                .select_related('product', 'client_branch__branch')
            )
            inv_n = 0
            for it in inv.iterator():
                key = (it.client_branch_id, it.activated_at)
                if key in existing_inv:
                    continue
                existing_inv.add(key)
                to_create.append(GiftCostEvent(
                    client_branch_id=it.client_branch_id,
                    product=it.product,
                    branch=it.client_branch.branch,
                    kind=GiftCostEvent.Kind.INVENTORY,
                    cost_rub=(getattr(it.product, 'cost_price_rub', None) or 0),
                    activated_at=it.activated_at,
                ))
                inv_n += 1

            # ── StoryGiftEntry (подарки из сториз / сайта / каталога VK) ──
            st = (
                StoryGiftEntry.objects
                .filter(activated_at__isnull=False)
                .select_related('product', 'client_branch__branch', 'activated_branch')
            )
            story_n = 0
            for e in st.iterator():
                key = (e.client_branch_id, e.activated_at)
                if key in existing_story:
                    continue
                existing_story.add(key)
                to_create.append(GiftCostEvent(
                    client_branch_id=e.client_branch_id,
                    product=e.product,
                    branch=e.activated_branch or e.client_branch.branch,
                    kind=GiftCostEvent.Kind.STORY,
                    cost_rub=(getattr(e.product, 'cost_price_rub', None) or 0),
                    activated_at=e.activated_at,
                ))
                story_n += 1

            if commit and to_create:
                GiftCostEvent.objects.bulk_create(to_create, batch_size=500)

            return inv_n, story_n
