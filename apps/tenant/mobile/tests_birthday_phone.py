"""
Телефон и RF-сегмент в ручке дней рождения (GET /api/v1/guests/birthdays/).

До 18.09.2026 ручка отдавала `phone`, `segment_emoji` и `segment_name` пустыми
строками — заглушками. Кабинет CheckUp на экране «Дни рождения» их рисует
(колонка телефона — чтобы позвонить и поздравить), и колонка молча пустовала.
Резолв здесь тот же, что в карточке гостя (`GuestDetailAPIView`): телефон с
согласия через ВК (№78) → телефон из последнего отзыва, где гость его указал.

Главный тест здесь — `PhoneLookupIsBatchedTest`: резолв обязан оставаться
пачечным. Список ДР открывают на всю сеть, и «по два запроса на гостя» тут
означает сотни запросов на один экран.

В ORM не ходим: модели подменяются в модулях, откуда их берёт вьюха.
"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tenant.mobile.api import views as V

VP = 'apps.tenant.mobile.api.views.'
ACCESS = 'apps.shared.users.access.'


def _guest(client_id=1, vk_id=777, phone='', phone_source='', birth_date=None):
    client = SimpleNamespace(vk_id=vk_id, first_name='Иван', last_name='Петров',
                             phone=phone, phone_source=phone_source, pk=client_id)
    return SimpleNamespace(
        client=client, client_id=client_id, birth_date=birth_date or date.today(),
        branch_id=1, branch=SimpleNamespace(name='Институтская'),
        created_at=datetime(2026, 1, 1), is_employee=False,
    )


def _call(guests, *, review_rows=(), scores=(), cb_rows=None, query=''):
    """
    Зовёт ручку с подменёнными моделями.

    review_rows — [(client_branch_id, phone)] из отзывов, свежие первыми;
    scores      — объекты GuestRFScore; cb_rows — карта «профиль → гость».
    """
    if cb_rows is None:
        cb_rows = [(10 + g.client_id, g.client_id) for g in guests]

    mock_cb = MagicMock()
    chain = mock_cb.objects.filter.return_value
    chain.select_related.return_value.order_by.return_value.iterator.return_value = iter(guests)
    chain.values.return_value.annotate.return_value = []          # монеты
    chain.values_list.return_value = cb_rows                      # профили → гость

    mock_log = MagicMock()
    mock_log.objects.filter.return_value.values_list.return_value = []

    mock_tm = MagicMock()
    (mock_tm.objects.filter.return_value
        .exclude.return_value
        .order_by.return_value
        .values_list.return_value) = list(review_rows)

    mock_score = MagicMock()
    mock_score.objects.select_related.return_value.filter.return_value = list(scores)

    request = APIRequestFactory().get('/api/v1/guests/birthdays/' + query)
    force_authenticate(request, user=SimpleNamespace(
        is_authenticated=True, is_active=True, is_superuser=True, pk=1, is_staff=True))

    with patch('apps.tenant.branch.models.ClientBranch', mock_cb), \
         patch('apps.tenant.senler.models.AutoBroadcastLog', mock_log), \
         patch(VP + 'TestimonialMessage', mock_tm), \
         patch('apps.tenant.analytics.models.GuestRFScore', mock_score), \
         patch(ACCESS + 'user_allowed_branches', return_value=None), \
         patch(ACCESS + 'current_schema_name', return_value='dev'):
        response = V.GuestBirthdaysAPIView.as_view()(request)
    return response, mock_tm


def _score(client_id, emoji='🏆', name='Чемпионы'):
    return SimpleNamespace(client_id=client_id,
                           segment=SimpleNamespace(emoji=emoji, name=name))


class PhoneResolveTest(SimpleTestCase):
    """Телефон: согласие через ВК → отзыв → пусто."""

    def test_consent_phone_wins(self):
        response, _ = _call([_guest(phone='+79001112233', phone_source='vk')],
                            review_rows=[(11, '+79990000000')])
        row = response.data['birthdays'][0]
        self.assertEqual((row['phone'], row['phone_source']), ('+79001112233', 'vk'))

    def test_phone_source_is_taken_from_the_guest(self):
        """Непроверенный телефон обязан приезжать со своей пометкой, а не как 'vk'."""
        response, _ = _call([_guest(phone='+79001112233', phone_source='vk_unverified')])
        self.assertEqual(response.data['birthdays'][0]['phone_source'], 'vk_unverified')

    def test_review_phone_is_the_fallback(self):
        response, _ = _call([_guest()], review_rows=[(11, '+79990000000')])
        row = response.data['birthdays'][0]
        self.assertEqual((row['phone'], row['phone_source']), ('+79990000000', 'review'))

    def test_freshest_review_phone_wins(self):
        """Строки приходят свежими первыми — берём первую, а не последнюю."""
        response, _ = _call([_guest()],
                            review_rows=[(11, '+79991111111'), (11, '+79992222222')])
        self.assertEqual(response.data['birthdays'][0]['phone'], '+79991111111')

    def test_no_phone_anywhere(self):
        response, _ = _call([_guest()])
        row = response.data['birthdays'][0]
        self.assertEqual((row['phone'], row['phone_source']), ('', ''))

    def test_other_guests_phone_never_leaks(self):
        """Телефон из отзыва привязан к профилю гостя, а не к выборке целиком."""
        guests = [_guest(client_id=1, vk_id=111), _guest(client_id=2, vk_id=222)]
        response, _ = _call(guests, review_rows=[(12, '+79992222222')])
        rows = {r['vk_id']: r['phone'] for r in response.data['birthdays']}
        self.assertEqual(rows['111'], '')
        self.assertEqual(rows['222'], '+79992222222')


class SegmentResolveTest(SimpleTestCase):

    def test_segment_is_returned(self):
        response, _ = _call([_guest()], scores=[_score(1)])
        row = response.data['birthdays'][0]
        self.assertEqual((row['segment_emoji'], row['segment_name']), ('🏆', 'Чемпионы'))

    def test_guest_without_score_gets_empty_strings(self):
        """Ни None, ни отсутствия поля: кабинет ждёт строки."""
        response, _ = _call([_guest()], scores=[])
        row = response.data['birthdays'][0]
        self.assertEqual((row['segment_emoji'], row['segment_name']), ('', ''))


class PhoneLookupIsBatchedTest(SimpleTestCase):
    """Запросов за телефонами — один на выборку, сколько бы ни было гостей."""

    def test_one_query_for_many_guests(self):
        guests = [_guest(client_id=i, vk_id=100 + i) for i in range(1, 6)]
        response, mock_tm = _call(guests)
        self.assertEqual(len(response.data['birthdays']), 5)
        self.assertEqual(mock_tm.objects.filter.call_count, 1)

    def test_no_query_at_all_for_empty_selection(self):
        response, mock_tm = _call([])
        self.assertEqual(response.data['birthdays'], [])
        mock_tm.objects.filter.assert_not_called()


class IncludePastDefaultTest(SimpleTestCase):
    """
    Прошедшие ДР включены ПО УМОЛЧАНИЮ — кабинет CheckUp это угадал наоборот.

    Их экран не передавал параметр, когда прошедшие надо СКРЫТЬ, и они
    приезжали всё равно. Закрепляем поведение обоих концов.
    """

    def _days(self, query):
        yesterday = date.today() - timedelta(days=3)
        response, _ = _call([_guest(birth_date=yesterday)], query=query)
        return [r['days_until'] for r in response.data['birthdays']]

    def test_past_included_when_param_missing(self):
        self.assertEqual(self._days(''), [-3])

    def test_past_included_when_param_empty(self):
        self.assertEqual(self._days('?include_past='), [-3])

    def test_past_hidden_only_by_explicit_zero(self):
        self.assertEqual(self._days('?include_past=0'), [])
