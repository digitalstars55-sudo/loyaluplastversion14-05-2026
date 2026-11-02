"""Этап 1 (02.11.2026). Гость: уровень и приглашение. Кабинет: подарочные сертификаты.

    GET  /api/v1/client/loyalty-extra/?vk_id=&branch_id=
    GET  /api/v1/certificates/?q=          POST /api/v1/certificates/
    POST /api/v1/certificates/<id>/redeem/ {amount, branch_id?, comment?}
    POST /api/v1/certificates/<id>/void/
    GET  /api/v1/public/certificate/<code>/  — страница для получателя, доступ по коду
Выключенный флаг сети отвечает 404.
"""
from django.db import connection, transaction
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.utils import timezone
from django.utils.html import escape
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.users.access import current_schema_name, user_allowed_branches

from .. import loyalty_extra as LE
from ..models import Branch, ClientBranch, GiftCertificate, GiftCertificateUse
from .client_phone import _err
from .client_receipt import _guest


def _config():
    tenant = getattr(connection, 'tenant', None)
    return getattr(tenant, 'config', None) if tenant is not None else None


def _who(user) -> str:
    return (getattr(user, 'get_full_name', lambda: '')() or getattr(user, 'username', '') or str(user))[:150]


class ClientLoyaltyExtraView(APIView):

    def get(self, request):
        cfg = _config()
        друг, уровни = getattr(cfg, 'referral_enabled', False), getattr(cfg, 'levels_enabled', False)
        if not друг and not уровни:
            return _err('feature_disabled', 'Не найдено.', status.HTTP_404_NOT_FOUND)
        try:
            заявлен = int(request.query_params.get('vk_id') or 0)
        except ValueError:
            заявлен = 0
        client, error = _guest(request, заявлен)
        if error:
            return error
        ответ = {'level': LE.уровень_гостя(client.pk, cfg) if уровни else None, 'referral': None}
        if друг:
            профили = ClientBranch.objects.filter(client=client).select_related('branch').order_by('-updated_at')
            точка = (request.query_params.get('branch_id') or '').strip()
            профиль = (профили.filter(branch__branch_id=точка).first() if точка.isdigit() else None) or профили.first()
            if профиль is not None:
                ответ['referral'] = LE.сводка_друзей(профиль, cfg)
                ответ['referral']['url'] = request.build_absolute_uri(ответ['referral']['path'])
        return Response(ответ)


def _ряд(c, request=None) -> dict:
    сегодня = timezone.localdate()
    if c.voided_at:
        статус = 'void'
    elif c.expires_at and c.expires_at < сегодня:
        статус = 'expired'
    elif c.balance == 0:
        статус = 'spent'
    else:
        статус = 'active'
    ряд = {
        'id': c.pk, 'code': c.code, 'nominal': c.nominal, 'balance': c.balance, 'status': статус,
        'expires_at': c.expires_at.isoformat() if c.expires_at else None, 'buyer_name': c.buyer_name,
        'recipient_name': c.recipient_name, 'recipient_phone': c.recipient_phone, 'note': c.note,
        'issued_by': c.issued_by, 'created_at': c.created_at.isoformat(),
        'uses': [{'amount': u.amount, 'who': u.who, 'comment': u.comment, 'created_at': u.created_at.isoformat(),
                  'branch': u.branch.name if u.branch_id else ''} for u in c.uses.all()],
    }
    if request is not None:
        ряд['url'] = request.build_absolute_uri(f'/api/v1/public/certificate/{c.code}/')
    return ряд


class _Кабинет(APIView):
    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not getattr(_config(), 'certificates_enabled', False):
            raise Http404


class CertificateListView(_Кабинет):

    def get(self, request):
        qs = GiftCertificate.objects.prefetch_related('uses__branch')
        q = (request.query_params.get('q') or '').strip()
        if q:
            qs = qs.filter(Q(code__iexact=q.upper()) | Q(recipient_phone__icontains=q) | Q(recipient_name__icontains=q)
                           | Q(buyer_name__icontains=q))
        return Response({'results': [_ряд(c, request) for c in qs[:200]]})

    def post(self, request):
        d = request.data
        try:
            номинал = int(d.get('nominal') or 0)
        except (TypeError, ValueError):
            номинал = 0
        if номинал <= 0 or номинал > 1_000_000:
            return _err('bad_nominal', 'Укажите номинал от 1 до 1 000 000 ₽.', status.HTTP_400_BAD_REQUEST)
        срок = None
        if d.get('expires_at'):
            from django.utils.dateparse import parse_date
            try:
                срок = parse_date(str(d['expires_at']))
            except ValueError:
                срок = None
            if срок is None or срок < timezone.localdate():
                return _err('bad_date', 'Срок действия указан неверно.', status.HTTP_400_BAD_REQUEST)
        c = GiftCertificate.objects.create(
            nominal=номинал, balance=номинал, expires_at=срок, buyer_name=str(d.get('buyer_name') or '')[:120],
            recipient_name=str(d.get('recipient_name') or '')[:120],
            recipient_phone=str(d.get('recipient_phone') or '')[:32], note=str(d.get('note') or '')[:255],
            issued_by=_who(request.user))
        return Response(_ряд(c, request), status=status.HTTP_201_CREATED)


class CertificateRedeemView(_Кабинет):

    def post(self, request, pk: int):
        try:
            сумма = int(request.data.get('amount') or 0)
        except (TypeError, ValueError):
            сумма = 0
        if сумма <= 0:
            return _err('bad_amount', 'Укажите сумму списания.', status.HTTP_400_BAD_REQUEST)
        точка = None
        branch_id = request.data.get('branch_id')
        if branch_id:
            разрешено = user_allowed_branches(request.user, current_schema_name())
            точка = Branch.objects.filter(pk=branch_id).first()
            if точка is None or (разрешено is not None and точка.pk not in разрешено):
                return _err('bad_branch', 'Нет доступа к точке.', status.HTTP_403_FORBIDDEN)
        with transaction.atomic():
            c = GiftCertificate.objects.select_for_update().filter(pk=pk).first()
            if c is None:
                raise Http404
            if c.voided_at:
                return _err('void', 'Сертификат отменён.', status.HTTP_409_CONFLICT)
            if c.expires_at and c.expires_at < timezone.localdate():
                return _err('expired', 'Срок сертификата закончился.', status.HTTP_409_CONFLICT)
            if сумма > c.balance:
                return _err('not_enough', f'На сертификате осталось {c.balance} ₽.', status.HTTP_409_CONFLICT)
            c.balance -= сумма
            c.save(update_fields=['balance'])
            GiftCertificateUse.objects.create(certificate=c, amount=сумма, branch=точка, who=_who(request.user),
                                              comment=str(request.data.get('comment') or '')[:255])
        return Response(_ряд(c, request))


class CertificateVoidView(_Кабинет):

    def post(self, request, pk: int):
        with transaction.atomic():
            c = GiftCertificate.objects.select_for_update().filter(pk=pk).first()
            if c is None:
                raise Http404
            if not c.voided_at:
                c.voided_at = timezone.now()
                c.voided_by = _who(request.user)
                c.save(update_fields=['voided_at', 'voided_by'])
        return Response(_ряд(c, request))


class CertificatePublicView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, code: str):
        cfg = _config()
        c = GiftCertificate.objects.filter(code=code.upper()).first() if getattr(cfg, 'certificates_enabled', False) else None
        if c is None:
            raise Http404
        сеть = escape(getattr(getattr(connection, 'tenant', None), 'name', ''))
        действует = 'Отменён' if c.voided_at else (f'до {c.expires_at:%d.%m.%Y}' if c.expires_at else 'без срока')
        return HttpResponse(f'''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Подарочный сертификат</title>
<style>body{{margin:0;background:#FDF8F0;color:#1B2838;font:16px/1.5 -apple-system,Roboto,sans-serif}}
main{{max-width:420px;margin:0 auto;padding:32px 16px;text-align:center}}
.c{{background:#fff;border:1px solid #EDE8DF;border-radius:20px;padding:28px 20px}}
b{{display:block;font-size:44px;line-height:1.1;margin:12px 0}}code{{display:block;font-size:24px;letter-spacing:2px;margin:16px 0;font-weight:700;word-break:break-all}}
p{{margin:4px 0;color:#5F6B7A}}</style></head><body><main><div class="c"><p>{сеть}</p><p>Подарочный сертификат</p>
<b>{c.balance} ₽</b><p>Номинал {c.nominal} ₽ · {действует}</p><code>{c.code}</code><p>Покажите код при оплате.</p></div></main></body></html>''')
