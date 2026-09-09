"""
Tests for analytics features:
  - get_pos_guests_count reads from POSGuestCache
  - fetch_pos_data_all_tenants_task skips tenants without POS config
"""
from datetime import date
from unittest.mock import MagicMock, patch

from django.test import TestCase


class GetPosGuestsCountTest(TestCase):
    """get_pos_guests_count sums POSGuestCache rows for the date range."""

    @patch('apps.tenant.analytics.api.services.POSGuestCache')
    def test_sums_all_branches_when_no_filter(self, MockCache):
        """Returns total across all branches when branch_ids is None."""
        from apps.tenant.analytics.api.services import get_pos_guests_count

        MockCache.objects.filter.return_value.aggregate.return_value = {'total': 150}

        result = get_pos_guests_count(None, date(2025, 1, 1), date(2025, 1, 31))
        self.assertEqual(result, 150)

    @patch('apps.tenant.analytics.api.services.POSGuestCache')
    def test_returns_zero_when_no_cache_data(self, MockCache):
        """Returns 0 when aggregate returns None (no cache rows)."""
        from apps.tenant.analytics.api.services import get_pos_guests_count

        MockCache.objects.filter.return_value.aggregate.return_value = {'total': None}

        result = get_pos_guests_count(None, date(2025, 1, 1), date(2025, 1, 31))
        self.assertEqual(result, 0)

    @patch('apps.tenant.analytics.api.services.POSGuestCache')
    def test_filters_by_branch_ids(self, MockCache):
        """Filters queryset by branch_ids when provided."""
        from apps.tenant.analytics.api.services import get_pos_guests_count

        mock_qs = MagicMock()
        mock_qs.filter.return_value.aggregate.return_value = {'total': 42}
        MockCache.objects.filter.return_value = mock_qs

        result = get_pos_guests_count([1, 2], date(2025, 1, 1), date(2025, 1, 7))
        mock_qs.filter.assert_called_once_with(branch__in=[1, 2])
        self.assertEqual(result, 42)


class FetchPosDataTaskTest(TestCase):
    """fetch_pos_data_all_tenants_task skips tenants without POS config."""

    @patch('apps.tenant.analytics.tasks.get_tenant_model')
    def test_skips_tenant_without_config(self, mock_get_tenant_model):
        """Tenant whose config access raises → skipped, no crash."""
        from apps.tenant.analytics.tasks import fetch_pos_data_all_tenants_task

        tenant = MagicMock()
        tenant.schema_name = 'test_schema'
        # Accessing .config raises
        type(tenant).config = property(
            lambda self: (_ for _ in ()).throw(Exception('no config'))
        )
        mock_get_tenant_model.return_value.objects.exclude.return_value \
            .select_related.return_value = [tenant]

        result = fetch_pos_data_all_tenants_task()
        self.assertEqual(result['tenants'], 0)

    @patch('apps.tenant.analytics.tasks.get_tenant_model')
    def test_skips_tenant_with_pos_none(self, mock_get_tenant_model):
        """Tenants with pos_type=none are skipped without API calls."""
        from apps.tenant.analytics.tasks import fetch_pos_data_all_tenants_task
        from apps.shared.config.models import POSType

        tenant = MagicMock()
        tenant.schema_name = 'test_schema'
        tenant.config.pos_type = POSType.NONE

        mock_get_tenant_model.return_value.objects.exclude.return_value \
            .select_related.return_value = [tenant]

        with patch('apps.tenant.analytics.tasks.sync_get_guests_for_period') as mock_svc:
            fetch_pos_data_all_tenants_task()
            mock_svc.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# Автоотправка ответов ИИ на позитивные отзывы
# ═══════════════════════════════════════════════════════════════════════════
#
# Всё без БД: гварды/расчёт времени/клавиатура — чистые функции, а модели
# подменяются простыми объектами. MagicMock тут НЕЛЬЗЯ для conv/cfg:
# getattr(mock, 'ai_needs_human') вернёт truthy-мок и тест соврёт.

import urllib.parse  # noqa: E402
from datetime import datetime  # noqa: E402

from django.test import SimpleTestCase, override_settings  # noqa: E402
from django.utils import timezone as dj_timezone  # noqa: E402


class _FakeMessages:
    """Мини-queryset: .exclude(...).values_list('text', flat=True)."""

    def __init__(self, texts):
        self._texts = list(texts)

    def exclude(self, **kwargs):
        return self

    def values_list(self, *args, **kwargs):
        return list(self._texts)


class _FakeBranch:
    def __init__(self, yandex='', gis=''):
        self.review_link_yandex = yandex
        self.review_link_2gis = gis


class _FakeConv:
    """Тред-заглушка в состоянии «всё хорошо, можно отправлять»."""

    def __init__(self, **kw):
        self.pk = 1
        self.sentiment = 'POSITIVE'
        self.is_replied = False
        self.ai_draft = 'Спасибо за тёплые слова! Ждём вас снова.'
        self.ai_draft_rejected = False
        self.ai_needs_human = False
        self.ai_comment = 'Гость хвалит кухню и обслуживание.'
        self.vk_sender_id = '12345'
        self.branch_id = None
        self.branch = None
        self.auto_send_status = ''
        self.messages = _FakeMessages(['Очень вкусно, спасибо большое!'])
        for key, value in kw.items():
            setattr(self, key, value)


class _FakeCfg:
    def __init__(self, **kw):
        self.enabled = True
        self.auto_send_enabled = True
        self.auto_send_delay_minutes = 15
        self.auto_send_attach_links = True
        self.auto_send_links_text = 'Будем рады отзыву — кнопки ниже'
        self.auto_send_daily_limit = 50
        self.auto_send_branch_enabled = {}
        self.auto_ack_enabled = True
        self.auto_ack_delay_minutes = 30
        self.auto_ack_text = 'Спасибо большое за обратную связь 🙏 Мы во всём разберёмся и вернёмся с ответом.'
        for key, value in kw.items():
            setattr(self, key, value)


@patch('apps.tenant.analytics.auto_reply._ack_already_sent', return_value=False)
@patch('apps.tenant.analytics.auto_reply._auto_sent_today_count', return_value=0)
class AutoAckPrecheckTest(SimpleTestCase):
    """auto_ack_precheck (автоподтверждение на негатив): по проверке на причину."""

    def _neg(self, **kw):
        base = {'sentiment': 'PARTIALLY_NEGATIVE', 'ai_draft': ''}
        base.update(kw)
        return _FakeConv(**base)

    def test_negative_ok(self, *_):
        from apps.tenant.analytics.auto_reply import auto_ack_precheck
        self.assertEqual(auto_ack_precheck(self._neg(), _FakeCfg()), (True, ''))
        self.assertEqual(auto_ack_precheck(self._neg(sentiment='NEGATIVE'), _FakeCfg()), (True, ''))

    def test_config_off(self, *_):
        from apps.tenant.analytics.auto_reply import auto_ack_precheck
        self.assertEqual(auto_ack_precheck(self._neg(), _FakeCfg(auto_ack_enabled=False)), (False, 'config_off'))

    def test_positive_and_neutral_are_not_acked(self, *_):
        from apps.tenant.analytics.auto_reply import auto_ack_precheck
        self.assertEqual(auto_ack_precheck(self._neg(sentiment='POSITIVE'), _FakeCfg()), (False, 'not_negative'))
        self.assertEqual(auto_ack_precheck(self._neg(sentiment='NEUTRAL'), _FakeCfg()), (False, 'not_negative'))

    def test_manual_reply_wins(self, *_):
        from apps.tenant.analytics.auto_reply import auto_ack_precheck
        self.assertEqual(auto_ack_precheck(self._neg(is_replied=True), _FakeCfg()), (False, 'manual_reply'))

    def test_no_vk_sender(self, *_):
        from apps.tenant.analytics.auto_reply import auto_ack_precheck
        self.assertEqual(auto_ack_precheck(self._neg(vk_sender_id=''), _FakeCfg()), (False, 'no_vk_sender'))

    def test_empty_text(self, *_):
        from apps.tenant.analytics.auto_reply import auto_ack_precheck
        self.assertEqual(auto_ack_precheck(self._neg(), _FakeCfg(auto_ack_text='  ')), (False, 'no_ack_text'))

    def test_branch_disabled(self, *_):
        from apps.tenant.analytics.auto_reply import auto_ack_precheck
        cfg = _FakeCfg(auto_send_branch_enabled={'3': False})
        self.assertEqual(auto_ack_precheck(self._neg(branch_id=3), cfg), (False, 'branch_disabled'))

    def test_already_acked(self, mock_count, mock_acked):
        from apps.tenant.analytics.auto_reply import auto_ack_precheck
        mock_acked.return_value = True
        self.assertEqual(auto_ack_precheck(self._neg(), _FakeCfg()), (False, 'already_acked'))

    def test_daily_limit(self, mock_count, mock_acked):
        from apps.tenant.analytics.auto_reply import auto_ack_precheck
        mock_count.return_value = 50
        self.assertEqual(auto_ack_precheck(self._neg(), _FakeCfg()), (False, 'daily_limit'))

    def test_draft_and_needs_human_do_not_block_ack(self, *_):
        """Подтверждение не зависит от черновика и от вопроса в отзыве."""
        from apps.tenant.analytics.auto_reply import auto_ack_precheck
        conv = self._neg(ai_draft='', ai_draft_rejected=True, ai_needs_human=True)
        self.assertEqual(auto_ack_precheck(conv, _FakeCfg()), (True, ''))


@patch('apps.tenant.analytics.auto_reply._auto_sent_today_count', return_value=0)
class AutoSendPrecheckTest(SimpleTestCase):
    """auto_send_precheck: по одной проверке на каждую причину отказа."""

    def _check(self, conv=None, cfg=None):
        from apps.tenant.analytics.auto_reply import auto_send_precheck
        return auto_send_precheck(conv or _FakeConv(), cfg or _FakeCfg())

    def test_ok_for_plain_positive_review(self, _cnt):
        ok, reason = self._check()
        self.assertTrue(ok)
        self.assertEqual(reason, '')

    def test_master_flag_off(self, _cnt):
        ok, reason = self._check(cfg=_FakeCfg(auto_send_enabled=False))
        self.assertFalse(ok)
        self.assertEqual(reason, 'config_off')

    def test_drafts_feature_off(self, _cnt):
        ok, reason = self._check(cfg=_FakeCfg(enabled=False))
        self.assertFalse(ok)
        self.assertEqual(reason, 'config_off')

    def test_not_positive(self, _cnt):
        ok, reason = self._check(conv=_FakeConv(sentiment='NEGATIVE'))
        self.assertFalse(ok)
        self.assertEqual(reason, 'not_positive')

    def test_already_replied(self, _cnt):
        ok, reason = self._check(conv=_FakeConv(is_replied=True))
        self.assertFalse(ok)
        self.assertEqual(reason, 'manual_reply')

    def test_rejected_draft(self, _cnt):
        ok, reason = self._check(conv=_FakeConv(ai_draft_rejected=True))
        self.assertFalse(ok)
        self.assertEqual(reason, 'rejected_draft')

    def test_no_draft(self, _cnt):
        ok, reason = self._check(conv=_FakeConv(ai_draft='   '))
        self.assertFalse(ok)
        self.assertEqual(reason, 'no_draft')

    def test_needs_human(self, _cnt):
        ok, reason = self._check(conv=_FakeConv(ai_needs_human=True))
        self.assertFalse(ok)
        self.assertEqual(reason, 'needs_human')

    def test_numeric_only_by_ai_comment(self, _cnt):
        conv = _FakeConv(ai_comment='Оценка-цифра: 10 (без текста).')
        ok, reason = self._check(conv=conv)
        self.assertFalse(ok)
        self.assertEqual(reason, 'numeric_only')

    def test_numeric_only_by_message_text(self, _cnt):
        # «10!!!» после выброса цифр и пунктуации не оставляет букв.
        conv = _FakeConv(messages=_FakeMessages(['10!!!', '👍']))
        ok, reason = self._check(conv=conv)
        self.assertFalse(ok)
        self.assertEqual(reason, 'numeric_only')

    def test_no_vk_sender(self, _cnt):
        ok, reason = self._check(conv=_FakeConv(vk_sender_id=''))
        self.assertFalse(ok)
        self.assertEqual(reason, 'no_vk_sender')

    def test_branch_disabled(self, _cnt):
        conv = _FakeConv(branch_id=7)
        cfg = _FakeCfg(auto_send_branch_enabled={'7': False})
        ok, reason = self._check(conv=conv, cfg=cfg)
        self.assertFalse(ok)
        self.assertEqual(reason, 'branch_disabled')

    def test_branch_enabled_explicitly(self, _cnt):
        conv = _FakeConv(branch_id=7)
        cfg = _FakeCfg(auto_send_branch_enabled={'7': True})
        ok, _reason = self._check(conv=conv, cfg=cfg)
        self.assertTrue(ok)

    def test_vk_thread_without_branch_allowed(self, _cnt):
        # ВК-тред (branch=None) не блокируется картой точек.
        cfg = _FakeCfg(auto_send_branch_enabled={'7': False})
        ok, _reason = self._check(cfg=cfg)
        self.assertTrue(ok)

    def test_daily_limit(self, mock_count):
        mock_count.return_value = 50
        ok, reason = self._check(cfg=_FakeCfg(auto_send_daily_limit=50))
        self.assertFalse(ok)
        self.assertEqual(reason, 'daily_limit')


class ComputeAutoSendTimeTest(SimpleTestCase):
    """compute_auto_send_time: +delay днём, перенос на 09:00+delay в тихие часы."""

    def setUp(self):
        # Тихие часы могут быть переопределены env на проде — фиксируем 22..9,
        # иначе тест начнёт зависеть от конфигурации сервера.
        from apps.tenant.analytics import auto_reply
        for name, value in (
            ('REVIEW_PUSH_QUIET_START_HOUR', 22),
            ('REVIEW_PUSH_QUIET_END_HOUR', 9),
        ):
            p = patch.object(auto_reply, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _local(self, y, m, d, hh, mm):
        return datetime(y, m, d, hh, mm, tzinfo=dj_timezone.get_current_timezone())

    def test_daytime_just_adds_delay(self):
        from apps.tenant.analytics.auto_reply import compute_auto_send_time
        now = self._local(2026, 3, 10, 14, 0)
        got = dj_timezone.localtime(compute_auto_send_time(now, 15))
        self.assertEqual((got.day, got.hour, got.minute), (10, 14, 15))

    def test_late_evening_moves_to_next_morning(self):
        from apps.tenant.analytics.auto_reply import compute_auto_send_time
        now = self._local(2026, 3, 10, 21, 50)   # +15 = 22:05 → тихо
        got = dj_timezone.localtime(compute_auto_send_time(now, 15))
        self.assertEqual((got.day, got.hour, got.minute), (11, 9, 15))

    def test_night_moves_to_same_morning(self):
        from apps.tenant.analytics.auto_reply import compute_auto_send_time
        now = self._local(2026, 3, 10, 3, 0)     # +15 = 03:15 → тихо
        got = dj_timezone.localtime(compute_auto_send_time(now, 15))
        self.assertEqual((got.day, got.hour, got.minute), (10, 9, 15))

    def test_result_is_in_the_future(self):
        from apps.tenant.analytics.auto_reply import compute_auto_send_time
        now = dj_timezone.now()
        self.assertGreater(compute_auto_send_time(now, 5), now)


class BuildReviewLinksKeyboardTest(SimpleTestCase):
    """build_review_links_keyboard: кнопки только под непустые ссылки."""

    Y = 'https://yandex.ru/maps/org/1/reviews/'
    G = 'https://2gis.ru/firm/1/tab/reviews'

    def test_both_links_from_branch(self):
        from apps.tenant.analytics.auto_reply import build_review_links_keyboard
        conv = _FakeConv(branch_id=3, branch=_FakeBranch(self.Y, self.G))
        kb = build_review_links_keyboard(conv)
        self.assertTrue(kb['inline'])
        self.assertEqual(len(kb['buttons']), 2)
        first = kb['buttons'][0][0]['action']
        self.assertEqual(first['type'], 'open_link')
        self.assertEqual(first['link'], self.Y)
        for row in kb['buttons']:
            self.assertLessEqual(len(row[0]['action']['label']), 40)

    def test_single_link_gives_single_button(self):
        from apps.tenant.analytics.auto_reply import build_review_links_keyboard
        conv = _FakeConv(branch_id=3, branch=_FakeBranch(self.Y, ''))
        kb = build_review_links_keyboard(conv)
        self.assertEqual(len(kb['buttons']), 1)
        self.assertEqual(kb['buttons'][0][0]['action']['link'], self.Y)

    @patch('apps.tenant.branch.api.services.get_fallback_review_links')
    def test_falls_back_to_network_links(self, mock_fb):
        from apps.tenant.analytics.auto_reply import build_review_links_keyboard
        mock_fb.return_value = (self.Y, self.G)
        kb = build_review_links_keyboard(_FakeConv())   # branch=None
        self.assertEqual(len(kb['buttons']), 2)
        mock_fb.assert_called_once()

    @patch('apps.tenant.branch.api.services.get_fallback_review_links')
    def test_no_links_at_all_returns_none(self, mock_fb):
        from apps.tenant.analytics.auto_reply import build_review_links_keyboard
        mock_fb.return_value = ('', '')
        self.assertIsNone(build_review_links_keyboard(_FakeConv()))


class NeedsHumanParsingTest(SimpleTestCase):
    """analyze_message: needs_human из ответа модели + эвристика-страховка."""

    def _answer(self, raw_json):
        block = MagicMock()
        block.text = raw_json
        resp = MagicMock()
        resp.content = [block]
        client = MagicMock()
        client.messages.create.return_value = resp
        return client

    def _run(self, raw_json, text):
        from apps.tenant.analytics import ai_service
        with patch.object(ai_service, '_build_system_prompt', return_value='sys'), \
             patch('anthropic.Anthropic', return_value=self._answer(raw_json)):
            return ai_service.analyze_message(text, 'VK_MESSAGE')

    @override_settings(ANTHROPIC_API_KEY='sk-test')
    def test_flag_from_model(self):
        res = self._run(
            '{"sentiment":"POSITIVE","comment":"ок","needs_human":true}',
            'Всё супер, приду ещё',
        )
        self.assertTrue(res['needs_human'])
        self.assertEqual(res['sentiment'], 'POSITIVE')

    @override_settings(ANTHROPIC_API_KEY='sk-test')
    def test_missing_key_defaults_to_false(self):
        # Старый формат ответа (без needs_human) + текст без вопроса.
        res = self._run(
            '{"sentiment":"POSITIVE","comment":"ок"}',
            'Всё супер, приду ещё',
        )
        self.assertFalse(res['needs_human'])

    @override_settings(ANTHROPIC_API_KEY='sk-test')
    def test_heuristic_question_mark_wins(self):
        res = self._run(
            '{"sentiment":"POSITIVE","comment":"ок","needs_human":false}',
            'Всё супер! А во сколько вы закрываетесь?',
        )
        self.assertTrue(res['needs_human'])

    def test_heuristic_markers(self):
        from apps.tenant.analytics.ai_service import _heuristic_needs_human
        self.assertTrue(_heuristic_needs_human('Хочу забронировать стол на 5'))
        self.assertTrue(_heuristic_needs_human('Перезвоните мне пожалуйста'))
        self.assertTrue(_heuristic_needs_human('Подскажите, есть ли доставка'))
        self.assertFalse(_heuristic_needs_human('Очень вкусно, спасибо!'))
        self.assertFalse(_heuristic_needs_human(''))

    def test_numeric_rating_never_needs_human(self):
        from apps.tenant.analytics.ai_service import _try_numeric_rating
        res = _try_numeric_rating('10', 'VK_MESSAGE')
        self.assertEqual(res['sentiment'], 'POSITIVE')
        self.assertFalse(res['needs_human'])


class SendVkReplyKeyboardTest(SimpleTestCase):
    """send_vk_reply: клавиатура уходит в теле POST, сообщение помечается как ИИ."""

    def _vk_response(self, payload=b'{"response": 777}'):
        resp = MagicMock()
        resp.read.return_value = payload
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=resp)
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    @patch('apps.tenant.branch.api.services.TestimonialMessage')
    @patch('apps.tenant.senler.models.SenlerConfig')
    @patch('urllib.request.urlopen')
    def test_keyboard_is_posted(self, mock_urlopen, MockCfg, MockMsg):
        from apps.tenant.branch.api.services import send_vk_reply

        mock_urlopen.return_value = self._vk_response()
        cfg = MagicMock()
        cfg.vk_community_token = 'tok'
        MockCfg.objects.filter.return_value.first.return_value = cfg

        conv = MagicMock()
        conv.vk_sender_id = '12345'
        conv.branch_id = 3

        keyboard = {
            'inline': True,
            'buttons': [[{'action': {
                'type': 'open_link', 'link': 'https://yandex.ru/maps/1/',
                'label': '⭐ Яндекс Карты',
            }}]],
        }
        send_vk_reply(conv, 'Спасибо!', sender_name='ИИ-ассистент',
                      keyboard=keyboard, is_ai_generated=True)

        # Это POST: тело в data=, а не в URL.
        body = mock_urlopen.call_args.kwargs['data'].decode('utf-8')
        self.assertIn('keyboard', body)
        decoded = urllib.parse.unquote_plus(body)
        self.assertIn('"inline": true', decoded)
        self.assertIn('open_link', decoded)
        self.assertNotIn('keyboard', mock_urlopen.call_args.args[0])

        # Сообщение сохранено с пометкой «отправил ИИ».
        self.assertTrue(MockMsg.objects.create.call_args.kwargs['is_ai_generated'])

    @patch('apps.tenant.branch.api.services.TestimonialMessage')
    @patch('apps.tenant.senler.models.SenlerConfig')
    @patch('urllib.request.urlopen')
    def test_without_keyboard_param_absent(self, mock_urlopen, MockCfg, MockMsg):
        from apps.tenant.branch.api.services import send_vk_reply

        mock_urlopen.return_value = self._vk_response()
        cfg = MagicMock()
        cfg.vk_community_token = 'tok'
        MockCfg.objects.filter.return_value.first.return_value = cfg

        conv = MagicMock()
        conv.vk_sender_id = '12345'
        conv.branch_id = 3

        with patch('apps.tenant.analytics.auto_reply.cancel_auto_send') as mock_cancel:
            send_vk_reply(conv, 'Ответ менеджера')
            # Ручной ответ снимает запланированный автоответ.
            mock_cancel.assert_called_once()

        body = mock_urlopen.call_args.kwargs['data'].decode('utf-8')
        self.assertNotIn('keyboard', body)
        self.assertFalse(MockMsg.objects.create.call_args.kwargs['is_ai_generated'])


class DraftFreshnessTest(SimpleTestCase):
    """
    Актуальность AI-черновика (09.09.2026): черновик свежий, пока последнее
    сообщение гостя — то, по которому он сгенерирован. Гость дописал →
    устарел → перегенерация; наши ответы/поллинг/переклассификация — нет.
    """

    def _conv(self, draft='Текст', marker=None, updated_at=None, rejected=False):
        c = MagicMock()
        c.pk = 1
        c.ai_draft = draft
        c.ai_draft_message_id = marker
        c.ai_draft_rejected = rejected
        c.updated_at = updated_at
        return c

    def test_no_draft_is_stale(self):
        from apps.tenant.analytics.auto_reply import draft_is_stale
        self.assertTrue(draft_is_stale(self._conv(draft=''), (10, dj_timezone.now())))

    def test_marker_matches_last_guest_message_is_fresh(self):
        from apps.tenant.analytics.auto_reply import draft_is_stale
        self.assertFalse(draft_is_stale(self._conv(marker=10), (10, dj_timezone.now())))

    def test_new_guest_message_after_marker_is_stale(self):
        from apps.tenant.analytics.auto_reply import draft_is_stale
        self.assertTrue(draft_is_stale(self._conv(marker=10), (11, dj_timezone.now())))

    def test_no_guest_messages_is_fresh(self):
        from apps.tenant.analytics.auto_reply import draft_is_stale
        self.assertFalse(draft_is_stale(self._conv(marker=10), None))

    def test_legacy_draft_without_marker_uses_updated_at(self):
        from datetime import timedelta
        from apps.tenant.analytics.auto_reply import draft_is_stale
        now = dj_timezone.now()
        # Черновик обновлён час назад, гость написал 5 минут назад → устарел.
        self.assertTrue(draft_is_stale(self._conv(updated_at=now - timedelta(hours=1)), (5, now - timedelta(minutes=5))))
        # Черновик новее последнего сообщения гостя → свежий.
        self.assertFalse(draft_is_stale(self._conv(updated_at=now), (5, now - timedelta(minutes=5))))

    def test_cap_constant_is_sane(self):
        from apps.tenant.analytics.auto_reply import DRAFT_AUTO_GEN_CAP, DRAFT_REGEN_DEBOUNCE_SEC
        self.assertGreaterEqual(DRAFT_AUTO_GEN_CAP, 3)
        self.assertLessEqual(DRAFT_AUTO_GEN_CAP, 20)
        self.assertGreaterEqual(DRAFT_REGEN_DEBOUNCE_SEC, 60)


class EnqueueAiDraftDebounceTest(SimpleTestCase):
    """_enqueue_ai_draft: первый черновик — сразу; перегенерация — одна задача с задержкой."""

    @patch('apps.tenant.analytics.tasks.auto_generate_draft_task')
    @patch('apps.tenant.branch.models.TestimonialConversation')
    def test_fresh_thread_enqueues_immediately(self, MockTC, mock_task):
        from apps.tenant.branch.api.services import _enqueue_ai_draft
        conv = MagicMock(ai_draft='', ai_draft_rejected=False, auto_send_status='')
        MockTC.objects.filter.return_value.only.return_value.first.return_value = conv
        _enqueue_ai_draft(7)
        mock_task.delay.assert_called_once()
        mock_task.apply_async.assert_not_called()

    @patch('apps.tenant.analytics.auto_reply.cancel_auto_send')
    @patch('django.core.cache.cache')
    @patch('apps.tenant.analytics.tasks.auto_generate_draft_task')
    @patch('apps.tenant.branch.models.TestimonialConversation')
    def test_existing_draft_debounces_and_cancels_old_plan(self, MockTC, mock_task, mock_cache, mock_cancel):
        from apps.tenant.branch.api.services import _enqueue_ai_draft
        from apps.tenant.analytics.auto_reply import DRAFT_REGEN_DEBOUNCE_SEC
        conv = MagicMock(ai_draft='Старый черновик', ai_draft_rejected=False, auto_send_status='scheduled')
        MockTC.objects.filter.return_value.only.return_value.first.return_value = conv
        mock_cache.add.side_effect = [True, False, False]

        for _ in range(3):  # гость пишет три сообщения подряд
            _enqueue_ai_draft(7)

        self.assertEqual(mock_task.apply_async.call_count, 1)
        self.assertEqual(mock_task.apply_async.call_args.kwargs['countdown'], DRAFT_REGEN_DEBOUNCE_SEC)
        mock_task.delay.assert_not_called()
        self.assertEqual(mock_cancel.call_count, 3)
        self.assertEqual(mock_cancel.call_args.args[1], 'guest_wrote_again')


class AnalyticsApiRequiresAuthTest(SimpleTestCase):
    """
    Все ручки /api/v1/analytics/* закрыты для анонима.

    09.09.2026: в settings нет DEFAULT_PERMISSION_CLASSES (= AllowAny), и 14 вьюх
    аналитики — включая массовую рассылку rf/send-broadcast — отвечали без
    авторизации. Тест не даёт новой вьюхе снова родиться открытой.
    """

    def test_every_analytics_api_view_declares_is_authenticated(self):
        import inspect

        from rest_framework.permissions import IsAuthenticated
        from rest_framework.views import APIView

        from apps.tenant.analytics.api import views as v

        checked = 0
        for name, cls in inspect.getmembers(v, inspect.isclass):
            if cls is APIView or not issubclass(cls, APIView) or cls.__module__ != v.__name__:
                continue
            checked += 1
            with self.subTest(view=name):
                self.assertIn(IsAuthenticated, cls.permission_classes)
        self.assertGreaterEqual(checked, 22)

    def test_anonymous_requests_are_rejected(self):
        from rest_framework.test import APIRequestFactory

        from apps.tenant.analytics.api.views import BranchListAPIView, SendSegmentBroadcastAPIView

        factory = APIRequestFactory()

        resp = SendSegmentBroadcastAPIView.as_view()(
            factory.post('/api/v1/analytics/rf/send-broadcast/', {'message_text': 'x'}),
        )
        self.assertIn(resp.status_code, (401, 403))

        resp = BranchListAPIView.as_view()(factory.get('/api/v1/analytics/branches/'))
        self.assertIn(resp.status_code, (401, 403))
