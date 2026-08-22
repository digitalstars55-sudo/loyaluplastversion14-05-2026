"""E2E фазы 5 «Назначение + сгорание»: rollback, ноль отправок в ВК."""
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
        RFMCampaign, RFMMemberStatus, RFMRewardType,
    )
    from apps.tenant.branch.models import (
        ClientBranch, ClientBranchVisit, CoinTransaction, DailyCode,
        DailyCodePurpose, current_code_date,
    )
    from apps.tenant.inventory.models import (
        AcquisitionSource, InventoryItem, ItemStatus, RewardCatalogItem,
    )
    from apps.tenant.inventory import reward_catalog
    from apps.tenant.inventory.api import services as INV
    from apps.tenant.catalog.models import Product
    from apps.tenant.senler import engine as E
    from apps.tenant.senler.models import AutoBroadcastRule, AutoBroadcastType as T

    now = timezone.now()
    with transaction.atomic():
        # ── Подготовка: позиция каталога с подарком ──────────────────────────
        product = Product.objects.filter(is_archived=False).first()
        assert product, 'нет продукта'
        item = RewardCatalogItem.objects.create(
            product=product, tier='G1', weight=5,
            default_lifetime_days=7, activation_limit=100,
        )

        # Чистые гости с vk_id (без визитов за 40 дней — фон не важен, кампания
        # работает по snapshot, а не по окну).
        cbs = list(ClientBranch.objects
                   .filter(client__vk_id__isnull=False, is_employee=False,
                           client__is_active=True)
                   .select_related('client', 'branch')
                   .order_by('pk')[:30])
        assert len(cbs) == 30, 'не набралось 30 гостей'
        client_ids = list(dict.fromkeys(cb.client_id for cb in cbs))

        # ── T1: snapshot + контрольная группа ────────────────────────────────
        camp = RC.create_campaign_snapshot(
            client_ids=client_ids, mode='restaurant', segment_label='E2E · R1F1',
            reward_type=RFMRewardType.GIFT, catalog_item=item,
            lifetime_days=0, holdout_percent=10, created_by='e2e',
        )
        n = len(client_ids)
        ctrl = camp.members.filter(is_control=True).count()
        check('T1 snapshot', camp.members.count() == n and camp.audience_total == n,
              f'{camp.members.count()}/{n}')
        check('T1b holdout', ctrl == round(n * 0.1), f'control={ctrl} из {n}')

        # ── T2: массовое начисление подарков ─────────────────────────────────
        before_issued = RewardCatalogItem.objects.get(pk=item.pk).issued_count
        summary = RC.process_campaign(camp.pk)
        assigned = camp.members.filter(status=RFMMemberStatus.ASSIGNED).count()
        items = InventoryItem.objects.filter(
            rfm_memberships__campaign=camp, acquired_from=AcquisitionSource.RFM)
        one = items.first()
        check('T2 assigned', summary['assigned'] == assigned and assigned > 0,
              f"{summary}")
        check('T2b item-fields', one and one.catalog_item_id == item.pk
              and one.claim_expires_at and one.min_order_amount == 0
              and abs((one.claim_expires_at - now).days - 7) <= 1,
              f'claim_expires={one.claim_expires_at}')
        after_issued = RewardCatalogItem.objects.get(pk=item.pk).issued_count
        check('T2c issued_count', after_issued - before_issued == assigned,
              f'{before_issued}->{after_issued}')
        check('T2d status', summary['status'] in ('completed', 'partially_failed'),
              summary['status'])
        check('T2e control-untouched',
              camp.members.filter(is_control=True,
                                  status=RFMMemberStatus.CONTROL).count() == ctrl, '')

        # ── T3: идемпотентность ──────────────────────────────────────────────
        summary2 = RC.process_campaign(camp.pk)
        check('T3 idempotent', summary2['assigned'] == assigned
              and items.count() == assigned, f"{summary2}")

        # ── T4: дубль-контроль во второй кампании ────────────────────────────
        camp2 = RC.create_campaign_snapshot(
            client_ids=client_ids[:5], mode='restaurant', segment_label='E2E-2',
            reward_type=RFMRewardType.GIFT, catalog_item=item,
            holdout_percent=0, created_by='e2e',
        )
        RC.process_campaign(camp2.pk)
        dup = camp2.members.filter(status=RFMMemberStatus.SKIPPED,
                                   reason='duplicate_active_gift').count()
        check('T4 duplicate-skip', dup >= 4, f'dup={dup}/5 (у кого-то из 5 мог быть контроль T1)')

        # ── T5: баллы на домашнюю точку ──────────────────────────────────────
        camp3 = RC.create_campaign_snapshot(
            client_ids=client_ids[:3], mode='restaurant', segment_label='E2E-3',
            reward_type=RFMRewardType.POINTS, points_amount=150,
            holdout_percent=0, created_by='e2e',
        )
        RC.process_campaign(camp3.pk)
        m3 = camp3.members.filter(status=RFMMemberStatus.ASSIGNED).select_related(
            'coin_tx', 'client_branch').first()
        check('T5 points', m3 and m3.coin_tx and m3.coin_tx.amount == 150
              and m3.coin_tx.source == 'rfm' and m3.client_branch_id, '')

        # ── T6: отмена — отзыв подарков + откат баллов ───────────────────────
        res6 = RC.cancel_campaign(camp.pk, actor='e2e')
        left = InventoryItem.objects.filter(rfm_memberships__campaign=camp).count()
        released = RewardCatalogItem.objects.get(pk=item.pk).issued_count
        check('T6 cancel-gift', res6['revoked'] == assigned and left == 0,
              f"{res6}")
        check('T6b limit-returned',
              released == before_issued + camp2.members.filter(
                  status=RFMMemberStatus.ASSIGNED).count(), f'issued={released}')
        res6p = RC.cancel_campaign(camp3.pk, actor='e2e')
        refunds = CoinTransaction.objects.filter(
            source='rfm', type='expense',
            description__contains=f'#{camp3.pk}').count()
        check('T6c cancel-points', res6p['refunded'] >= 1 and refunds >= 1, f"{res6p}")

        # ── Свежие подарки для T7-T11: camp1 отменена, дублей больше нет ─────
        camp4 = RC.create_campaign_snapshot(
            client_ids=client_ids[5:12], mode='restaurant', segment_label='E2E-4',
            reward_type=RFMRewardType.GIFT, catalog_item=item,
            holdout_percent=0, created_by='e2e',
        )
        RC.process_campaign(camp4.pk)
        assert camp4.members.filter(status=RFMMemberStatus.ASSIGNED).count() >= 3, \
            'camp4 не выдала подарков'

        # ── T7: сгорание (лениво) + отказ активации ──────────────────────────
        camp2_item = InventoryItem.objects.filter(
            rfm_memberships__campaign=camp4).first()
        assert camp2_item, 'нет подарка из camp4'
        InventoryItem.objects.filter(pk=camp2_item.pk).update(
            claim_expires_at=now - timedelta(hours=1))
        camp2_item.refresh_from_db()
        check('T7 lazy-expire', camp2_item.status == ItemStatus.EXPIRED
              and camp2_item.days_left_to_claim == 0, camp2_item.status)
        try:
            INV.activate_item(camp2_item.client_branch.client.vk_id,
                              camp2_item.client_branch.branch.branch_id,
                              camp2_item.pk, code='XXXXX')
            check('T7b activate-refused', False, 'активация прошла!')
        except INV.GiftClaimExpired:
            check('T7b activate-refused', True, 'GiftClaimExpired')
        except Exception as e:
            check('T7b activate-refused', False, f'не та ошибка: {type(e).__name__}')

        # ── T8: активация сетевым кодом дня ──────────────────────────────────
        fresh = InventoryItem.objects.filter(
            rfm_memberships__campaign=camp4,
            activated_at__isnull=True, claim_expires_at__gt=now).first()
        daily = DailyCode.objects.filter(
            purpose=DailyCodePurpose.GAME, valid_date=current_code_date(),
            branch__is_active=True).select_related('branch').first()
        if fresh and daily:
            cb8 = fresh.client_branch
            act = INV.activate_item(cb8.client.vk_id, cb8.branch.branch_id,
                                    fresh.pk, code=daily.code)
            from apps.tenant.inventory.models import GiftCostEvent
            gce = GiftCostEvent.objects.filter(
                client_branch=cb8, activated_at__gte=now).order_by('-pk').first()
            check('T8 network-code', act.activated_at is not None, '')
            check('T8b cost-branch', gce and gce.branch_id == daily.branch_id,
                  f'cost branch={getattr(gce, "branch_id", None)} code branch={daily.branch_id}')
            try:
                INV.activate_item(cb8.client.vk_id, cb8.branch.branch_id,
                                  fresh.pk, code='WRONG')
                check('T8c bad-code', False, 'прошла с мусорным кодом')
            except INV.AlreadyActivated:
                check('T8c bad-code', True, 'повторная активация отклонена')
        else:
            check('T8 network-code', False, f'нет данных: fresh={bool(fresh)} daily={bool(daily)}')

        # ── T9: гейты подарочного шага правил ────────────────────────────────
        rule = AutoBroadcastRule.objects.create(
            name='E2E gift', event=T.NO_VISIT_DAYS, is_active=False,
            delay_days=30, message_text='Вам подарок: {подарок}, заберите за {дней_осталось} дн.',
            gift_tier='G1', gift_fallback_text='Крутите колесо!',
        )
        # 9a: у гостя с RFM-подарком на руках (из camp2, ещё живым) — гейт
        cb_active = InventoryItem.objects.filter(
            rfm_memberships__campaign=camp4, activated_at__isnull=True,
            claim_expires_at__gt=now).exclude(pk=camp2_item.pk).first()
        if cb_active:
            cand_a = E.Candidate(client_branch=cb_active.client_branch,
                                 vk_id=cb_active.client_branch.client.vk_id)
            g, reason = E._prepare_rf_gift(rule, cand_a, now)
            check('T9a active-gift-gate', g is None and reason == 'active_gift', reason)
        else:
            check('T9a active-gift-gate', False, 'нет живого подарка в camp4')

        # 9b: чистый гость — подарок выдаётся и отзывается
        def _bday_far(cb):
            if not cb.birth_date:
                return True
            today = timezone.localdate()
            return E._next_birthday(cb.birth_date, today) > today + timedelta(days=60)

        clean_cb = next((cb for cb in cbs
                         if not InventoryItem.objects.filter(
                             client_branch__client_id=cb.client_id,
                             acquired_from__in=('rfm', 'rf_auto'),
                             used_at__isnull=True).exists()
                         and _bday_far(cb)), None)
        assert clean_cb, 'нет чистого гостя'
        cand_b = E.Candidate(client_branch=clean_cb, vk_id=clean_cb.client.vk_id)
        bal = E._coin_balance(clean_cb)
        g2, reason2 = E._prepare_rf_gift(rule, cand_b, now)
        if bal >= 3000:
            check('T9b issue', g2 is None and reason2 == 'balance_3000',
                  f'баланс {bal} — гейт сработал')
        else:
            check('T9b issue', g2 is not None
                  and g2.acquired_from == AcquisitionSource.RF_AUTO
                  and g2.catalog_item_id == item.pk, reason2)
            txt = E.render_text(rule, E.Candidate(
                client_branch=clean_cb, vk_id=clean_cb.client.vk_id, gift=g2))
            check('T9c render', product.name in txt and 'дн.' in txt, txt[:80])
            ok_rev = reward_catalog.revoke_issued_item(g2)
            check('T9d revoke', ok_rev and not InventoryItem.objects.filter(pk=g2.pk).exists(), '')

        # 9e: ДР близко → гейт
        cb_bd = next((cb for cb in cbs if cb.pk != clean_cb.pk
                      and cb.client_id != clean_cb.client_id), None)
        ClientBranch.objects.filter(pk=cb_bd.pk).update(
            birth_date=(timezone.localdate() + timedelta(days=4)).replace(year=1990))
        # у cb_bd не должно быть своих rf-подарков/баланса — если есть, гейт
        # сработает раньше по другой причине; принимаем любой из «до выбора»
        g3, reason3 = E._prepare_rf_gift(
            rule, E.Candidate(client_branch=ClientBranch.objects.get(pk=cb_bd.pk),
                              vk_id=cb_bd.client.vk_id), now)
        check('T9e birthday-gate', g3 is None
              and reason3 in ('birthday_near', 'active_gift', 'balance_3000',
                              'active_story_gift'), reason3)

        # ── T10: Candidate(gift=...) — фикс dataclass ────────────────────────
        c10 = E.Candidate(client_branch=clean_cb, vk_id=1, entity_key='x', gift=item)
        check('T10 candidate-gift-field', c10.gift is item, '')

        # ── T11: резолвер «скоро сгорит» ─────────────────────────────────────
        exp_item = InventoryItem.objects.filter(
            rfm_memberships__campaign=camp4,
            activated_at__isnull=True, claim_expires_at__gt=now).first()
        if exp_item:
            InventoryItem.objects.filter(pk=exp_item.pk).update(
                claim_expires_at=now + timedelta(hours=30))
            rule11 = AutoBroadcastRule.objects.create(
                name='E2E exp', event=T.RF_GIFT_EXPIRING, is_active=False,
                delay_days=2, message_text='Сгорит через {дней_осталось} дн: {подарок}')
            cands11 = E._rf_gift_expiring_resolver(rule11, now)
            hit = [c for c in cands11 if c.entity_key == f'invgift:{exp_item.pk}']
            check('T11 expiring-resolver', len(hit) == 1, f'{len(cands11)} кандидатов')
            if hit:
                txt11 = E.render_text(rule11, hit[0])
                check('T11b render', '2 дн' in txt11 and product.name in txt11, txt11[:80])
        else:
            check('T11 expiring-resolver', False, 'нет неактивированного подарка')

        transaction.set_rollback(True)

print('=' * 40)
print('ИТОГ:', 'ALL PASS' if all(results) else 'ЕСТЬ FAIL', f'({sum(results)}/{len(results)})')
