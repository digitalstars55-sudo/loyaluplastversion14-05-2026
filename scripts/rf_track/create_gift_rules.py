"""Подарочные RF-правила (G1) + напоминание о сгорании на levone. Идемпотентно."""
from django.utils import timezone
from django_tenants.utils import schema_context

with schema_context('levone'):
    from apps.tenant.analytics.models import RFSegment
    from apps.tenant.senler.models import (
        AutoBroadcastRule, AutoBroadcastType as T, FollowUpCondition,
    )
    from apps.tenant.senler import engine as E

    now = timezone.now()
    seg = {s.code: s for s in RFSegment.objects.all()}
    parent_r2f1 = AutoBroadcastRule.objects.get(
        name='RF · R2F1 Случайные — шаг 1 без подарка (16 дней)')

    specs = [
        dict(
            name='RF 🎁 R2F1 Случайные — подарок G1 (догон через 4 дня)',
            event=T.FOLLOW_UP, delay_days=4, seg_code='R2F1',
            parent=parent_r2f1, condition=FollowUpCondition.NOT_VISITED,
            gift_tier='G1', gift_lifetime_days=7,
            text=('Мы приготовили для вас подарок 🎁 «{подарок}» уже лежит в '
                  '«Моих подарках» — загляните в приложение! Активировать можно '
                  'в кафе по коду дня, осталось {дней_осталось} дн. Адреса: {адреса}'),
            fallback=('Загляните к нам на этой неделе! 🎡 Колесо фортуны ждёт '
                      'вращения, а ваши {баланс} б. — пополнения. {адреса}'),
        ),
        dict(
            name='RF 🎁 R1F1 Засыпающие — подарок G1 (31 день)',
            event=T.NO_VISIT_DAYS, delay_days=31, seg_code='R1F1',
            gift_tier='G1', gift_lifetime_days=10,
            text=('Мы скучаем! 🥺 И поэтому дарим: «{подарок}» уже ждёт вас в '
                  '«Моих подарках». Успейте активировать за {дней_осталось} дн. — '
                  'в кафе, по коду дня. Ждём: {адреса}'),
            fallback=('Мы скучаем! 🥺 Загляните к нам: у вас {баланс} б., '
                      'а вращение колеса фортуны добавит ещё. {адреса}'),
        ),
        dict(
            name='RF 🎁 R0F1 Потерянные — подарок G1 (61 день)',
            event=T.NO_VISIT_DAYS, delay_days=61, seg_code='R0F1',
            gift_tier='G1', gift_lifetime_days=10,
            text=('Целых два месяца без вас 💔 Возвращайтесь — мы приготовили '
                  'подарок: «{подарок}» уже в «Моих подарках», забрать можно '
                  'в течение {дней_осталось} дн. по коду дня в кафе. {адреса}'),
            fallback=('Целых два месяца без вас 💔 Возвращайтесь: ваши {баланс} б. '
                      'на месте, колесо фортуны соскучилось. {адреса}'),
        ),
        dict(
            name='RF ⏳ Напоминание — подарок скоро сгорит (за 2 дня)',
            event=T.RF_GIFT_EXPIRING, delay_days=2, seg_code=None,
            text=('Напоминаем: ваш подарок «{подарок}» сгорит через '
                  '{дней_осталось} дн. 🎁 Успейте активировать его в кафе '
                  'по коду дня. Адреса: {адреса}'),
        ),
    ]

    for sp in specs:
        defaults = dict(
            event=sp['event'],
            delay_days=sp['delay_days'],
            message_text=sp['text'],
            priority=10,
            is_active=False,
            gift_tier=sp.get('gift_tier', ''),
            gift_lifetime_days=sp.get('gift_lifetime_days', 0),
            gift_fallback_text=sp.get('fallback', ''),
        )
        if sp.get('parent'):
            defaults['parent_rule'] = sp['parent']
            defaults['follow_up_condition'] = sp['condition']
        rule, created = AutoBroadcastRule.objects.get_or_create(
            name=sp['name'], defaults=defaults)
        if created and sp.get('seg_code'):
            rule.rf_segments.set([seg[sp['seg_code']]])
        res = E.run_rule(rule, now, dry_run=True)
        print(f'{"СОЗДАНО" if created else "ЕСТЬ"} #{rule.pk} {rule.name} '
              f'→ сегодня: {res.get("would_send", 0)} ({res.get("reason", "")})')
        if not rule.is_active:
            rule.is_active = True
            rule.save(update_fields=['is_active'])
    print('готово; активных правил:',
          AutoBroadcastRule.objects.filter(is_active=True).count())
