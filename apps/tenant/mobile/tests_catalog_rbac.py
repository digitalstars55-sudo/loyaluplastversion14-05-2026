"""
Права по точкам у каталога, квестов и акций (контракт 3б.7, м34).

Сотрудник, ограниченный точками, до 18.09.2026 видел и правил каталог, квесты
и акции ВСЕЙ сети: RBAC применялся в аналитике и отзывах, а эти восемь вьюх его
не спрашивали. Здесь проверяем оба конца правила:
  • для НЕограниченного пользователя не изменилось НИЧЕГО (это главное: вьюхи
    живые, на них мобилка и веб);
  • ограниченный видит только свои точки, чужая точка = 404, а запись в
    сетевой каталог ему запрещена (403).

В ORM не ходим: модели подменяются в модулях, откуда их импортируют вьюхи,
`get_object_or_404` — в модуле вьюх.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tenant.mobile.api import views as V

VP = 'apps.tenant.mobile.api.views.'


def _user():
    return SimpleNamespace(is_authenticated=True, is_active=True, is_superuser=False,
                           username='manager', pk=2, is_staff=True)


def _call(view_cls, method, path, *, data=None, **kwargs):
    factory = APIRequestFactory()
    if method == 'get':
        request = factory.get(path)
    else:
        request = getattr(factory, method)(path, data or {}, format='json')
    force_authenticate(request, user=_user())
    return view_cls.as_view()(request, **kwargs)


class CatalogWriteIsNetworkOnlyTest(SimpleTestCase):
    """Каталог и категории — сетевые: ограниченный сотрудник их не пишет."""

    WRITES = (
        (V.ProductCategoryListCreateAPIView, 'post', '/api/v1/catalog/categories/', {}),
        (V.ProductListCreateAPIView, 'post', '/api/v1/catalog/products/', {}),
        (V.ProductCategoryDetailAPIView, 'patch', '/api/v1/catalog/categories/1/', {'pk': 1}),
        (V.ProductCategoryDetailAPIView, 'delete', '/api/v1/catalog/categories/1/', {'pk': 1}),
        (V.ProductDetailAPIView, 'patch', '/api/v1/catalog/products/1/', {'pk': 1}),
        (V.ProductDetailAPIView, 'delete', '/api/v1/catalog/products/1/', {'pk': 1}),
    )

    def test_limited_staff_cannot_write(self):
        for view, method, path, kwargs in self.WRITES:
            with self.subTest(view=view.__name__, method=method):
                with patch(VP + '_branch_limit', return_value=[3]):
                    resp = _call(view, method, path, data={'name': 'x'}, **kwargs)
                self.assertEqual(resp.status_code, 403)
                self.assertEqual(resp.data['code'], 'role_not_allowed')

    def test_unrestricted_staff_is_not_touched(self):
        """У неограниченного гард молчит — дальше работает прежний код."""
        with patch(VP + '_branch_limit', return_value=None), \
             patch(VP + 'get_object_or_404') as get_obj:
            category = SimpleNamespace(pk=1, name='Кофе', ordering=0)
            category.save = MagicMock()
            get_obj.return_value = category
            with patch(VP + '_serialize_category', return_value={'id': 1}):
                resp = _call(V.ProductCategoryDetailAPIView, 'patch',
                             '/api/v1/catalog/categories/1/', data={'name': 'Напитки'}, pk=1)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(category.name, 'Напитки')
        category.save.assert_called_once()


class PromotionsScopeTest(SimpleTestCase):
    """Акции привязаны к точке — ограниченный сотрудник видит и правит свои."""

    def test_list_is_filtered(self):
        qs = MagicMock()
        qs.filter.return_value = []
        promotions = MagicMock()
        promotions.objects.select_related.return_value.order_by.return_value = qs
        with patch('apps.tenant.branch.models.Promotions', promotions), \
             patch(VP + '_branch_limit', return_value=[3]):
            resp = _call(V.PromotionListCreateAPIView, 'get', '/api/v1/branch/promotions/')
        self.assertEqual(resp.status_code, 200)
        qs.filter.assert_called_once_with(branch_id__in=[3])

    def test_list_is_not_filtered_for_unrestricted(self):
        qs = MagicMock()
        qs.__iter__ = lambda self_: iter([])
        promotions = MagicMock()
        promotions.objects.select_related.return_value.order_by.return_value = qs
        with patch('apps.tenant.branch.models.Promotions', promotions), \
             patch(VP + '_branch_limit', return_value=None):
            resp = _call(V.PromotionListCreateAPIView, 'get', '/api/v1/branch/promotions/')
        self.assertEqual(resp.status_code, 200)
        qs.filter.assert_not_called()

    def test_create_on_foreign_branch_is_404(self):
        with patch(VP + '_branch_limit', return_value=[3]):
            resp = _call(V.PromotionListCreateAPIView, 'post', '/api/v1/branch/promotions/',
                         data={'branch_id': 9, 'title': 'x', 'discount': 'y', 'dates': 'z'})
        self.assertEqual((resp.status_code, resp.data['code']), (404, 'not_found'))

    def test_patch_of_foreign_promotion_is_404(self):
        promo = SimpleNamespace(pk=5, branch_id=9, title='t', discount='d', dates='x')
        promo.save = MagicMock()
        with patch(VP + 'get_object_or_404', return_value=promo), \
             patch(VP + '_branch_limit', return_value=[3]):
            resp = _call(V.PromotionDetailAPIView, 'patch', '/api/v1/branch/promotions/5/',
                         data={'title': 'чужая'}, pk=5)
        self.assertEqual((resp.status_code, resp.data['code']), (404, 'not_found'))
        promo.save.assert_not_called()

    def test_delete_of_foreign_promotion_is_404(self):
        promo = SimpleNamespace(pk=5, branch_id=9)
        promo.delete = MagicMock()
        with patch(VP + 'get_object_or_404', return_value=promo), \
             patch(VP + '_branch_limit', return_value=[3]):
            resp = _call(V.PromotionDetailAPIView, 'delete', '/api/v1/branch/promotions/5/', pk=5)
        self.assertEqual((resp.status_code, resp.data['code']), (404, 'not_found'))
        promo.delete.assert_not_called()

    def test_own_promotion_is_editable(self):
        promo = SimpleNamespace(pk=5, branch_id=3, title='t', discount='d', dates='x')
        promo.save = MagicMock()
        with patch(VP + 'get_object_or_404', return_value=promo), \
             patch(VP + '_branch_limit', return_value=[3]), \
             patch(VP + '_serialize_promotion', return_value={'id': 5}):
            resp = _call(V.PromotionDetailAPIView, 'patch', '/api/v1/branch/promotions/5/',
                         data={'title': 'своя'}, pk=5)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(promo.title, 'своя')


class QuestScopeTest(SimpleTestCase):
    """Квест сетевой, но виден сотруднику, если задействует его точку."""

    def test_quest_visible_helper(self):
        quest = MagicMock()
        quest.branch_assignments.filter.return_value.exists.return_value = True
        self.assertTrue(V._quest_visible(quest, None), 'без ограничений видно всё')
        self.assertTrue(V._quest_visible(quest, [3]))
        quest.branch_assignments.filter.return_value.exists.return_value = False
        self.assertFalse(V._quest_visible(quest, [3]))

    def test_list_is_filtered(self):
        qs = MagicMock()
        qs.filter.return_value.distinct.return_value = []
        quest_model = MagicMock()
        quest_model.objects.annotate.return_value.order_by.return_value = qs
        with patch('apps.tenant.quest.models.Quest', quest_model), \
             patch(VP + '_branch_limit', return_value=[3]):
            resp = _call(V.QuestListCreateAPIView, 'get', '/api/v1/quests/')
        self.assertEqual(resp.status_code, 200)
        qs.filter.assert_called_once_with(branch_assignments__branch_id__in=[3])

    def test_create_with_foreign_branch_is_404(self):
        quest_model, quest_branch, branch = MagicMock(), MagicMock(), MagicMock()
        with patch('apps.tenant.quest.models.Quest', quest_model), \
             patch('apps.tenant.quest.models.QuestBranch', quest_branch), \
             patch('apps.tenant.branch.models.Branch', branch), \
             patch(VP + '_branch_limit', return_value=[3]):
            resp = _call(V.QuestListCreateAPIView, 'post', '/api/v1/quests/',
                         data={'branch_ids': [3, 9], 'name': 'Квест'})
        self.assertEqual((resp.status_code, resp.data['code']), (404, 'not_found'))
        quest_model.objects.create.assert_not_called()

    def test_all_branches_means_all_of_mine(self):
        quest_model, quest_branch, branch = MagicMock(), MagicMock(), MagicMock()
        branch.objects.filter.return_value.values_list.return_value = [1, 3, 9]
        created = SimpleNamespace(pk=1, ordering=0, is_active=True)
        quest_model.objects.create.return_value = created
        with patch('apps.tenant.quest.models.Quest', quest_model), \
             patch('apps.tenant.quest.models.QuestBranch', quest_branch), \
             patch('apps.tenant.branch.models.Branch', branch), \
             patch(VP + '_branch_limit', return_value=[3]), \
             patch(VP + '_serialize_quest', return_value={'id': 1}):
            resp = _call(V.QuestListCreateAPIView, 'post', '/api/v1/quests/',
                         data={'all_branches': True, 'name': 'Квест'})
        self.assertEqual(resp.status_code, 201)
        # Квест заведён только на точку 3, «все точки» чужие не захватили.
        self.assertEqual([c.kwargs['branch_id'] for c in quest_branch.objects.create.call_args_list],
                         [3])

    def test_patch_of_foreign_quest_is_404(self):
        quest = MagicMock()
        quest.branch_assignments.filter.return_value.exists.return_value = False
        with patch(VP + 'get_object_or_404', return_value=quest), \
             patch(VP + '_branch_limit', return_value=[3]):
            resp = _call(V.QuestDetailAPIView, 'patch', '/api/v1/quests/5/',
                         data={'name': 'чужой'}, pk=5)
        self.assertEqual((resp.status_code, resp.data['code']), (404, 'not_found'))
        quest.save.assert_not_called()

    def test_delete_of_foreign_quest_is_404(self):
        quest = MagicMock()
        quest.branch_assignments.filter.return_value.exists.return_value = False
        with patch(VP + 'get_object_or_404', return_value=quest), \
             patch(VP + '_branch_limit', return_value=[3]):
            resp = _call(V.QuestDetailAPIView, 'delete', '/api/v1/quests/5/', pk=5)
        self.assertEqual((resp.status_code, resp.data['code']), (404, 'not_found'))
        quest.delete.assert_not_called()
