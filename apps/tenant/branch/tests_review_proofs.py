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
             patch.object(RP.CoinTransaction, 'objects') as tx:
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
        (p, credited), tx = self.run_approve(proof)
        self.assertFalse(credited)
        tx.create_transfer.assert_not_called()

    def test_rejected_cannot_be_approved(self):
        with self.assertRaises(RP.ProofError):
            self.run_approve(_proof('rejected'))

    def test_reject_needs_reason(self):
        with patch.object(RP.transaction, 'atomic', lambda: nullcontext()), \
             patch.object(RP.ReviewProof, 'objects') as proofs:
            proof = _proof()
            proofs.select_for_update.return_value.filter.return_value.first.return_value = proof
            with self.assertRaises(RP.ProofError):
                RP.reject(5, '  ', 'Анна')
            RP.reject(5, 'Скриншот не с нашей точки', 'Анна')
            self.assertEqual(proof.status, 'rejected')


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
