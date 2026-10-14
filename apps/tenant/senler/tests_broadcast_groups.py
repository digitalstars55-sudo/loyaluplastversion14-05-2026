"""
Часть аудитории и A/B с фиксированными группами (ТЗ маркетолога 14.10.2026, п.5).

План групп — чистая функция над аудиторией: проверяем без БД.
  1) без A/B и без ограничения — вся аудитория одним текстом, как раньше;
  2) «часть аудитории» количеством и процентом;
  3) A/B 300 + 300 из 1000 — остальные 400 не получают ничего;
  4) группы не пересекаются и одинаковы при каждом пересчёте (seed — id черновика);
  5) старый случай «проценты в сумме 100» — хвост последнему;
  6) разбор и проверки тела запроса.
"""
from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.tenant.senler.api import broadcasts as b


def _audience(sizes):
    """Точки с последовательными id гостей: sizes=[600, 400] → 1000 гостей на двух точках."""
    rows, next_id = [], 1
    for i, n in enumerate(sizes):
        rows.append({'branch_id': i + 1, 'name': f'Т{i + 1}', 'count': n,
                     'cb_ids': list(range(next_id, next_id + n))})
        next_id += n
    return {'total': sum(sizes), 'by_branch': rows}


def _draft(pk=7, variants=None, mode='', value=None, text='Привет'):
    return SimpleNamespace(pk=pk, variants=variants or [], message_text=text,
                           audience_limit_mode=mode, audience_limit_value=value)


def _ids(group):
    return {cb for _, cbs in group['by_branch'] for cb in cbs}


class PlanGroupsTest(SimpleTestCase):

    def test_вся_аудитория_как_раньше(self):
        plan = b.plan_groups(_draft(), _audience([600, 400]))
        self.assertEqual((plan['available'], plan['planned']), (1000, 1000))
        self.assertEqual(len(plan['groups']), 1)
        self.assertEqual(plan['groups'][0]['text'], 'Привет')

    def test_часть_аудитории_количеством(self):
        plan = b.plan_groups(_draft(mode='count', value=250), _audience([600, 400]))
        self.assertEqual(plan['planned'], 250)
        self.assertEqual(len(_ids(plan['groups'][0])), 250)

    def test_часть_аудитории_процентом(self):
        plan = b.plan_groups(_draft(mode='percent', value=10), _audience([600, 400]))
        self.assertEqual(plan['planned'], 100)

    def test_количество_больше_аудитории(self):
        plan = b.plan_groups(_draft(mode='count', value=5000), _audience([10]))
        self.assertEqual(plan['planned'], 10)

    def test_ab_300_300_остальные_ничего(self):
        variants = [{'count': 300, 'message_text': 'A'}, {'count': 300, 'message_text': 'B'}]
        plan = b.plan_groups(_draft(variants=variants), _audience([600, 400]))
        a, bb = plan['groups']
        self.assertEqual((a['count'], bb['count'], plan['planned']), (300, 300, 600))
        self.assertEqual(plan['available'] - plan['planned'], 400)
        self.assertFalse(_ids(a) & _ids(bb))
        self.assertEqual((a['text'], bb['text']), ('A', 'B'))

    def test_группы_фиксированы_при_пересчёте(self):
        variants = [{'percent': 30, 'message_text': 'A'}, {'percent': 30, 'message_text': 'B'}]
        one = b.plan_groups(_draft(variants=variants), _audience([600, 400]))
        two = b.plan_groups(_draft(variants=variants), _audience([600, 400]))
        self.assertEqual([_ids(g) for g in one['groups']], [_ids(g) for g in two['groups']])
        other = b.plan_groups(_draft(pk=8, variants=variants), _audience([600, 400]))
        self.assertNotEqual(_ids(one['groups'][0]), _ids(other['groups'][0]))

    def test_сумма_100_хвост_последнему(self):
        variants = [{'percent': 33, 'message_text': 'A'}, {'percent': 67, 'message_text': 'B'}]
        plan = b.plan_groups(_draft(variants=variants), _audience([7]))
        self.assertEqual(plan['planned'], 7)

    def test_точка_гостя_сохраняется(self):
        variants = [{'count': 50, 'message_text': 'A'}, {'count': 50, 'message_text': 'B'}]
        aud = _audience([600, 400])
        plan = b.plan_groups(_draft(variants=variants), aud)
        own = {cb: row['branch_id'] for row in aud['by_branch'] for cb in row['cb_ids']}
        for g in plan['groups']:
            for row, cbs in g['by_branch']:
                self.assertTrue(all(own[cb] == row['branch_id'] for cb in cbs))


class PayloadTest(SimpleTestCase):

    def test_лимит(self):
        self.assertEqual(b._parse_audience_limit({'mode': 'count', 'value': '300'}), ('count', 300))
        self.assertEqual(b._parse_audience_limit(None), ('', None))
        for bad in ({'mode': 'percent', 'value': 101}, {'mode': 'count', 'value': 0}, {'mode': 'x', 'value': 1}):
            with self.assertRaises(b.PayloadError):
                b._parse_audience_limit(bad)

    def test_варианты_количеством(self):
        v = b._parse_variants([{'count': '300', 'message_text': ' A '}, {'count': 300, 'message_text': 'B'}])
        self.assertEqual(v[0], {'count': 300, 'message_text': 'A'})
        self.assertIsNone(b._validate_texts('', v))

    def test_смешивать_количество_и_процент_нельзя(self):
        v = b._parse_variants([{'count': 300, 'message_text': 'A'}, {'percent': 30, 'message_text': 'B'}])
        self.assertIn('одинаково', b._validate_texts('', v))

    def test_проценты_меньше_100_можно(self):
        v = b._parse_variants([{'percent': 30, 'message_text': 'A'}, {'percent': 30, 'message_text': 'B'}])
        self.assertIsNone(b._validate_texts('', v))


class ControlAndVariantImageTest(SimpleTestCase):
    """Волна 2 (14.10.2026): контрольная группа и своя картинка у варианта A/B."""

    def test_контрольная_группа_процентом(self):
        d = _draft()
        d.control_percent = 10
        plan = b.plan_groups(d, _audience([600, 400]))
        self.assertEqual((plan['planned'], len(plan['control_cb_ids'])), (900, 100))
        self.assertFalse(_ids(plan['groups'][0]) & set(plan['control_cb_ids']))

    def test_хвост_части_аудитории_контрольная(self):
        plan = b.plan_groups(_draft(mode='count', value=300), _audience([1000]))
        self.assertEqual(len(plan['control_cb_ids']), 700)

    def test_вся_аудитория_без_контроля(self):
        self.assertEqual(b.plan_groups(_draft(), _audience([50]))['control_cb_ids'], [])

    def test_картинка_варианта_только_своя(self):
        out = b._parse_variants([
            {'count': 10, 'message_text': 'A', 'image': 'broadcasts/variants/a1.png'},
            {'count': 10, 'message_text': 'B', 'image': '../../etc/passwd'},
            {'count': 10, 'message_text': 'C', 'image': 'broadcasts/variants/x/../y.png'},
        ])
        self.assertEqual([v.get('image') for v in out], ['broadcasts/variants/a1.png', None, None])

    def test_группа_несёт_картинку(self):
        d = _draft(variants=[{'count': 5, 'message_text': 'A', 'image': 'broadcasts/variants/a.png'},
                             {'count': 5, 'message_text': 'B'}])
        groups = b.plan_groups(d, _audience([20]))['groups']
        self.assertEqual([g['image'] for g in groups], ['broadcasts/variants/a.png', ''])
