"""
RF-переходы за период: остыли / потеплели / без изменений / новые (ТЗ маркетолога п.4).

Каждый гость считается ОДИН раз: берётся сегмент до первого перехода периода и
сегмент после последнего. По ТЗ (16.10.2026) остывание — переход в более холодную
строку R (R3 «Свежий» → R2 «Тёплый» → R1 → R0 «Холодный»), потепление — обратное;
изменение только F — «без изменений». Новый — первый переход периода без
сегмента «до», в потепление не входит. Тёплая аудитория — строки R3 и R2.
"""
from __future__ import annotations


def rank(code: str) -> int | None:
    try:
        return int(code[1]) + int(code[3])
    except (IndexError, ValueError, TypeError):
        return None


WARM_R = 2          # R3 «Свежий» и R2 «Тёплый»


def r_of(code: str) -> int | None:
    try:
        return int(code[1])
    except (IndexError, ValueError, TypeError):
        return None


def bucket(before: str, after: str) -> str:
    if not before:
        return 'new'
    rb, ra = r_of(before), r_of(after)
    if rb is None or ra is None or ra == rb:
        return 'unchanged'
    return 'warmed' if ra > rb else 'cooled'


def classify(paths, scored_total: int, *, warm_now_unmoved: int | None = None) -> dict:
    """paths — {гость: (код «до» или '', код «после»)} → итоги, доли и подсказка.

    `warm_now_unmoved` — сколько гостей БЕЗ переходов в периоде сидит в тёплых
    строках (у них начало = конец); None — тёплую аудиторию не считаем.
    """
    out = {'warmed': 0, 'cooled': 0, 'unchanged': 0, 'new': 0, 'total': scored_total}
    тёплые_до = тёплые_после = 0
    for before, after in paths.values():
        out[bucket(before, after)] += 1
        if before and (r_of(before) or 0) >= WARM_R:
            тёплые_до += 1
        if (r_of(after) or 0) >= WARM_R:
            тёплые_после += 1
    unmoved = max(0, scored_total - len(paths))
    out['unchanged'] += unmoved
    out['start_total'] = scored_total - out['new']
    out['end_total'] = scored_total
    if warm_now_unmoved is not None:
        out['warm_start'] = тёплые_до + warm_now_unmoved
        out['warm_end'] = тёплые_после + warm_now_unmoved
    base = out['start_total'] or 0
    out['shares'] = {k: (round(out[k] * 100 / base, 1) if base else None)
                     for k in ('warmed', 'cooled', 'unchanged')}
    out['shares']['new'] = round(out['new'] * 100 / scored_total, 1) if scored_total else None
    out['conclusion'], out['advice'] = summary(out)
    return out


def summary(c: dict) -> tuple[str, str]:
    w, k, n = c['warmed'], c['cooled'], c['new']
    тёплая = (f' Тёплая аудитория на конец периода — {c["warm_end"]} гостей.'
              if c.get('warm_end') is not None else '')
    if not (w or k or n):
        return ('За период гости между сегментами не переходили.',
                'Сегменты пересчитываются ночью; если так держится неделю — проверьте, идут ли сканы QR.')
    if k > w:
        return (f'За период {k} гостей стали холоднее, {w} — теплее.{тёплая} База теряет активность.',
                'Запустите возвратную авторассылку на остывших (R2/R3) с подарком и сроком 7–10 дней '
                'и сравните с контрольной группой.')
    if w > k:
        return (f'За период {w} гостей стали теплее, {k} — холоднее.{тёплая} Активность растёт.',
                'Закрепите рост: поблагодарите потеплевших и предложите задание на второй визит.')
    return (f'Потеплело и остыло поровну: по {w}.{тёплая}',
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
    paths = _paths(qs)
    тёплые_без_переходов = scores.filter(r_score__gte=WARM_R).exclude(client_id__in=list(paths)).count()
    return classify(paths, scores.count(), warm_now_unmoved=тёплые_без_переходов)


def _paths(qs) -> dict:
    paths: dict = {}
    for guest, before, after in (qs.order_by('created_at')
                                 .values_list('client_id', 'from_segment__code', 'to_segment__code')):
        first = paths.get(guest, (before or '', None))[0]
        paths[guest] = (first, after or '')
    return paths


BUCKETS = ('warmed', 'cooled', 'unchanged_moved', 'new')


def guests(branch_ids, start, end, mode, which: str, limit: int = 200) -> list[dict]:
    """Гости группы «потеплели / остыли / новые / перешли без смены R» — для списка."""
    from apps.tenant.analytics.models import RFMigrationLog, RFMigrationLogDelivery
    from apps.tenant.branch.models import ClientBranch
    from apps.shared.guest.models import Client
    Mig = RFMigrationLogDelivery if mode == 'delivery' else RFMigrationLog
    qs = Mig.objects.filter(created_at__date__gte=start, created_at__date__lte=end, to_segment__isnull=False)
    if branch_ids:
        qs = qs.filter(client_id__in=ClientBranch.objects.filter(branch__in=branch_ids).values('client_id'))
    want = 'unchanged' if which == 'unchanged_moved' else which
    picked = {g: p for g, p in _paths(qs).items() if bucket(*p) == want}
    rows = []
    for c in Client.objects.filter(pk__in=list(picked)[:limit]):
        before, after = picked[c.pk]
        rows.append({'vk_id': c.vk_id, 'name': ' '.join(x for x in (c.first_name, c.last_name) if x),
                     'before': before, 'after': after, 'vk_url': f'https://vk.com/id{c.vk_id}'})
    return rows
