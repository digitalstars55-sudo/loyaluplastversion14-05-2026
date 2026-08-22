"""Фаза 1: 5 RF-правил (M0, без подарков) на levone. РЕАЛЬНОЕ создание, идемпотентно."""
from django.utils import timezone
from django_tenants.utils import schema_context, get_tenant_model

RULES = [
    dict(
        name='RF · R3F1 Новички — напомнить механику (7 дней)',
        delay_days=7, seg_code='R3F1', priority=10,
        text=('Здравствуйте! 🎡 На прошлой неделе вы заглядывали к нам — спасибо! '
              'Напоминаем: каждый визит — это вращение колеса фортуны и баллы. '
              'У вас уже {баланс} б., их можно обменять на подарки в приложении. '
              'Ждём снова: {адреса}'),
    ),
    dict(
        name='RF · R3F2 Растущие — закрепить привычку (10 дней)',
        delay_days=10, seg_code='R3F2', priority=10,
        text=('Отличный темп! 🚀 Вы копите баллы быстрее многих: на счету уже {баланс} б. '
              'Загляните на этой неделе — новое вращение колеса приблизит к подарку '
              'из магазина баллов. Мы тут: {адреса}'),
    ),
    dict(
        name='RF · R3F3 Суперфанаты — мягкое касание (12 дней)',
        delay_days=12, seg_code='R3F3', priority=10,
        text=('Спасибо, что вы с нами так часто! 💜 На вашем счету {баланс} б. — '
              'загляните в магазин подарков в приложении: возможно, пора себя '
              'порадовать. До встречи: {адреса}'),
    ),
    dict(
        name='RF · R2F1 Случайные — шаг 1 без подарка (16 дней)',
        delay_days=16, seg_code='R2F1', priority=10,
        text=('Давно не виделись! 😊 Ваши {баланс} б. никуда не делись — они ждут '
              'в приложении. Заходите: отсканируйте QR за столом, крутаните колесо '
              'фортуны и пополните копилку. Адреса: {адреса}'),
    ),
    dict(
        name='RF · R2F2 Лояльные — шаг 1 без подарка (17 дней)',
        delay_days=17, seg_code='R2F2', priority=10,
        text=('Кажется, вам пора сделать паузу у нас ☕ Вы наш частый гость, и мы '
              'это ценим: на счету {баланс} б. Новый визит — новое вращение колеса '
              'и ещё баллы к подаркам. Ждём: {адреса}'),
    ),
]

with schema_context('levone'):
    from apps.tenant.analytics.models import RFSegment
    from apps.tenant.senler.models import AutoBroadcastRule, AutoBroadcastType as T
    from apps.tenant.senler import engine as E
    from apps.shared.config.models import ClientConfig

    print('Сегменты levone:', list(RFSegment.objects.values_list('code', 'name')))

    now = timezone.now()
    created = []
    for spec in RULES:
        seg = RFSegment.objects.filter(code=spec['seg_code']).first()
        if seg is None:
            print('!! нет сегмента', spec['seg_code'], '— правило пропущено')
            continue
        rule, was_created = AutoBroadcastRule.objects.get_or_create(
            name=spec['name'],
            defaults=dict(
                event=T.NO_VISIT_DAYS,
                delay_days=spec['delay_days'],
                message_text=spec['text'],
                priority=spec['priority'],
                is_active=False,   # включим после предпросмотра ниже
            ),
        )
        if was_created:
            rule.rf_segments.set([seg])
        created.append(rule)
        res = E.run_rule(rule, now, dry_run=True)
        print(f'{"СОЗДАНО" if was_created else "УЖЕ ЕСТЬ"} #{rule.pk} {rule.name}'
              f' → сегодня получателей: {res.get("would_send", 0)} ({res.get("reason", "")})')

    # Антиспам-оркестратор: включаем флаг сети (резолв Company по schema_name —
    # НЕ через connection.tenant, он FakeTenant без pk).
    company = get_tenant_model().objects.get(schema_name='levone')
    ClientConfig.objects.filter(company=company).update(rf_orchestrator_enabled=True)
    print('rf_orchestrator_enabled:', E._orchestrator_enabled())

    for rule in created:
        if not rule.is_active:
            rule.is_active = True
            rule.save(update_fields=['is_active'])
    print('Активировано правил:', sum(1 for r in created if r.is_active), 'из', len(created))
