"""
RF-переходы за период: остыли / потеплели / без изменений / новые (ТЗ маркетолога п.4).

Каждый гость считается ОДИН раз: берётся сегмент до первого перехода периода и
сегмент после последнего. Сила сегмента — R + F из кода вида «R3F2». Новый —
первый переход периода без сегмента «до». Без изменений — гости с RF-оценкой,
у которых за период переходов не было (или ушли и вернулись в ту же силу).
"""
from __future__ import annotations


def rank(code: str) -> int | None:
    try:
        return int(code[1]) + int(code[3])
    except (IndexError, ValueError, TypeError):
        return None


def classify(paths, scored_total: int) -> dict:
    """paths — {гость: (код «до» или '', код «после»)} → итоги и подсказка."""
    out = {'warmed': 0, 'cooled': 0, 'unchanged': 0, 'new': 0, 'total': scored_total}
    moved_same = 0
    for before, after in paths.values():
        if not before:
            out['new'] += 1
            continue
        rb, ra = rank(before), rank(after)
        if rb is None or ra is None or ra == rb:
            moved_same += 1
        elif ra > rb:
            out['warmed'] += 1
        else:
            out['cooled'] += 1
    out['unchanged'] = max(0, scored_total - len(paths)) + moved_same
    out['conclusion'], out['advice'] = summary(out)
    return out


def summary(c: dict) -> tuple[str, str]:
    w, k, n = c['warmed'], c['cooled'], c['new']
    if not (w or k or n):
        return ('За период гости между сегментами не переходили.',
                'Сегменты пересчитываются ночью; если так держится неделю — проверьте, идут ли сканы QR.')
    if k > w:
        return (f'Остыло больше, чем потеплело: {k} против {w}. База теряет активность.',
                'Запустите возвратную авторассылку на остывших (R2/R3) с подарком и сроком 7–10 дней '
                'и сравните с контрольной группой.')
    if w > k:
        return (f'Потеплело больше, чем остыло: {w} против {k}. Активность растёт.',
                'Закрепите рост: поблагодарите потеплевших и предложите задание на второй визит.')
    return (f'Потеплело и остыло поровну: по {w}.',
            'Посмотрите, из каких сегментов уходят чаще, и начните рассылку с них.')


def build(branch_ids, start, end, mode='restaurant') -> dict:
    from apps.tenant.analytics.models import (
        GuestRFScore, GuestRFScoreDelivery, RFMigrationLog, RFMigrationLogDelivery,
    )
    from apps.tenant.branch.models import ClientBranch

    Mig = RFMigrationLogDelivery if mode == 'delivery' else RFMigrationLog
    Score = GuestRFScoreDelivery if mode == 'delivery' else GuestRFScore
    qs = Mig.objects.filter(created_at__date__gte=start, created_at__date__lte=end, to_segment__isnull=False)
    scores = Score.objects.all()
    if branch_ids:
        guests = ClientBranch.objects.filter(branch__in=branch_ids).values('client_id')
        qs = qs.filter(client_id__in=guests)
        scores = scores.filter(client_id__in=guests)
    paths: dict = {}
    for guest, before, after in (qs.order_by('created_at')
                                 .values_list('client_id', 'from_segment__code', 'to_segment__code')):
        first = paths.get(guest, (before or '', None))[0]
        paths[guest] = (first, after or '')
    return classify(paths, scores.count())
