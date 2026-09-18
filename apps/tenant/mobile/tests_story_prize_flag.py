"""
Признак «приз сториз» в каталоге (★18 ревью CheckUp, №19 + №23).

Кабинет CheckUp управляет пулом призов «игры через сториз» через каталог
товаров, поэтому `is_story_prize` должен и отдаваться, и приниматься — иначе
включённая игра остаётся без подарков, а сотрудник не понимает почему.

Модели тенантные, в тестовую БД не ходим: сериализация проверяется на фейковом
товаре, а наличие поля в записи POST/PATCH — по исходнику вьюх (в этом модуле
разбор лежит inline, отдельной функции для него нет).
"""
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

from django.test import SimpleTestCase

from apps.tenant.mobile.api import views as V


def _product(**over):
    values = dict(pk=5, name='Кофе', description='', emoji='', price=0,
                  is_super_prize=False, is_birthday_prize=False, is_story_prize=True,
                  image=None, created_at=None, updated_at=None)
    values.update(over)
    product = SimpleNamespace(**values)
    product.branch_assignments = MagicMock()
    product.branch_assignments.all.return_value = []
    product.created_at = SimpleNamespace(isoformat=lambda: '2026-09-18T00:00:00')
    product.updated_at = SimpleNamespace(isoformat=lambda: '2026-09-18T00:00:00')
    return product


class StoryPrizeFlagTest(SimpleTestCase):

    def test_serializer_returns_the_flag(self):
        data = V._serialize_product(_product())
        self.assertIn('is_story_prize', data)
        self.assertTrue(data['is_story_prize'])

    def test_flag_is_off_by_default_in_output(self):
        self.assertFalse(V._serialize_product(_product(is_story_prize=False))['is_story_prize'])

    def test_create_accepts_the_flag(self):
        source = inspect.getsource(V.ProductListCreateAPIView.post)
        self.assertIn('is_story_prize', source)
        # Тем же разбором, что у соседних признаков призов.
        self.assertIn("str(d.get('is_story_prize', '')).lower() in ('1', 'true', 'yes')", source)

    def test_update_accepts_the_flag(self):
        source = inspect.getsource(V.ProductDetailAPIView.patch)
        self.assertIn("if 'is_story_prize' in d:", source)
