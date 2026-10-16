"""Волна 3: очередь скриншотов отзывов на картах — баллы один раз, после проверки."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tenant.branch import review_proofs as RP
from apps.tenant.branch.api import review_proofs as API
from apps.tenant.branch.models import ReviewProof


def _img(ct='image/png', size=1000):
    return SimpleNamespace(content_type=ct, size=size)


def _proof(status='pending'):
    p = SimpleNamespace(pk=5, status=status, client=SimpleNamespace(pk=9), coins=0, transaction=None,
                        reviewed_by='', reviewed_at=None, reject_reason='', save=MagicMock())
    p.get_platform_display = lambda: 'Яндекс.Карты'
    p.get_status_display = lambda: dict(ReviewProof.Status.choices)[p.status]
    return p


class SubmitTest(SimpleTestCase):
    def test_validation(self):
        for platform, image, code in (('vk', _img(), 'invalid_payload'),
                                      ('yandex', None, 'invalid_payload'),
                                      ('yandex', _img(size=11 * 1024 * 1024), 'invalid_payload'),
                                      ('2gis', _img('application/pdf'), 'invalid_payload')):
            with self.assertRaises(RP.ProofError) as cm:
                RP.submit(SimpleNamespace(), 1, platform, image)
            self.assertEqual(cm.exception.code, code)

    def test_no_guest_in_branch_404_and_cooldown_409(self):
        with patch.object(RP.ClientBranch, 'objects') as cbs, patch.object(RP.ReviewProof, 'objects') as proofs:
            cbs.filter.return_value.first.return_value = None
            with self.assertRaises(RP.ProofError) as cm:
                RP.submit(SimpleNamespace(), 1, 'yandex', _img())
            self.assertEqual(cm.exception.http_status, 404)
            cbs.filter.return_value.first.return_value = SimpleNamespace(pk=9)
            proofs.filter.return_value.exclude.return_value.exists.return_value = True
            with self.assertRaises(RP.ProofError) as cm:
                RP.submit(SimpleNamespace(), 1, 'yandex', _img())
            self.assertEqual(cm.exception.code, 'already_sent')
            proofs.filter.return_value.exclude.return_value.exists.return_value = False
            RP.submit(SimpleNamespace(), 1, 'yandex', _img())
            proofs.create.assert_called_once()


class ApproveRejectTest(SimpleTestCase):
    def run_approve(self, proof, coins=50):
        with patch.object(RP.transaction, 'atomic', lambda: nullcontext()), \
             patch.object(RP.ReviewProof, 'objects') as proofs, \
             patch.object(RP.CoinTransaction, 'objects') as tx, \
             patch.object(RP, 'notify_guest') as self.notify:
            proofs.select_for_update.return_value.select_related.return_value.filter.return_value.first.return_value = proof
            tx.create_transfer.return_value = 'TX'
            result = RP.approve(5, coins, 'Анна')
            return result, tx

    def test_approve_credits_once(self):
        proof = _proof()
        (p, credited), tx = self.run_approve(proof)
        self.assertTrue(credited)
        self.assertEqual((p.status, p.coins, p.transaction), ('approved', 50, 'TX'))
        tx.create_transfer.assert_called_once()
        self.notify.assert_called_once()                # сообщение — после начисления
        (p, credited), tx = self.run_approve(proof)
        self.assertFalse(credited)
        tx.create_transfer.assert_not_called()

    def test_rejected_cannot_be_approved(self):
        with self.assertRaises(RP.ProofError):
            self.run_approve(_proof('rejected'))

    def test_reject_without_reason_sends_settings_text(self):
        # ТЗ 15.10.2026: гостю уходит текст из настроек сети, причина — для истории.
        with patch.object(RP.transaction, 'atomic', lambda: nullcontext()), \
             patch.object(RP.ReviewProof, 'objects') as proofs, \
             patch.object(RP, 'notify_guest') as notify:
            proof = _proof()
            proofs.select_for_update.return_value.filter.return_value.first.return_value = proof
            RP.reject(5, '  ', 'Анна')
            self.assertEqual((proof.status, proof.reject_reason), ('rejected', ''))
            notify.assert_called_once_with(proof)


class GuestFlagTest(SimpleTestCase):
    def test_flag_off_404(self):
        request = APIRequestFactory().get('/api/v1/client/review-proof/', {'vk_id': 1})
        with patch.object(API, '_config', return_value=SimpleNamespace(review_proof_enabled=False)):
            resp = API.ClientReviewProofView.as_view()(request)
        self.assertEqual(resp.status_code, 404)


class StaffScopeTest(SimpleTestCase):
    def test_out_of_scope_is_404(self):
        request = APIRequestFactory().post('/api/v1/review-proofs/5/approve/', {}, format='json')
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True, pk=1))
        with patch.object(API, '_in_scope', return_value=False):
            resp = API.ReviewProofApproveView.as_view()(request, pk=5)
        self.assertEqual(resp.status_code, 404)



def _decided(status='approved', coins=50, message_status=''):
    p = _proof(status)
    p.coins, p.message_status, p.message_error = coins, message_status, ''
    p.client = SimpleNamespace(pk=9, branch_id=3, client=SimpleNamespace(vk_id=777))
    return p


class NotifyGuestTest(SimpleTestCase):
    """Сообщения из настроек сети: {вознаграждение} подставлено, отправленное повторно не шлём."""

    def _run(self, proof, ok=True):
        cfg = SimpleNamespace(proof_approved_text='Начислили {вознаграждение} баллов',
                              proof_rejected_text='Не подтвердили', proof_coins=50)
        conf = SimpleNamespace(vk_community_token='t')
        with patch('apps.tenant.branch.models.ReviewAutoReplyConfig.get_singleton', return_value=cfg), \
             patch('apps.tenant.senler.models.SenlerConfig.objects') as sc, \
             patch('apps.tenant.senler.services.send_vk_message',
                   return_value=(ok, '' if ok else 'нет доступа', 1)) as send:
            sc.filter.return_value.first.return_value = conf
            out = RP.notify_guest(proof)
        return out, send

    def test_approved_text_with_reward(self):
        proof = _decided()
        out, send = self._run(proof)
        self.assertEqual(out, 'sent')
        self.assertEqual(send.call_args.args[1:], (777, 'Начислили 50 баллов'))
        self.assertEqual(proof.message_status, 'sent')

    def test_failed_is_saved_and_already_sent_not_repeated(self):
        proof = _decided()
        out, _ = self._run(proof, ok=False)
        self.assertEqual((out, proof.message_status, proof.message_error), ('failed', 'failed', 'нет доступа'))
        sent = _decided(message_status='sent')
        out, send = self._run(sent)
        self.assertEqual(out, 'sent')
        send.assert_not_called()

    def test_fill(self):
        self.assertEqual(RP.fill('за отзыв {вознаграждение} баллов', 30), 'за отзыв 30 баллов')
