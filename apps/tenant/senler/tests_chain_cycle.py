"""Волна 4: цепочки авторассылок — догоняющее от догоняющего можно, круг нельзя."""
from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.tenant.senler.api.auto_broadcasts import creates_cycle


def chain(*ids):
    """chain(1, 2, 3): 3 → родитель 2 → родитель 1."""
    node = None
    for i in ids:
        node = SimpleNamespace(pk=i, parent_rule=node)
    return node


class ChainCycleTest(SimpleTestCase):
    def test_long_chain_ok(self):
        self.assertFalse(creates_cycle(4, chain(1, 2, 3)))

    def test_cycle_detected(self):
        self.assertTrue(creates_cycle(1, chain(1, 2, 3)))
        self.assertTrue(creates_cycle(3, chain(1, 2, 3)))

    def test_too_deep_is_refused(self):
        self.assertTrue(creates_cycle(999, chain(*range(1, 30))))
