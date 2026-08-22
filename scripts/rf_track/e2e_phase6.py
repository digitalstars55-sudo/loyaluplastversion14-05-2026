"""E2E фазы 6 (атрибуция возврата + KPI-сводка): rollback, ноль отправок."""
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from django_tenants.utils import schema_context

results = []
def check(name, cond, detail=''):
    results.append(bool(cond))
    print(('PASS' if cond else 'FAIL'), name, detail)

with schema_context('levone'):
    from apps.tenant.analytics import rfm_campaigns as RC
    from apps.tenant.analytics.models import (
        GuestRFScore, RFMMemberStatus, RFMRewardType,
    )
    from apps.tenant.branch.models import ClientBranch, ClientBranchVisit
    from apps.tenant.catalog.models import Product
    from apps.tenant.inventory.models import RewardCatalogItem

    now = timezone.now()
    with transaction.atomic():
        product = Product.objects.filter(is_archived=False).first()
        item = RewardCatalogItem.objects.create(
            product=product, tier='G1', default_lifetime_days=7)

        recent = set(ClientBranchVisit.objects.filter(
            visited_at__gte=now - timedelta(days=40)).values_list('client_id', flat=True))
        cbs = list(ClientBranch.objects
                   .filter(client__vk_id__isnull=False, is_employee=False,
                           client__is_active=True)
                   .exclude(pk__in=recent)
                   .select_related('client')
                   .order_by('pk')[:20])
        client_ids = list(dict.fromkeys(cb.client_id for cb in cbs))[:10]
        assert len(client_ids) == 10, 'нужно 10 гостей'

        camp = RC.create_campaign_snapshot(
            client_ids=client_ids, mode='restaurant', segment_label='E2E6 · R1F1',
            r_score=1, f_score=1,
            reward_type=RFMRewardType.GIFT, catalog_item=item,
            holdout_percent=20, created_by='e2e6',
        )
        RC.process_campaign(camp.pk)

        # Бэкдейт: начисление «10 дней назад», старт кампании тоже.
        past = now - timedelta(days=10)
        camp.members.filter(status=RFMMemberStatus.ASSIGNED).update(assigned_at=past)
        type(camp).objects.filter(pk=camp.pk).update(started_at=past)

        assigned = list(camp.members.filter(status=RFMMemberStatus.ASSIGNED)
                        .select_related('client_branch'))
        control = list(camp.members.filter(status=RFMMemberStatus.CONTROL))
        assert len(assigned) >= 5 and len(control) >= 1, f'{len(assigned)}/{len(control)}'

        # Возвраты: 2 из основной (визит через 3 дня), 1 из контроля.
        def visit(client_id, days_after):
            cb = ClientBranch.objects.filter(client_id=client_id,
                                             is_employee=False).first()
            v = ClientBranchVisit.objects.create(client=cb)
            ClientBranchVisit.objects.filter(pk=v.pk).update(
                visited_at=past + timedelta(days=days_after))

        # чистим шум: другие свежие визиты этих гостей не создаём — берём как есть,
        # но проверяем ДЕЛЬТУ (returned >= наших синтетических)
        visit(assigned[0].client_id, 3)
        visit(assigned[1].client_id, 5)
        visit(control[0].client_id, 4)
        # возврат ВНЕ окна атрибуции (окно 30д, визит через 40) — не должен засчитаться
        # (эмулируем коротким окном)
        type(camp).objects.filter(pk=camp.pk).update(attribution_window_days=4)

        # улучшение позиции: у assigned[0] хранимый скор R2F1 (>R1F1).
        # segment FK может быть NOT NULL — обновляем существующую строку,
        # а если её нет, создаём с любым сегментом.
        updated_sc = GuestRFScore.objects.filter(
            client_id=assigned[0].client_id).update(r_score=2, f_score=1)
        if not updated_sc:
            from apps.tenant.analytics.models import RFSegment
            GuestRFScore.objects.create(
                client_id=assigned[0].client_id, recency_days=5, frequency=1,
                r_score=2, f_score=1, segment=RFSegment.objects.first())

        res = RC.attribute_returns(camp.pk)
        m0 = camp.members.get(pk=assigned[0].pk)
        m1 = camp.members.get(pk=assigned[1].pk)
        mc = camp.members.get(pk=control[0].pk)

        check('A1 return-in-window', m0.first_return_at is not None
              and m0.segment_after == 'R2F1', f'{m0.first_return_at} {m0.segment_after}')
        check('A2 out-of-window', m1.first_return_at is None,
              'визит на 5-й день при окне 4 дня — не засчитан')
        check('A3 control-return', mc.first_return_at is not None,
              'контроль считается от старта кампании')
        check('A4 summary', res['assigned_returned'] >= 1 and res['control_returned'] >= 1
              and res['assigned_base'] == len(assigned)
              and res['control_base'] == len(control), f'{res}')
        check('A5 improved', res['assigned_improved'] >= 1,
              f"improved={res['assigned_improved']} (R2F1 > R1F1)")
        check('A6 uplift', res['uplift_pp'] is not None, f"uplift={res['uplift_pp']}пп")

        # идемпотентность: повторный вызов не плодит изменений
        res2 = RC.attribute_returns(camp.pk)
        check('A7 idempotent', res2['assigned_returned'] == res['assigned_returned'], '')

        transaction.set_rollback(True)

print('=' * 40)
print('ИТОГ:', 'ALL PASS' if all(results) else 'ЕСТЬ FAIL', f'({sum(results)}/{len(results)})')
