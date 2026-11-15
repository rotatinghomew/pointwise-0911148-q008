import copy
import json
import tempfile
import unittest
from pathlib import Path

from app.breaks import common_break_scan, month_index, month_name
from app.domain import OPERATIONS, seed_dataset
from app.service import Service

AS_OF = '2026-09-30T23:59:59Z'
FAST = {'resamples': 99}


def scan(dataset, **options):
    return common_break_scan(dataset, {**FAST, **options})


def build_dataset(specs, first='2024-01', last='2026-06'):
    """按合约形状合成数据：恒定营业时长、确定性微小扰动、可选断点。"""
    lo, hi = month_index(first), month_index(last)
    activity, calendar, coverage, versions = [], [], [], []
    for m in range(lo, hi + 1):
        calendar.append({'month': month_name(m), 'promotion': 0})
    for offset, (code, spec) in enumerate(sorted(specs.items())):
        versions.append({'region_code': code, 'valid_from': first,
                         'valid_to': month_name(hi + 1), 'scale': 1.0, 'area_id': code})
        k = month_index(spec['break_month']) if spec.get('break_month') else None
        for i, m in enumerate(range(lo, hi + 1)):
            rate = spec['base'] + spec.get('trend', 0.0) * i
            if k is not None and m >= k:
                rate += spec.get('level_shift', 0.0) + spec.get('trend_shift', 0.0) * (m - k)
            wobble = ((i * 37 + offset * 11) % 11 - 5) * 0.0004
            activity.append({'region_code': code, 'month': month_name(m),
                             'value': rate * (1 + wobble) * 100, 'open_hours': 100})
            coverage.append({'region_code': code, 'month': month_name(m),
                             'status': 'observed'})
    return {'activity': activity, 'region_versions': versions,
            'calendar': calendar, 'coverage': coverage}


class SeedDataTests(unittest.TestCase):
    def setUp(self):
        self.dataset = seed_dataset()
        self.result = scan(self.dataset)

    def test_common_date_and_evidence_groups(self):
        common = self.result['common_break']
        self.assertTrue(common['significant'])
        self.assertLess(common['p_value'], 0.05)
        self.assertIn(common['date'], ('2025-08', '2025-09', '2025-10'))
        self.assertEqual(common['contributing_segments'], 7)
        headline = self.result['headline']
        self.assertEqual(headline['verdict'], 'common_break_heterogeneous')
        self.assertEqual(set(headline['supports']), {'r0', 'r1', 'r2'})
        self.assertEqual(set(headline['opposes']), {'r5', 'r6'})
        self.assertEqual(set(headline['no_detectable_shift']), {'r3', 'r4'})
        self.assertEqual(set(headline['insufficient_data']), {'r7', 'r7-realigned'})

    def test_magnitude_and_heterogeneity_are_reported(self):
        evidence = {row['area_id']: row for row in self.result['evidence']}
        up = evidence['r1']['effect_6m_pct']
        self.assertGreater(up['estimate'], 10.0)
        self.assertLess(up['lower'], up['estimate'])
        self.assertGreater(up['upper'], up['estimate'])
        down = evidence['r5']['effect_6m_pct']
        self.assertLess(down['estimate'], 0.0)
        self.assertEqual(self.result['heterogeneity']['verdict'], 'heterogeneous')
        self.assertLess(self.result['magnitude']['same_sign_share'], 1.0)
        spread = self.result['magnitude']['effect_6m_pct']
        self.assertLess(spread['min'], 0.0)
        self.assertGreater(spread['max'], 0.0)

    def test_gaps_are_not_bridged(self):
        segments = {row['area_id']: row for row in self.result['panel']['segments']}
        self.assertEqual(segments['r0']['first'], '2024-03')
        self.assertEqual(segments['r0']['months'], 28)
        gaps = {(gap['region_code'], gap['month']): gap['cause']
                for gap in self.result['guards']['gaps_not_bridged']}
        self.assertEqual(gaps[('r0', '2024-01')], 'missing')
        self.assertEqual(gaps[('r0', '2024-02')], 'closed')

    def test_boundary_change_is_not_a_break(self):
        segments = {row['area_id']: row for row in self.result['panel']['segments']}
        self.assertEqual(segments['r7']['last'], '2025-06')
        self.assertEqual(segments['r7-realigned']['first'], '2025-07')
        self.assertEqual(segments['r7']['status'], 'insufficient_data')
        self.assertEqual(segments['r7-realigned']['status'], 'insufficient_data')
        changes = self.result['guards']['boundary_changes']
        self.assertEqual([(c['region_code'], c['month']) for c in changes], [('r7', '2025-07')])
        for row in self.result['region_specific_breaks']:
            self.assertNotIn(row['area_id'], ('r7', 'r7-realigned'))

    def test_window_robustness_is_reported(self):
        reviews = self.result['window_robustness']
        self.assertEqual(len(reviews), 3)
        for entry in reviews:
            self.assertIn('date', entry)
            self.assertIn('significant', entry)
            self.assertIn('plausible_range', entry)
        self.assertFalse(self.result['robust_to_windows'])

    def test_hypothesized_month_is_evaluated_without_search(self):
        result = scan(self.dataset, hypothesized_month='2025-07')
        block = result['hypothesized_month']
        self.assertTrue(block['evaluable'])
        self.assertTrue(block['significant'])
        self.assertEqual(result['evaluation_date'], '2025-07')
        self.assertEqual(result['evaluation_basis'], 'hypothesized')

    def test_unevaluable_hypothesized_month_falls_back(self):
        result = scan(self.dataset, hypothesized_month='2024-02')
        self.assertFalse(result['hypothesized_month']['evaluable'])
        self.assertIsNotNone(result['evaluation_note'])
        self.assertEqual(result['evaluation_basis'], 'scan_estimate')

    def test_deterministic_given_same_input(self):
        self.assertEqual(self.result, scan(self.dataset))

    def test_result_is_json_safe(self):
        json.dumps(self.result, ensure_ascii=False, allow_nan=False)

    def test_source_data_is_not_mutated(self):
        dataset = seed_dataset()
        before = copy.deepcopy(dataset)
        scan(dataset)
        self.assertEqual(dataset, before)

    def test_empty_dataset_reports_insufficient_evidence(self):
        dataset = seed_dataset()
        for records in dataset.values():
            records.clear()
        result = scan(dataset)
        self.assertIsNone(result['common_break'])
        self.assertEqual(result['headline']['verdict'], 'insufficient_data')
        self.assertEqual(result['evidence'], [])


class SyntheticPanelTests(unittest.TestCase):
    def test_aligned_break_is_confirmed_as_common(self):
        specs = {f'r{i}': {'base': 10.0 + 2 * i, 'trend': 0.001 * (10.0 + 2 * i),
                           'break_month': '2025-06',
                           'level_shift': 0.1 * (10.0 + 2 * i), 'trend_shift': 0.0}
                 for i in range(4)}
        result = scan(build_dataset(specs))
        self.assertTrue(result['common_break']['significant'])
        self.assertIn(result['common_break']['date'], ('2025-05', '2025-06', '2025-07'))
        self.assertEqual(result['headline']['verdict'], 'common_break_confirmed')
        self.assertEqual(len(result['headline']['supports']), 4)
        self.assertEqual(result['heterogeneity']['verdict'], 'compatible_with_common_size')

    def test_distant_region_break_is_region_specific_not_common(self):
        specs = {f'r{i}': {'base': 10.0 + i, 'trend': 0.01, 'break_month': '2025-06',
                           'level_shift': 1.0, 'trend_shift': 0.02}
                 for i in range(4)}
        specs['r9'] = {'base': 12.0, 'trend': 0.01, 'break_month': '2026-01',
                       'level_shift': -1.5, 'trend_shift': -0.05}
        result = scan(build_dataset(specs))
        self.assertIn(result['common_break']['date'], ('2025-05', '2025-06', '2025-07'))
        specific = {row['area_id']: row for row in result['region_specific_breaks']}
        self.assertIn('r9', specific)
        self.assertIn(specific['r9']['date'], ('2025-12', '2026-01', '2026-02'))
        self.assertLess(specific['r9']['effect_6m_pct'], 0.0)
        self.assertNotIn('r0', specific)

    def test_no_break_anywhere_is_not_called_common(self):
        specs = {f'r{i}': {'base': 10.0 + i, 'trend': 0.01} for i in range(4)}
        result = scan(build_dataset(specs))
        self.assertFalse(result['common_break']['significant'])
        self.assertEqual(result['headline']['verdict'], 'no_common_break')
        self.assertEqual(result['region_specific_breaks'], [])


class ServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / 'source.json'
        self.source.write_text(Path('data/source.json').read_text())
        self.service = Service(self.source, Path(self.temporary.name) / 'state.sqlite3')

    def request(self, request_id, as_of):
        return {'request_id': request_id, 'operation': 'common_break_scan',
                'as_of': as_of, 'options': dict(FAST)}

    def test_operation_is_registered(self):
        self.assertIn('common_break_scan', self.service.describe()['operations'])
        self.assertIn('regional_series', self.service.describe()['operations'])

    def test_vintage_snapshot_is_reproducible(self):
        early = self.service.run(self.request('v-early', '2026-09-10T00:00:00Z'))
        late = self.service.run(self.request('v-late', AS_OF))
        self.assertNotEqual(early['dataset_digest'], late['dataset_digest'])
        self.assertEqual(self.service.run(self.request('v-early', '2026-09-10T00:00:00Z')), early)
        self.assertEqual(self.service.repository.analysis('v-early'), early)
        self.assertEqual(self.service.repository.analysis('v-late'), late)

    def test_old_result_survives_source_replacement(self):
        previous = self.service.run(self.request('v-keep', AS_OF))
        self.source.write_text('{}')
        self.assertEqual(self.service.run(self.request('v-keep', AS_OF)), previous)


if __name__ == '__main__':
    unittest.main()
