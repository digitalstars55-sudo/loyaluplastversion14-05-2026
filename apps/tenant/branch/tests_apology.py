"""Подарок-извинение за низкую оценку (apology_gift.py), 15.10.2026."""
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from apps.tenant.branch import apology_gift as ag


def cfg(**kw):
    base = dict(apology_gift_enabled=True, apology_gift_max_rating=3, apology_gift_tier='G1',
                apology_gift_text='', rf_reward_max_cost_rub=0, rf_daily_contact_limit=0)
    base.update(kw)
    return SimpleNamespace(**base)


class _QS:
    """Мини-заменитель queryset: filter/select_related/order_by возвращают себя."""
    def __init__(self, first=None, exists=False, values=()):
        self._first, self._exists, self._values = first, exists, list(values)

    def select_related(self, *a): return self
    def filter(self, *a, **k): return self
    def order_by(self, *a): return self
    def values_list(self, *a, **k): return self
    def first(self): return self._first
    def exists(self): return self._exists
    def __getitem__(self, s): return self._values[s]
    def __iter__(self): return iter(self._values)


class ApologyRulesTest(SimpleTestCase):
    def test_threshold_and_switch(self):
        self.assertTrue(ag.should_offer(1, cfg()))
        self.assertTrue(ag.should_offer(3, cfg()))
        self.assertFalse(ag.should_offer(4, cfg()))
        self.assertFalse(ag.should_offer(None, cfg()))
        self.assertFalse(ag.should_offer(1, cfg(apology_gift_enabled=False)))
        self.assertFalse(ag.should_offer(1, None))

    def test_text_default_and_broken_template(self):
        t = ag._render(cfg(), name='Анна', gift='Десерт', days=7)
        self.assertIn('Анна', t); self.assertIn('Десерт', t); self.assertIn('7', t)
        broken = ag._render(cfg(apology_gift_text='Привет {who}'), name='Анна', gift='Десерт', days=7)
        self.assertIn('Десерт', broken)

    def test_schedule_only_for_low_rating(self):
        with mock.patch.object(ag, '_config', return_value=cfg()), \
                mock.patch('django.db.transaction.on_commit') as oc:
            ag.schedule(SimpleNamespace(pk=1, rating=5))
            self.assertFalse(oc.called)
            ag.schedule(SimpleNamespace(pk=2, rating=2))
            self.assertTrue(oc.called)

    def test_schedule_never_raises(self):
        with mock.patch.object(ag, '_config', side_effect=RuntimeError('boom')):
            ag.schedule(SimpleNamespace(pk=1, rating=1))


class ApologyGrantTest(SimpleTestCase):
    def setUp(self):
        self.conv = SimpleNamespace(vk_sender_id='42', branch='B')
        self.msg = SimpleNamespace(pk=7, rating=2, conversation=self.conv, branch='B')
        self.cb = SimpleNamespace(client_id=5, client=SimpleNamespace(first_name='Анна'))
        self.item = SimpleNamespace(product=SimpleNamespace(name='Десерт'), default_lifetime_days=7,
                                    effective_cost_price=100)

    def run_grant(self, *, cb=True, cooldown=False, item=True, issued=True, vk_error=None, config=None):
        issued_obj = SimpleNamespace(pk=99) if issued else None
        patches = [
            mock.patch.object(ag, '_config', return_value=config or cfg()),
            mock.patch('apps.tenant.branch.models.TestimonialMessage.objects', _QS(first=self.msg)),
            mock.patch('apps.tenant.branch.models.ClientBranch.objects', _QS(first=self.cb if cb else None)),
            mock.patch('apps.tenant.inventory.models.InventoryItem.objects', _QS(exists=cooldown)),
            mock.patch('apps.tenant.inventory.reward_catalog.pick_reward', return_value=self.item if item else None),
            mock.patch('apps.tenant.inventory.reward_catalog.issue_to_guest', return_value=issued_obj),
            mock.patch('apps.tenant.branch.api.services.send_vk_reply',
                       side_effect=vk_error) ,
        ]
        for p in patches:
            p.start()
        self.addCleanup(mock.patch.stopall)
        import apps.tenant.branch.api.services as svc
        result = ag.grant(7)
        return result, svc.send_vk_reply

    def test_happy_path_gift_and_message(self):
        (issued, reason), send = self.run_grant()
        self.assertEqual(reason, '')
        self.assertEqual(issued.pk, 99)
        text = send.call_args.args[1]
        self.assertIn('Анна', text); self.assertIn('Десерт', text)
        self.assertFalse(send.call_args.kwargs['mark_replied'])

    def test_guest_not_on_branch(self):
        (issued, reason), send = self.run_grant(cb=False)
        self.assertEqual(reason, 'no_guest'); self.assertFalse(send.called)

    def test_cooldown(self):
        (issued, reason), send = self.run_grant(cooldown=True)
        self.assertEqual(reason, 'cooldown'); self.assertFalse(send.called)

    def test_no_gift_in_catalog(self):
        (issued, reason), _ = self.run_grant(item=False)
        self.assertEqual(reason, 'no_gift')

    def test_vk_failure_keeps_gift(self):
        (issued, reason), _ = self.run_grant(vk_error=ValueError('no token'))
        self.assertEqual(reason, ''); self.assertEqual(issued.pk, 99)

    def test_switched_off(self):
        (issued, reason), _ = self.run_grant(config=cfg(apology_gift_enabled=False))
        self.assertEqual(reason, 'off')
