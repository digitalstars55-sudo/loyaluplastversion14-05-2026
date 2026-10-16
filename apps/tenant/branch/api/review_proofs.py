"""
Скриншоты отзывов на картах (волна 3, 14.10.2026) — `review_proofs.py`.

Гость (подпись запуска ВК / веб-сессия):
    POST /api/v1/client/review-proof/  multipart {vk_id, branch_id, platform, image}
    GET  /api/v1/client/review-proof/?vk_id=     — свои скриншоты и статусы
Сотрудник (охват точек как у точек контакта; право решает кабинет CheckUp):
    GET  /api/v1/review-proofs/?status=pending&branch_ids=
    POST /api/v1/review-proofs/<id>/approve/
    POST /api/v1/review-proofs/<id>/reject/  {reason?}
    POST /api/v1/review-proofs/<id>/resend/  — повторить ТОЛЬКО сообщение гостю
Сумма и тексты сообщений — настройки отзывов сети (`ReviewAutoReplyConfig.proof_*`).
Флаг сети `review_proof_enabled` выключен → гостевые ручки 404.
"""
import logging

from django.db import connection
from rest_framework import status as http
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.users.access import current_schema_name, effective_branch_ids

from .. import review_proofs as RP
from ..models import ReviewProof
from .client_phone import _err
from .client_receipt import _guest

log = logging.getLogger(__name__)


def _config():
    tenant = getattr(connection, 'tenant', None)
    return getattr(tenant, 'config', None) if tenant is not None else None


def _row(p, request=None, staff=False):
    url = p.image.url if p.image else ''
    if request is not None and url.startswith('/'):
        url = request.build_absolute_uri(url)
    row = {
        'id': p.pk, 'platform': p.platform, 'platform_label': p.get_platform_display(),
        'status': p.status, 'status_label': p.get_status_display(), 'coins': p.coins,
        'reject_reason': p.reject_reason, 'created_at': p.created_at.isoformat(),
        'reviewed_at': p.reviewed_at.isoformat() if p.reviewed_at else None,
        'image_url': url,
        'source': p.source or 'app',
        'message_status': p.message_status, 'message_error': p.message_error,
        'branch': {'id': p.client.branch_id, 'branch_id': p.client.branch.branch_id,
                   'name': p.client.branch.name},
    }
    if staff:
        g = p.client.client
        row.update({
            'reviewed_by': p.reviewed_by,
            'guest': {'vk_id': g.vk_id, 'name': ' '.join(x for x in (g.first_name, g.last_name) if x),
                      'vk_url': f'https://vk.com/id{g.vk_id}'},
        })
    return row


class ClientReviewProofView(APIView):

    def post(self, request):
        cfg = _config()
        if not getattr(cfg, 'review_proof_enabled', False):
            return _err('feature_disabled', 'Не найдено.', http.HTTP_404_NOT_FOUND)
        try:
            claimed = int(request.data.get('vk_id') or 0)
        except (TypeError, ValueError):
            claimed = 0
        client, error = _guest(request, claimed)
        if error:
            return error
        try:
            proof = RP.submit(client, request.data.get('branch_id'), str(request.data.get('platform') or ''),
                              request.FILES.get('image'))
        except RP.ProofError as e:
            return _err(e.code, e.detail, e.http_status)
        log.info('review proof: vk_id=%s %s id=%s', client.vk_id, proof.platform, proof.pk)
        return Response(_row(proof, request), status=http.HTTP_201_CREATED)

    def get(self, request):
        cfg = _config()
        if not getattr(cfg, 'review_proof_enabled', False):
            return _err('feature_disabled', 'Не найдено.', http.HTTP_404_NOT_FOUND)
        try:
            claimed = int(request.query_params.get('vk_id') or 0)
        except ValueError:
            claimed = 0
        client, error = _guest(request, claimed)
        if error:
            return error
        rows = (ReviewProof.objects.filter(client__client=client)
                .select_related('client__branch')[:20])
        return Response({'coins': RP.reward(cfg),
                         'results': [_row(p, request) for p in rows]})


def _scope(request):
    raw = request.query_params.get('branch_ids') or ''
    requested = [int(x) for x in raw.split(',') if x.strip().isdigit()] or None
    return effective_branch_ids(request.user, current_schema_name(), requested)


def _who(user):
    full = getattr(user, 'get_full_name', lambda: '')() or ''
    return full or getattr(user, 'username', '') or str(getattr(user, 'pk', ''))


class ReviewProofListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = ReviewProof.objects.select_related('client__branch', 'client__client')
        scope = _scope(request)
        if scope:
            qs = qs.filter(client__branch_id__in=scope)
        st = (request.query_params.get('status') or '').strip()
        if st:
            if st not in ReviewProof.Status.values:
                return _err('invalid_payload', 'status: ' + ' | '.join(ReviewProof.Status.values),
                            http.HTTP_400_BAD_REQUEST)
            qs = qs.filter(status=st)
        pending = qs.model.objects.filter(status=ReviewProof.Status.PENDING)
        if scope:
            pending = pending.filter(client__branch_id__in=scope)
        rows = list(qs[:100])
        return Response({'pending': pending.count(),
                         'coins': RP.reward(_config()),
                         'results': [_row(p, request, staff=True) for p in rows]})


def _in_scope(request, pk):
    scope = _scope(request)
    qs = ReviewProof.objects.filter(pk=pk)
    if scope:
        qs = qs.filter(client__branch_id__in=scope)
    return qs.exists()


class ReviewProofApproveView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk: int):
        if not _in_scope(request, pk):
            return _err('not_found', 'Скриншот не найден', http.HTTP_404_NOT_FOUND)
        coins = RP.reward(_config())
        try:
            proof, credited = RP.approve(pk, coins, _who(request.user))
        except RP.ProofError as e:
            return _err(e.code, e.detail, e.http_status)
        except Exception:                                 # noqa: BLE001
            # Начисление откатилось целиком — заявка «На проверке», повтор безопасен.
            log.exception('review proof %s: credit failed', pk)
            return _err('credit_failed', 'Ошибка начисления — баллы не начислены, повторите',
                        http.HTTP_502_BAD_GATEWAY)
        log.info('review proof %s approved by=%s coins=%s new=%s', pk, _who(request.user), proof.coins, credited)
        proof = ReviewProof.objects.select_related('client__branch', 'client__client').get(pk=pk)
        return Response(dict(_row(proof, request, staff=True), already_approved=not credited))


class ReviewProofRejectView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk: int):
        if not _in_scope(request, pk):
            return _err('not_found', 'Скриншот не найден', http.HTTP_404_NOT_FOUND)
        try:
            RP.reject(pk, str((request.data or {}).get('reason') or ''), _who(request.user))
        except RP.ProofError as e:
            return _err(e.code, e.detail, e.http_status)
        proof = ReviewProof.objects.select_related('client__branch', 'client__client').get(pk=pk)
        return Response(_row(proof, request, staff=True))


class ReviewProofResendView(APIView):
    """Повторить сообщение гостю по уже принятому решению (баллы не трогает)."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk: int):
        if not _in_scope(request, pk):
            return _err('not_found', 'Скриншот не найден', http.HTTP_404_NOT_FOUND)
        try:
            RP.resend(pk)
        except RP.ProofError as e:
            return _err(e.code, e.detail, e.http_status)
        proof = ReviewProof.objects.select_related('client__branch', 'client__client').get(pk=pk)
        return Response(_row(proof, request, staff=True))
