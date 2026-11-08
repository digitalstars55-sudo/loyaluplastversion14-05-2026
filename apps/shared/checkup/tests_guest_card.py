"""
Карточка звонящего CheckUp (08.11.2026): гость сети по номеру.

Как в tests_scans_export.py: БД не трогаем. Разбор номера, день рождения, выбор
гостя и сборка ответа — чистые функции; цепочка проверок ручки — RequestFactory
с подменой того, что ходит в тенантную схему.

Что заперто:
  • порядок отказов: внешний адрес → 403, выключено → 503, секрет → 401,
    сеть не в белом списке → 403, кривой номер → 400;
  • секрет СВОЙ: секрет выгрузки сканов и обмена эту ручку не открывают (контракт 4.15);
  • пустой белый список = ни одной сети;
  • в ответе нет телефона, VK ID и полной фамилии, у дня рождения нет года;
  • баллы — сумма по точкам, по точке тоже видно;
  • из дублей одного номера берётся гость со свежей карточкой, остальные считаются.
"""
import datetime
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock

from django.test import RequestFactory, SimpleTestCase, override_settings

from . import guest_card as GC

URL = '/api/v1/internal/checkup/guest-card/'
SECRET = 'test-card-secret'
TODAY = datetime.date(2026, 11, 8)


def _moment(day):
    return datetime.datetime(2026, 11, day, 12, 0, tzinfo=datetime.timezone.utc)


def _profile(pk, guest=500, branch_name='Levone | Ленина', branch_pk=3, birth=None, employee=False, day=1):
    guest_obj = SimpleNamespace(pk=guest, first_name='Анна', last_name='Смирнова', phone='+79100004118',
                                vk_id=123456)
    branch = SimpleNamespace(branch_id=branch_pk, name=branch_name)
    return SimpleNamespace(pk=pk, client_id=guest, client=guest_obj, branch=branch, branch_id=branch_pk,
                           birth_date=birth, is_employee=employee, created_at=_moment(day))


class PhoneTest(SimpleTestCase):

    def test_forms_of_one_number(self):
        for raw in ('+7 (910) 000-41-18', '89100004118', '9100004118', '79100004118'):
            with self.subTest(raw=raw):
                self.assertEqual(GC.phone_variants(raw), ['+79100004118', '79100004118'])

    def test_not_a_russian_number(self):
        for raw in ('', '12345', '+1 202 555 0100', '+7910000411', None):
            with self.subTest(raw=raw):
                self.assertEqual(GC.phone_variants(raw), [])


class BirthdayTest(SimpleTestCase):

    def test_month_day_without_year(self):
        self.assertEqual(GC.birth_month_day(datetime.date(1990, 11, 20)), '11-20')
        self.assertEqual(GC.birth_month_day(datetime.date(1992, 2, 29)), '02-29')
        self.assertIsNone(GC.birth_month_day(None))


class PickGuestTest(SimpleTestCase):

    def test_fresher_guest_wins_and_others_counted(self):
        profiles = [_profile(1, guest=500, day=1), _profile(2, guest=500, day=2), _profile(3, guest=777, day=5)]
        self.assertEqual(GC.pick_guest(profiles), (777, 1))

    def test_nobody(self):
        self.assertEqual(GC.pick_guest([]), (None, 0))


class AssembleTest(SimpleTestCase):

    def test_shape_without_personal_data(self):
        profiles = [_profile(1, birth=datetime.date(1990, 11, 20)),
                    _profile(2, branch_name='Levone | Набережная', branch_pk=4)]
        out = GC.assemble(profiles[0].client, profiles, {1: 120, 2: 380}, [TODAY, TODAY],
                          {'date': '2026-11-01', 'branch': 'Levone | Ленина'},
                          [{'title': 'Ролл в подарок'}, {'title': 'Кофе'}, {'title': 'Десерт'}, {'title': 'Ещё'}],
                          0)
        self.assertTrue(out['found'])
        self.assertEqual(out['name'], 'Анна')
        self.assertEqual(out['last_initial'], 'С.')
        self.assertEqual(out['points'], 500)
        self.assertEqual(out['coins_by_branch'][0], {'branch_id': 4, 'name': 'Levone | Набережная', 'coins': 380})
        self.assertEqual(out['gifts_waiting'], ['Ролл в подарок', 'Кофе', 'Десерт'])
        self.assertEqual(out['gifts_waiting_count'], 4)
        self.assertEqual(out['birth_date'], '11-20')
        текст = str(out)
        for лишнее in ('9100004118', '123456', 'Смирнова', '1990'):
            self.assertNotIn(лишнее, текст)

    def test_employee_from_any_point(self):
        profiles = [_profile(1), _profile(2, employee=True)]
        out = GC.assemble(profiles[0].client, profiles, {}, [], None, [], 2)
        self.assertTrue(out['is_employee'])
        self.assertEqual((out['points'], out['visits'], out['same_phone']), (0, 0, 2))


class ParseQueryTest(SimpleTestCase):

    def test_ok_and_bad(self):
        self.assertEqual(GC.parse_query({'tenant_schema': 'LevOne', 'phone': '8 910 000 41 18'}),
                         {'tenant_schema': 'levone', 'phones': ['+79100004118', '79100004118']})
        for params in ({'tenant_schema': 'levone', 'phone': '123'}, {'tenant_schema': 'public', 'phone': '89100004118'},
                       {'phone': '89100004118'}):
            with self.subTest(params=params), self.assertRaises(GC.ExportError) as cm:
                GC.parse_query(params)
            self.assertEqual(cm.exception.status, 400)


@override_settings(CHECKUP_GUEST_CARD_SECRET=SECRET, CHECKUP_GUEST_CARD_TENANTS=['levone'],
                   CHECKUP_SCANS_EXPORT_SECRET='scans-secret', CHECKUP_TOKEN_EXCHANGE_SECRET='exchange-secret',
                   TIME_ZONE='Europe/Moscow')
class ViewTest(SimpleTestCase):

    def _get(self, params=None, secret=SECRET, **extra):
        headers = {}
        if secret is not None:
            headers['HTTP_X_LOYALUP_GUEST_SECRET'] = secret
        headers.update(extra)
        request = RequestFactory().get(URL, params if params is not None else
                                       {'tenant_schema': 'levone', 'phone': '+79100004118'}, **headers)
        return GC.GuestCardView.as_view()(request)

    def test_external_address_is_403(self):
        self.assertEqual(self._get(REMOTE_ADDR='8.8.8.8').status_code, 403)
        self.assertEqual(self._get(HTTP_X_FORWARDED_FOR='8.8.8.8').status_code, 403)

    @override_settings(CHECKUP_GUEST_CARD_SECRET='')
    def test_empty_secret_is_503_even_with_other_secrets(self):
        resp = self._get(secret='scans-secret')
        self.assertEqual(resp.status_code, 503)
        self.assertIn(b'card_disabled', resp.content)

    def test_wrong_or_foreign_secret_is_401(self):
        for secret in ('nope', 'scans-secret', 'exchange-secret', None):
            with self.subTest(secret=secret):
                self.assertEqual(self._get(secret=secret).status_code, 401)

    @override_settings(CHECKUP_GUEST_CARD_TENANTS=[])
    def test_empty_allowlist_means_no_tenant(self):
        resp = self._get()
        self.assertEqual(resp.status_code, 403)
        self.assertIn(b'tenant_not_allowed', resp.content)

    def test_other_tenant_is_403(self):
        self.assertEqual(self._get({'tenant_schema': 'asap_orel', 'phone': '+79100004118'}).status_code, 403)

    def test_bad_phone_is_400(self):
        self.assertEqual(self._get({'tenant_schema': 'levone', 'phone': '12'}).status_code, 400)

    def test_ok_and_phone_not_logged(self):
        with mock.patch.object(GC, 'resolve_tenant'), \
             mock.patch.object(GC, 'schema_context', return_value=nullcontext()) as ctx, \
             mock.patch.object(GC, 'build_card', return_value={'found': False}) as build, \
             self.assertLogs(GC.log.name, level='INFO') as logs:
            resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.content), {'found': False})
        ctx.assert_called_once_with('levone')
        self.assertEqual(build.call_args.args[0]['phones'][0], '+79100004118')
        self.assertNotIn('9100004118', ' '.join(logs.output))


class FileFallbackTest(SimpleTestCase):
    """Секрет и сети из файлов рядом с .env.prod, когда в окружении их нет."""

    def test_files_used_when_settings_empty(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.env').mkdir()
            (Path(d) / '.env' / 'checkup_guest_card.secret').write_text('file-secret\n')
            (Path(d) / '.env' / 'checkup_guest_card.tenants').write_text('levone, ASAP_orel\n')
            with override_settings(BASE_DIR=Path(d), CHECKUP_GUEST_CARD_SECRET='', CHECKUP_GUEST_CARD_TENANTS=[]):
                self.assertTrue(GC.secret_matches('file-secret'))
                self.assertEqual(GC.allowed_tenants(), ['levone', 'asap_orel'])
            with override_settings(BASE_DIR=Path(d), CHECKUP_GUEST_CARD_SECRET='env-secret'):
                self.assertFalse(GC.secret_matches('file-secret'))

    def test_no_files_means_disabled(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d, \
                override_settings(BASE_DIR=Path(d), CHECKUP_GUEST_CARD_SECRET='', CHECKUP_GUEST_CARD_TENANTS=[]):
            self.assertFalse(GC.card_enabled())
            self.assertEqual(GC.allowed_tenants(), [])
