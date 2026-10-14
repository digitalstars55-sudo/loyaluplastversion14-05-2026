"""
Скан QR фискального чека (волна 2 ТЗ маркетолога, 14.10.2026).

Строка QR чека по 54-ФЗ: `t=20261014T1530&s=1250.00&fn=7380440700123456&i=12345&fp=1234567890&n=1`
  t  — дата и время (местное время кассы), s — сумма, fn — фискальный накопитель,
  i  — номер фискального документа, fp — фискальный признак, n — 1 приход, 2 возврат.

Правила зачёта:
  1) только приход (n=1), не в будущем, не старше `receipt_max_age_hours`;
  2) пара (fn, i) засчитывается один раз на всю сеть;
  3) точка — по ФН из `Branch.fiscal_fns`; незнакомый ФН — сохраняем без баллов
     (по таким строкам видно, какую кассу добавить точке), повторный скан тем же
     гостем после добавления ФН засчитывается;
  4) не больше `receipt_daily_limit` засчитанных чеков у гостя за сутки;
  5) баллы = сумма × `receipt_cashback_percent` / 100 вниз, на счёт точки чека.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs

from django.db import IntegrityError, transaction
from django.utils import timezone

FUTURE_SLACK = timedelta(minutes=10)
_FN_RE = re.compile(r'\d{16}')


class ReceiptError(Exception):
    def __init__(self, code: str, detail: str, http_status: int = 400):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.http_status = http_status


@dataclass
class ParsedReceipt:
    when: datetime
    amount: Decimal
    fn: str
    fd: str
    fp: str
    kind: str
    raw: str


def parse_qr(raw: str) -> ParsedReceipt:
    """Разбор строки QR чека; ошибка формата — ReceiptError('qr_invalid')."""
    text = (raw or '').strip()
    bad = ReceiptError('qr_invalid', 'Это не QR-код кассового чека.')
    if not text or len(text) > 255:
        raise bad
    q = {k.lower(): v[0].strip() for k, v in parse_qs(text, keep_blank_values=True).items() if v}
    t, s, fn, fd = q.get('t', ''), q.get('s', ''), q.get('fn', ''), q.get('i', '')
    if not (t and s and fn.isdigit() and fd.isdigit()):
        raise bad
    # Строго по длине: strptime съел бы «1530» как 15:03:00.
    fmt = {13: '%Y%m%dT%H%M', 15: '%Y%m%dT%H%M%S'}.get(len(t))
    if not fmt or not t.replace('T', '', 1).isdigit():
        raise bad
    try:
        when = datetime.strptime(t, fmt)
    except ValueError:
        raise bad
    try:
        amount = Decimal(s.replace(',', '.')).quantize(Decimal('0.01'))
    except InvalidOperation:
        raise bad
    if amount <= 0:
        raise bad
    return ParsedReceipt(
        when=timezone.make_aware(when, timezone.get_current_timezone()),
        amount=amount, fn=fn, fd=fd, fp=q.get('fp', ''), kind=q.get('n', '1'), raw=text,
    )


def branch_fns(text: str) -> set[str]:
    return set(_FN_RE.findall(text or ''))


def find_branch(fn: str):
    from apps.tenant.branch.models import Branch
    for branch in Branch.objects.filter(is_active=True).exclude(fiscal_fns=''):
        if fn in branch_fns(branch.fiscal_fns):
            return branch
    return None


def coins_for(amount: Decimal, percent: int) -> int:
    if percent <= 0:
        return 0
    return int(amount * percent // 100)


def process_receipt(client, raw: str, config, now=None) -> dict:
    """
    Засчитать чек гостю. Возвращает {status, coins, amount, branch, receipt_at}
    или поднимает ReceiptError с кодом для мини-приложения.
    """
    from apps.tenant.branch.models import (
        ClientBranch, CoinTransaction, ReceiptScan, ReceiptScanStatus,
        TransactionSource, TransactionType,
    )

    now = now or timezone.now()
    r = parse_qr(raw)
    if r.kind != '1':
        raise ReceiptError('not_sale', 'Это чек возврата — баллы начисляются только за покупку.')
    if r.when > now + FUTURE_SLACK:
        raise ReceiptError('qr_invalid', 'Время в чеке ещё не наступило.')
    max_age = timedelta(hours=int(getattr(config, 'receipt_max_age_hours', 48) or 48))
    if now - r.when > max_age:
        raise ReceiptError('too_old', f'Чек старше {int(max_age.total_seconds() // 3600)} ч — баллы за него уже не начислить.')

    existing = ReceiptScan.objects.filter(fn=r.fn, fd=r.fd).first()
    if existing is not None:
        if existing.client_id != client.pk:
            raise ReceiptError('duplicate', 'Этот чек уже отсканировал другой гость.', 409)
        if existing.status == ReceiptScanStatus.CREDITED:
            raise ReceiptError('already_yours', 'Вы уже отсканировали этот чек.', 409)

    branch = find_branch(r.fn)
    if branch is None:
        if existing is None:
            try:
                ReceiptScan.objects.create(
                    client=client, fn=r.fn, fd=r.fd, fp=r.fp, receipt_at=r.when, amount=r.amount,
                    status=ReceiptScanStatus.UNKNOWN_FN, raw=r.raw,
                )
            except IntegrityError:
                raise ReceiptError('duplicate', 'Этот чек уже отсканировали.', 409)
        return {'status': ReceiptScanStatus.UNKNOWN_FN, 'coins': 0, 'amount': str(r.amount),
                'branch': None, 'receipt_at': r.when.isoformat()}

    limit = int(getattr(config, 'receipt_daily_limit', 3) or 0)
    if limit:
        today = ReceiptScan.objects.filter(
            client=client, status=ReceiptScanStatus.CREDITED, created_at__gte=now - timedelta(hours=24),
        ).count()
        if today >= limit:
            raise ReceiptError('daily_limit', f'За сутки можно засчитать не больше {limit} чеков.', 429)

    coins = coins_for(r.amount, int(getattr(config, 'receipt_cashback_percent', 0) or 0))
    try:
        with transaction.atomic():
            if existing is not None:
                scan = ReceiptScan.objects.select_for_update().get(pk=existing.pk)
                if scan.status == ReceiptScanStatus.CREDITED:
                    raise ReceiptError('already_yours', 'Вы уже отсканировали этот чек.', 409)
            else:
                scan = ReceiptScan(client=client, fn=r.fn, fd=r.fd, fp=r.fp, receipt_at=r.when,
                                   amount=r.amount, raw=r.raw)
            scan.branch = branch
            scan.status = ReceiptScanStatus.CREDITED
            scan.coins = coins
            scan.save()
            if coins:
                cb, _ = ClientBranch.objects.get_or_create(client=client, branch=branch)
                CoinTransaction.objects.create_transfer(
                    cb, coins, TransactionType.INCOME, TransactionSource.RECEIPT,
                    description=f'Чек ФН {r.fn} №{r.fd}',
                )
    except IntegrityError:
        raise ReceiptError('duplicate', 'Этот чек уже отсканировали.', 409)
    return {'status': ReceiptScanStatus.CREDITED, 'coins': coins, 'amount': str(r.amount),
            'branch': {'id': branch.branch_id, 'name': branch.name}, 'receipt_at': r.when.isoformat()}
