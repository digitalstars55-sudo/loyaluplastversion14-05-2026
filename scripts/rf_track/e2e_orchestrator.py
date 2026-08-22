"""E2E оркестратора RF (ТЗ v1.1 §5) + {баланс}: rollback, ноль отправок."""
from datetime import timedelta, date
from django.db import transaction
from django.utils import timezone
from django_tenants.utils import schema_context, get_tenant_model

results = []
def check(name, cond, detail=''):
    results.append(bool(cond))
    print(('PASS' if cond else 'FAIL'), name, detail)

with schema_context('levone'):
    from apps.tenant.senler import engine as E
    from apps.tenant.senler.models import (
        AutoBroadcastRule, AutoBroadcastLog, AutoBroadcastType as T,
    )
    from apps.shared.config.models import ClientConfig
    from apps.tenant.branch.models import ClientBranch, ClientBranchVisit, CoinTransaction

    now = timezone.now()
    with transaction.atomic():
        # 6 разных реальных гостей без визитов за 30 дней (чистый фон)
        recent_cb = set(ClientBranchVisit.objects.filter(
            visited_at__gte=now - timedelta(days=40)).values_list('client_id', flat=True))
        cbs = list(ClientBranch.objects
                   .filter(client__vk_id__isnull=False, is_employee=False)
                   .exclude(pk__in=recent_cb)
                   .select_related('client')
                   .order_by('pk')[:6])
        assert len(cbs) == 6, 'не набралось 6 чистых гостей'
        g_gap, g_lim14, g_lim30, g_visit, g_bday, g_clean = cbs
        def cand(cb):
            return E.Candidate(client_branch=cb, vk_id=cb.client.vk_id)

        # ── {баланс} ─────────────────────────────────────────────────────
        from django.db.models import Q as _Q, Sum
        agg = CoinTransaction.objects.filter(client=g_clean).aggregate(
            i=Sum('amount', filter=_Q(type='income')),
            e=Sum('amount', filter=_Q(type='expense')))
        expect_balance = (agg['i'] or 0) - (agg['e'] or 0)
        rule = AutoBroadcastRule.objects.create(
            name='E2E', event=T.NO_VISIT_DAYS, is_active=False, delay_days=30,
            message_text='Баланс: {баланс} б.')
        txt = E.render_text(rule, cand(g_clean))
        check('B1 {баланс}', txt == f'Баланс: {expect_balance} б.', repr(txt))

        # ── подготовка нарушений ─────────────────────────────────────────
        def add_log(cb, trig, ago_hours):
            row = AutoBroadcastLog.objects.create(trigger_type=trig, vk_id=cb.client.vk_id)
            AutoBroadcastLog.objects.filter(pk=row.pk).update(
                sent_at=now - timedelta(hours=ago_hours))

        add_log(g_gap, T.NO_VISIT_DAYS, 10)                    # 10 часов назад
        add_log(g_lim14, T.SUBSCRIBED_DAYS, 24 * 5)            # 2 за 14 дней
        add_log(g_lim14, T.NO_VISIT_DAYS, 24 * 10)
        add_log(g_lim30, T.NO_VISIT_DAYS, 24 * 20)             # 3 за 30 дней
        add_log(g_lim30, T.NO_VISIT_DAYS, 24 * 24)
        add_log(g_lim30, T.FOLLOW_UP, 24 * 28)
        v = ClientBranchVisit.objects.create(client=g_visit)   # визит 2 дня назад
        ClientBranchVisit.objects.filter(pk=v.pk).update(
            visited_at=now - timedelta(days=2))
        today = timezone.localdate()
        bd = today + timedelta(days=3)                          # ДР через 3 дня
        ClientBranch.objects.filter(pk=g_bday.pk).update(
            birth_date=bd.replace(year=1990))

        all_cands = [cand(cb) for cb in cbs]

        # ── O1: флаг ВЫКЛЮЧЕН (дефолт) — оркестратор прозрачен ───────────
        out = E._apply_rf_orchestrator(list(all_cands), T.NO_VISIT_DAYS, now)
        check('O1 disabled-transparent', len(out) == 6, f'{len(out)}/6 прошло (флаг выкл)')

        # ── включаем флаг ────────────────────────────────────────────────
        company = get_tenant_model().objects.get(schema_name='levone')
        ClientConfig.objects.filter(company=company).update(rf_orchestrator_enabled=True)
        check('O2 flag-on', E._orchestrator_enabled() is True, '')

        out = E._apply_rf_orchestrator(list(all_cands), T.NO_VISIT_DAYS, now)
        out_vk = {c.vk_id for c in out}
        vk = lambda cb: cb.client.vk_id
        check('O3 72h-gap', vk(g_gap) not in out_vk, 'RF было 10ч назад — отсечён')
        check('O4 limit-14d', vk(g_lim14) not in out_vk, '2 RF за 14 дней — отсечён')
        check('O5 limit-30d', vk(g_lim30) not in out_vk, '3 RF за 30 дней — отсечён')
        check('O6 visit-cooldown', vk(g_visit) not in out_vk, 'визит 2 дня назад — отсечён')
        check('O7 bday-freeze', vk(g_bday) not in out_vk, 'ДР через 3 дня — заморожен')
        check('O8 clean-passes', vk(g_clean) in out_vk, 'чистый гость прошёл')

        # ── O9: не-RF событие оркестратор не трогает ─────────────────────
        out_bd = E._apply_rf_orchestrator(list(all_cands), T.BIRTHDAY, now)
        check('O9 non-rf-untouched', len(out_bd) == 6, 'ДР-событие идёт мимо оркестратора')

        transaction.set_rollback(True)

print('=' * 40)
print('ИТОГ:', 'ALL PASS' if all(results) else 'ЕСТЬ FAIL')
