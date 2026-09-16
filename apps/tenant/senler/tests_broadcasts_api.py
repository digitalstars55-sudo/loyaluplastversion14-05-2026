"""
Тесты JSON-API рассылок для внешнего кабинета CheckUp (senler/api/*).

Стратегия патчей
────────────────
Модели senler — ТЕНАНТНЫЕ: в тестовой БД их таблиц нет, поэтому в ORM тут
не ходим вообще. Вьюхи импортируют модели и сервисы на уровне модуля, так
что подменяем их прямо в apps.tenant.senler.api.broadcasts:
  BroadcastDraft / BroadcastSend   — менеджеры-моки;
  resolve_audience                 — пересчёт аудитории;
  effective_branch_ids             — RBAC (None = доступны все точки).

Что проверяем:
  1) предохранитель аудитории (чистые функции guard.py) — таблица значений;
  2) валидацию тела при создании черновика;
  3) обязательность expected_count/confirm и 409 при расхождении аудитории;
  4) аварийные действия истории (cancel на завершённой рассылке → 409);
  5) что КАЖДАЯ вьюха модуля закрыта IsAuthenticated (как у analytics).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tenant.senler.api import broadcasts as b
from apps.tenant.senler.api.guard import audience_changed, tolerance

BROADCASTS = 'apps.tenant.senler.api.broadcasts.'


def _user(username='t', is_superuser=True):
    return SimpleNamespace(
        is_authenticated=True, is_active=True, is_superuser=is_superuser,
        username=username, pk=1, is_staff=True,
    )


def _post(view, path, payload, *, pk=None):
    factory = APIRequestFactory()
    request = factory.post(path, payload, format='json')
    force_authenticate(request, user=_user())
    return view.as_view()(request, pk=pk) if pk is not None else view.as_view()(request)


# ── 1. Предохранитель аудитории ───────────────────────────────────────────────

class AudienceGuardTest(SimpleTestCase):
    """
    ЧП 2026-08-21: «рассылка на 1 ушла 612». Допуск max(5, 10%) в ЛЮБУЮ
    сторону — иначе просим подтвердить заново.
    """

    def test_table(self):
        cases = [
            # (показано, фактически, должно ли блокировать)
            (1,    612,  True),    # тот самый инцидент
            (100,  105,  False),   # дрейф внутри 10%
            (100,  111,  True),    # +11 > допуска 10
            (100,  89,   True),    # −11 тоже блокируем
            (100,  100,  False),
            (1,    5,    False),   # мелкие ячейки: абсолютный минимум 5
            (1,    7,    True),
            (0,    0,    False),
            (0,    6,    True),
            (1000, 1090, False),   # 10% от 1000 = 100
            (1000, 1101, True),
        ]
        for expected, actual, changed in cases:
            with self.subTest(expected=expected, actual=actual):
                self.assertEqual(audience_changed(expected, actual), changed)

    def test_tolerance_values(self):
        self.assertEqual(tolerance(0), 5)
        self.assertEqual(tolerance(10), 5)
        self.assertEqual(tolerance(100), 10)
        self.assertEqual(tolerance(1000), 100)

    def test_garbage_is_treated_as_changed(self):
        self.assertTrue(audience_changed(None, 10))
        self.assertTrue(audience_changed('abc', 10))
        self.assertTrue(audience_changed(10, None))


# ── 2. Валидация создания черновика ───────────────────────────────────────────

class CreateDraftValidationTest(SimpleTestCase):
    """Тело POST /api/v1/broadcasts/ проверяется ДО обращения к БД."""

    def setUp(self):
        patcher_access = patch(BROADCASTS + 'effective_branch_ids', return_value=None)
        patcher_model = patch(BROADCASTS + 'BroadcastDraft')
        self.addCleanup(patcher_access.stop)
        self.addCleanup(patcher_model.stop)
        self.access = patcher_access.start()
        self.model = patcher_model.start()

    def _create(self, payload):
        return _post(b.BroadcastDraftListCreateAPIView, '/api/v1/broadcasts/', payload)

    def test_empty_text(self):
        resp = self._create({'message_text': '   ', 'branch_ids': [1]})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'invalid_payload')
        self.model.objects.create.assert_not_called()

    def test_text_too_long(self):
        resp = self._create({'message_text': 'x' * 4097, 'branch_ids': [1]})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'invalid_payload')

    def test_variants_percent_sum(self):
        resp = self._create({
            'branch_ids': [1],
            'message_text': 'привет',
            'variants': [
                {'percent': 50, 'message_text': 'а'},
                {'percent': 40, 'message_text': 'б'},
            ],
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn('100', resp.data['detail'])

    def test_variant_empty_text(self):
        resp = self._create({
            'branch_ids': [1],
            'variants': [{'percent': 100, 'message_text': '  '}],
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'invalid_payload')

    def test_branch_ids_required(self):
        resp = self._create({'message_text': 'привет'})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'invalid_payload')
        self.model.objects.create.assert_not_called()

    def test_branch_ids_empty_list(self):
        resp = self._create({'message_text': 'привет', 'branch_ids': []})
        self.assertEqual(resp.status_code, 400)

    def test_foreign_branch_forbidden(self):
        self.access.return_value = [7]          # пользователь видит только точку 7
        resp = self._create({'message_text': 'привет', 'branch_ids': [7, 9]})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data['code'], 'branch_forbidden')
        self.model.objects.create.assert_not_called()

    def test_bad_date(self):
        resp = self._create({'message_text': 'привет', 'branch_ids': [1], 'start': '31.12.2026'})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'invalid_payload')


# ── 3. Отправка: expected_count, confirm, guard ───────────────────────────────

def _draft_mock(**over):
    draft = MagicMock()
    draft.pk = 1
    draft.branch_ids = [1]
    draft.status = 'draft'
    draft.message_text = 'привет'
    draft.variants = []
    draft.segment_id = None
    draft.segment = None
    draft.name = ''
    draft.mode = 'restaurant'
    draft.gender_filter = 'all'
    for key, value in over.items():
        setattr(draft, key, value)
    return draft


class SendDraftTest(SimpleTestCase):
    """Отправка без подтверждённой цифры аудитории невозможна."""

    def setUp(self):
        patcher_access = patch(BROADCASTS + 'effective_branch_ids', return_value=None)
        patcher_model = patch(BROADCASTS + 'BroadcastDraft')
        patcher_aud = patch(BROADCASTS + 'resolve_audience')
        for p in (patcher_access, patcher_model, patcher_aud):
            self.addCleanup(p.stop)
        self.access = patcher_access.start()
        self.model = patcher_model.start()
        self.audience = patcher_aud.start()
        self.draft = _draft_mock()
        self.model.objects.filter.return_value.first.return_value = self.draft
        self.audience.return_value = {'total': 100, 'by_branch': []}

    def _send(self, payload):
        return _post(b.BroadcastDraftSendAPIView, '/api/v1/broadcasts/1/send/', payload, pk=1)

    def test_expected_count_required(self):
        resp = self._send({'confirm': True})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'expected_count_required')
        self.audience.assert_not_called()

    def test_confirm_required(self):
        resp = self._send({'expected_count': 100})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'confirm_required')
        self.audience.assert_not_called()

    def test_audience_changed_conflict(self):
        self.audience.return_value = {'total': 612, 'by_branch': []}
        resp = self._send({'expected_count': 1, 'confirm': True})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'audience_changed')
        self.assertEqual(resp.data['expected'], 1)
        self.assertEqual(resp.data['actual'], 612)

    def test_empty_audience(self):
        self.audience.return_value = {'total': 0, 'by_branch': []}
        resp = self._send({'expected_count': 0, 'confirm': True})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'empty_audience')

    def test_already_sent(self):
        self.draft.status = 'sent'
        resp = self._send({'expected_count': 100, 'confirm': True})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'already_sent')

    def test_foreign_draft_is_404(self):
        self.access.return_value = [9]           # черновик по точке 1 — чужой
        resp = self._send({'expected_count': 100, 'confirm': True})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data['code'], 'not_found')

    def test_missing_draft_is_404(self):
        self.model.objects.filter.return_value.first.return_value = None
        resp = self._send({'expected_count': 100, 'confirm': True})
        self.assertEqual(resp.status_code, 404)


# ── 4. История запусков: аварийные действия ───────────────────────────────────

class SendActionsTest(SimpleTestCase):

    def setUp(self):
        patcher_access = patch(BROADCASTS + 'effective_branch_ids', return_value=None)
        patcher_model = patch(BROADCASTS + 'BroadcastSend')
        for p in (patcher_access, patcher_model):
            self.addCleanup(p.stop)
        self.access = patcher_access.start()
        self.model = patcher_model.start()
        self.send = MagicMock()
        self.send.pk = 5
        self.send.status = 'done'
        self.send.sent_count = 10
        self.send.error_message = ''
        self.model.objects.filter.return_value.first.return_value = self.send

    def test_cancel_done_send_is_conflict(self):
        resp = _post(b.BroadcastSendCancelAPIView,
                     '/api/v1/broadcasts/sends/5/cancel/', {'confirm': True}, pk=5)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'not_cancellable')
        self.send.save.assert_not_called()

    def test_cancel_requires_confirm(self):
        self.send.status = 'running'
        resp = _post(b.BroadcastSendCancelAPIView,
                     '/api/v1/broadcasts/sends/5/cancel/', {}, pk=5)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'confirm_required')
        self.send.save.assert_not_called()

    def test_cancel_running_send(self):
        self.send.status = 'running'
        resp = _post(b.BroadcastSendCancelAPIView,
                     '/api/v1/broadcasts/sends/5/cancel/', {'confirm': True}, pk=5)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.send.status, 'cancelled')
        self.assertIn('Отменено через API', self.send.error_message)
        self.send.save.assert_called_once()

    def test_edit_in_vk_requires_done(self):
        self.send.status = 'running'
        with patch(BROADCASTS + 'edit_broadcast_send_in_vk') as edit:
            resp = _post(b.BroadcastSendEditInVKAPIView,
                         '/api/v1/broadcasts/sends/5/edit-in-vk/',
                         {'confirm': True, 'message_text': 'новый текст'}, pk=5)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'not_editable')
        edit.assert_not_called()

    def test_edit_in_vk_calls_service(self):
        with patch(BROADCASTS + 'edit_broadcast_send_in_vk',
                   return_value={'updated': 3, 'skipped': [], 'errors': []}) as edit:
            resp = _post(b.BroadcastSendEditInVKAPIView,
                         '/api/v1/broadcasts/sends/5/edit-in-vk/',
                         {'confirm': True, 'message_text': 'новый текст'}, pk=5)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['updated'], 3)
        edit.assert_called_once_with(self.send, 'новый текст')

    def test_delete_in_vk_marks_cancelled(self):
        with patch(BROADCASTS + 'delete_broadcast_send_in_vk',
                   return_value={'deleted': 4, 'skipped': [], 'errors': []}):
            resp = _post(b.BroadcastSendDeleteInVKAPIView,
                         '/api/v1/broadcasts/sends/5/delete-in-vk/', {'confirm': True}, pk=5)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.send.status, 'cancelled')

    def test_delete_in_vk_keeps_status_on_errors(self):
        with patch(BROADCASTS + 'delete_broadcast_send_in_vk',
                   return_value={'deleted': 0, 'skipped': [], 'errors': ['VK error']}):
            resp = _post(b.BroadcastSendDeleteInVKAPIView,
                         '/api/v1/broadcasts/sends/5/delete-in-vk/', {'confirm': True}, pk=5)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.send.status, 'done')

    def test_foreign_send_is_404(self):
        self.access.return_value = [9]
        self.send.broadcast_id = 3
        self.send.broadcast.branch_id = 1
        resp = _post(b.BroadcastSendCancelAPIView,
                     '/api/v1/broadcasts/sends/5/cancel/', {'confirm': True}, pk=5)
        self.assertEqual(resp.status_code, 404)


# ── 5. Все вьюхи закрыты авторизацией ─────────────────────────────────────────

class BroadcastsApiRequiresAuthTest(SimpleTestCase):
    """
    В settings нет DEFAULT_PERMISSION_CLASSES (= AllowAny). Тест не даёт
    новой вьюхе кабинета родиться открытой — как и у analytics.
    """

    def test_every_view_declares_is_authenticated(self):
        import inspect

        from rest_framework.permissions import IsAuthenticated
        from rest_framework.views import APIView

        checked = 0
        for name, cls in inspect.getmembers(b, inspect.isclass):
            if cls is APIView or not issubclass(cls, APIView) or cls.__module__ != b.__name__:
                continue
            checked += 1
            with self.subTest(view=name):
                self.assertIn(IsAuthenticated, cls.permission_classes)
        self.assertGreaterEqual(checked, 8)

    def test_anonymous_is_rejected(self):
        factory = APIRequestFactory()
        resp = b.BroadcastDraftListCreateAPIView.as_view()(factory.get('/api/v1/broadcasts/'))
        self.assertIn(resp.status_code, (401, 403))
        resp = b.BroadcastSendListAPIView.as_view()(factory.get('/api/v1/broadcasts/sends/'))
        self.assertIn(resp.status_code, (401, 403))
