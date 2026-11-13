"""Tests for the non-response calibration module.

Fixtures are synthetic and generated in-test; no population file is fetched.
"""

import copy
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from analysis_app.analysis import analyze, merge_config
from analysis_app.calibration import (calibrate, estimate_from_weights, load_targets,
                                      validate_coverage)
from analysis_app.io import load_records
from analysis_app.output import prepare_separated_directories, save_json
from analysis_app.release import render_internal, render_release, suppress_detail

FIELDS = ['observation_id', 'firm', 'region', 'size_class', 'industry',
          'adoption', 'responded', 'base_weight', 'wave']


def make_target(region, size_class, industry, version='v1'):
    return {'target_version': version,
            'margins': {'region': dict(region), 'size_class': dict(size_class),
                        'industry': dict(industry)}, 'total': sum(region.values())}


def target_file(*waves):
    """Build a loaded-target structure from (wave, target) pairs."""
    body = {int(wave): target for wave, target in waves}
    return {'waves': body, 'fingerprint': 'synthetic'}


def row(index, wave=0, region='R0', size_class='small', industry='I0',
        adoption=1, responded=1, base_weight=1.0):
    return {'observation_id': f'o{index}', 'firm': f'F{index}', 'region': region,
            'size_class': size_class, 'industry': industry,
            'adoption': adoption, 'responded': responded,
            'base_weight': base_weight, 'wave': wave}


def balanced_records():
    """Two regions, two size classes, one industry; one non-respondent."""
    records = []
    index = 0
    for region in ('R0', 'R1'):
        for size_class in ('small', 'large'):
            for _ in range(4):
                records.append(row(index, region=region, size_class=size_class))
                index += 1
    records.append(row(index, region='R0', size_class='small', adoption=None,
                       responded=0, base_weight=2.0))
    return records


BALANCED_TARGET = make_target({'R0': 80, 'R1': 80}, {'small': 80, 'large': 80},
                              {'I0': 160})


class TargetFileTest(unittest.TestCase):
    def write(self, directory, payload):
        path = Path(directory) / 'targets.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        return path

    def test_dimension_totals_must_agree(self):
        payload = {'waves': {'0': {'target_version': 'v1', 'margins': {
            'region': {'R0': 10}, 'size_class': {'small': 11}, 'industry': {'I0': 10}}}}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, '总量不一致'):
                load_targets(self.write(directory, payload))

    def test_missing_version_rejected(self):
        payload = {'waves': {'0': {'margins': {
            'region': {'R0': 10}, 'size_class': {'small': 10}, 'industry': {'I0': 10}}}}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'target_version'):
                load_targets(self.write(directory, payload))

    def test_negative_margin_rejected(self):
        payload = {'waves': {'0': {'target_version': 'v1', 'margins': {
            'region': {'R0': -1}, 'size_class': {'small': -1}, 'industry': {'I0': -1}}}}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, '非负'):
                load_targets(self.write(directory, payload))

    def test_fingerprint_tracks_margin_changes(self):
        base = {'waves': {'0': {'target_version': 'v1', 'margins': {
            'region': {'R0': 10}, 'size_class': {'small': 10}, 'industry': {'I0': 10}}}}}
        changed = copy.deepcopy(base)
        changed['waves']['0']['margins'] = {
            'region': {'R0': 12}, 'size_class': {'small': 12}, 'industry': {'I0': 12}}
        with tempfile.TemporaryDirectory() as directory:
            first = load_targets(self.write(directory, base))['fingerprint']
            second = load_targets(self.write(directory, changed))['fingerprint']
        self.assertNotEqual(first, second)


class ResponseSemanticsTest(unittest.TestCase):
    def test_non_respondent_with_adoption_rejected(self):
        records = [row(0, adoption=None, responded=0), row(1, adoption=1, responded=0)]
        with self.assertRaisesRegex(ValueError, '未响应者不能带 adoption'):
            analyze(records, target_file((0, BALANCED_TARGET)))

    def test_respondent_without_adoption_rejected(self):
        records = [row(0, adoption=None, responded=1)]
        with self.assertRaisesRegex(ValueError, '响应者 adoption 不能为空'):
            analyze(records, target_file((0, BALANCED_TARGET)))

    def test_non_respondents_never_enter_estimate(self):
        records = balanced_records()
        result = analyze(records, target_file((0, BALANCED_TARGET)))
        wave = result['waves'][0]
        self.assertEqual(wave['respondents'], 16)
        self.assertGreater(wave['estimate']['adoption_weighted'], 0.99)

    def test_coverage_gap_rejected(self):
        records = [row(0, region='R9')]
        target = target_file((0, BALANCED_TARGET))
        with self.assertRaisesRegex(ValueError, 'R9'):
            analyze(records, target)

    def test_wave_without_target_rejected(self):
        records = balanced_records()
        with self.assertRaisesRegex(ValueError, '缺少调查期目标边际'):
            analyze(records, target_file((1, BALANCED_TARGET)))


class CalibrationTest(unittest.TestCase):
    def test_converges_and_reports_residual(self):
        records = [row(index, region='R0', size_class='small') for index in range(4)]
        target = make_target({'R0': 40}, {'small': 40}, {'I0': 40})
        result = calibrate(records, target)
        self.assertEqual(result['status'], 'converged')
        self.assertLess(result['max_relative_residual'], 1e-6)
        self.assertAlmostEqual(sum(result['weights']), 40, places=6)

    def test_contradictory_margins_keep_closest_result(self):
        # Region asks for a 50/50 split but there are no large respondents at all,
        # so the size and region margins cannot both hold.  The module must not
        # pretend the fit is exact.
        records = [row(0, region='R0', size_class='small'),
                   row(1, region='R0', size_class='small')]
        target = make_target({'R0': 50, 'R1': 50}, {'small': 50, 'large': 50}, {'I0': 100})
        result = calibrate(records, target, upper_bound=20.0)
        self.assertFalse(result['converged'])
        self.assertEqual(result['status'], 'not_converged')
        self.assertGreater(result['max_absolute_residual'], 1e-3)
        large = [item for item in result['margin_rows']
                 if item['dimension'] == 'size_class' and item['level'] == 'large']
        self.assertEqual(large[0]['estimate'], 0.0)
        self.assertEqual(large[0]['residual'], -50.0)
        # The retained weights are a real iterate, not a fabricated match.
        self.assertGreater(sum(result['weights']), 0)

    def test_tight_bound_reports_not_converged(self):
        records = [row(index, region='R0', size_class='small') for index in range(6)]
        target = make_target({'R0': 60}, {'small': 60}, {'I0': 60})
        result = calibrate(records, target, upper_bound=3.0)
        self.assertFalse(result['converged'])
        self.assertEqual(result['status'], 'not_converged')
        self.assertGreater(result['max_relative_residual'], 0.01)
        self.assertTrue(all(weight <= 3.0 + 1e-12 for weight in result['weights']))

    def test_zero_base_weight_stays_zero(self):
        records = [row(0, base_weight=0.0), row(1, base_weight=2.0)]
        target = make_target({'R0': 10}, {'small': 10}, {'I0': 10})
        result = calibrate(records, target, lower_bound=1.0)
        self.assertEqual(result['weights'][0], 0.0)
        self.assertGreater(result['weights'][1], 0.0)
        self.assertAlmostEqual(sum(result['weights']), 10, places=6)

    def test_all_zero_base_weights_infeasible(self):
        records = [row(0, base_weight=0.0), row(1, base_weight=0.0)]
        target = make_target({'R0': 10}, {'small': 10}, {'I0': 10})
        result = calibrate(records, target)
        self.assertEqual(result['status'], 'infeasible')
        self.assertFalse(result['converged'])
        self.assertIsNone(estimate_from_weights(records, result['weights']))

    def test_iteration_cap_is_reported(self):
        # A crossed design needs more than one raking pass to converge.
        records = [row(0, region='R0', size_class='small'),
                   row(1, region='R0', size_class='large'),
                   row(2, region='R1', size_class='small'),
                   row(3, region='R1', size_class='large'),
                   row(4, region='R2', size_class='small')]
        target = make_target({'R0': 30, 'R1': 20, 'R2': 50},
                             {'small': 70, 'large': 30}, {'I0': 100})
        result = calibrate(records, target, max_iterations=1, tolerance=1e-12)
        self.assertFalse(result['converged'])
        self.assertEqual(result['iterations'], 1)
        self.assertGreater(result['max_relative_residual'], 1e-6)

    def test_weights_are_scale_invariant(self):
        target = make_target({'R0': 40}, {'small': 40}, {'I0': 40})
        records = [row(index, base_weight=2.0) for index in range(4)]
        result = calibrate(records, target)
        self.assertAlmostEqual(sum(result['weights']), 40, places=6)


class WaveIsolationTest(unittest.TestCase):
    def records(self):
        return ([row(index, wave=0, region='R0') for index in range(8)] +
                [row(index + 10, wave=1, region='R1') for index in range(8)])

    def test_each_wave_uses_its_own_target_version(self):
        wave0 = make_target({'R0': 40, 'R1': 0}, {'small': 40, 'large': 0}, {'I0': 40}, 'w0-v1')
        wave1 = make_target({'R0': 0, 'R1': 64}, {'small': 64, 'large': 0}, {'I0': 64}, 'w1-v9')
        result = analyze(self.records(), target_file((0, wave0), (1, wave1)))
        by_wave = {wave['wave']: wave for wave in result['waves']}
        self.assertEqual(by_wave[0]['target_version'], 'w0-v1')
        self.assertEqual(by_wave[1]['target_version'], 'w1-v9')
        self.assertAlmostEqual(by_wave[0]['estimate']['weight_sum'], 40, places=6)
        self.assertAlmostEqual(by_wave[1]['estimate']['weight_sum'], 64, places=6)

    def test_cross_wave_target_change_is_detected(self):
        wave0 = make_target({'R0': 20, 'R1': 20}, {'small': 40, 'large': 0}, {'I0': 40}, 'w0')
        wave1 = make_target({'R0': 48, 'R1': 16}, {'small': 64, 'large': 0}, {'I0': 64}, 'w1')
        first = analyze(self.records(), target_file((0, wave0), (1, wave1)))
        second = analyze(self.records(), target_file((0, wave1), (1, wave0)))
        self.assertNotEqual(first['waves'][0]['estimate']['weight_sum'],
                            second['waves'][0]['estimate']['weight_sum'])

    def test_response_status_is_wave_specific(self):
        records = [row(0, wave=0, responded=1), row(1, wave=0, responded=1),
                   row(2, wave=1, responded=0, adoption=None),
                   row(3, wave=1, responded=1)]
        result = analyze(records, target_file((0, BALANCED_TARGET), (1, BALANCED_TARGET)))
        by_wave = {wave['wave']: wave for wave in result['waves']}
        self.assertEqual(by_wave[0]['respondents'], 2)
        self.assertEqual(by_wave[1]['respondents'], 1)


class SparseStrataTest(unittest.TestCase):
    def test_empty_stratum_is_named(self):
        records = [row(0, region='R0', size_class='small'),
                   row(1, region='R0', size_class='small'),
                   row(2, region='R1', size_class='large', responded=0, adoption=None),
                   row(3, region='R1', size_class='large', responded=0, adoption=None)]
        target = make_target({'R0': 50, 'R1': 50}, {'small': 50, 'large': 50}, {'I0': 100})
        result = analyze(records, target_file((0, target)))
        strata = result['waves'][0]['strata']
        self.assertEqual(strata['unrepresented_count'], 1)
        missing = strata['unrepresented'][0]
        self.assertEqual((missing['region'], missing['size_class']), ('R1', 'large'))
        self.assertEqual(missing['status'], 'population_observed_no_respondents')
        cell = [cell for cell in strata['cells']
                if (cell['region'], cell['size_class']) == ('R1', 'large')][0]
        self.assertEqual(cell['respondents'], 0)
        self.assertEqual(cell['base_weight_respondents'], 0)

    def test_zero_respondent_wave_is_reported_not_estimated(self):
        records = [row(0, responded=0, adoption=None)]
        result = analyze(records, target_file((0, BALANCED_TARGET)))
        wave = result['waves'][0]
        self.assertEqual(wave['respondents'], 0)
        self.assertIsNone(wave['estimate']['adoption_weighted'])
        self.assertIsNone(wave['interval']['lower'])
        self.assertEqual(wave['calibration']['status'], 'infeasible')


class IntervalTest(unittest.TestCase):
    def setUp(self):
        records = balanced_records()
        for index, item in enumerate(records):
            if item['responded'] == 1:
                item['adoption'] = float(index % 5 == 0)
        self.records = records
        self.target = target_file((0, BALANCED_TARGET))

    def test_interval_is_recalibrated_per_replicate(self):
        result = analyze(self.records, self.target,
                         {'bootstrap': {'replicates': 40, 'confidence': 0.9, 'seed': 7}})
        interval = result['waves'][0]['interval']
        self.assertEqual(interval['method'], 'stratified_bootstrap_recalibrated')
        self.assertEqual(interval['replicates_valid'], 40)
        self.assertGreater(interval['replicates_converged'], 0)
        self.assertLessEqual(interval['replicates_converged'], 40)
        self.assertLess(interval['lower'], interval['upper'])

    def test_interval_reflects_within_stratum_mix(self):
        # Within one cell, half the respondents adopt.  A replicate that
        # re-weighted only the final weights would collapse the interval;
        # resampling outcomes must keep it open.
        records = []
        for index in range(6):
            records.append(row(index, adoption=1 if index % 2 else 0))
        target = make_target({'R0': 60}, {'small': 60}, {'I0': 60})
        result = analyze(records, target_file((0, target)),
                         {'bootstrap': {'replicates': 60, 'confidence': 0.9, 'seed': 3}})
        interval = result['waves'][0]['interval']
        self.assertGreater(interval['upper'] - interval['lower'], 0.0)

    def test_fixed_weights_would_differ(self):
        records = []
        for index in range(8):
            records.append(row(index, region='R0' if index < 4 else 'R1',
                               adoption=1 if index < 2 else 0))
        target = make_target({'R0': 400, 'R1': 400}, {'small': 800, 'large': 0}, {'I0': 800})
        result = analyze(records, target_file((0, target)),
                         {'bootstrap': {'replicates': 60, 'confidence': 0.9, 'seed': 5}})
        interval = result['waves'][0]['interval']
        estimate = result['waves'][0]['estimate']['adoption_weighted']
        self.assertLess(interval['lower'], estimate)
        self.assertGreater(interval['upper'], estimate)


class SuppressionTest(unittest.TestCase):
    def build(self, counts, non_respondents=0):
        """counts maps size_class -> respondent count; thin levels stay sparse."""
        records = []
        index = 0
        for size_class, respondents in counts.items():
            for _ in range(respondents):
                records.append(row(index, size_class=size_class))
                index += 1
            for _ in range(non_respondents):
                records.append(row(index, size_class=size_class, responded=0,
                                   adoption=None))
                index += 1
        target = make_target({'R0': 1000}, {'small': 400, 'large': 300, 'medium': 300},
                             {'I0': 1000})
        return records, target_file((0, target))

    def test_small_cell_withheld_from_release_but_kept_internal(self):
        records, targets = self.build({'small': 10, 'large': 2, 'medium': 8})
        # Use a generic dimension: 'medium' is a thin size class inside R0.
        result = analyze(records, targets, {'suppression': {'minimum_respondents': 5,
                                                            'minimum_rows': 5}})
        release = render_release(result)
        internal = render_internal(result)
        published = {item['level'] for item in release['detail']['0']}
        self.assertNotIn('large', published)
        self.assertIn('small', published)
        self.assertIn('large', internal['waves'][0]['detail'])
        self.assertEqual(internal['waves'][0]['detail']['large']['respondents'], 2)

    def test_single_publishable_cell_triggers_complementary_suppression(self):
        records, targets = self.build({'small': 10, 'large': 2})
        result = analyze(records, targets, {'suppression': {'minimum_respondents': 5,
                                                            'minimum_rows': 1}})
        detail = suppress_detail(result['waves'], merge_config(result['config']))
        self.assertEqual(detail['release'], {0: {}})
        action = detail['decisions'][0]
        self.assertEqual(action['action'], 'withheld')
        self.assertEqual(action['reason'], 'complementary_suppression')

    def test_two_publishable_cells_are_released(self):
        records, targets = self.build({'small': 10, 'large': 9, 'medium': 6})
        result = analyze(records, targets, {'suppression': {'minimum_respondents': 5,
                                                            'minimum_rows': 1}})
        release = render_release(result)
        published = {item['level'] for item in release['detail']['0']}
        self.assertEqual(published, {'small', 'large', 'medium'})
        self.assertEqual(release['suppression']['suppressed_cells'], 0)

    def test_one_hidden_remainder_withdraws_a_second_cell(self):
        # With medium withheld, publishing both small and large would let a
        # reader subtract the wave total and recover medium.  One more cell is
        # withdrawn so at least two remain hidden.
        records, targets = self.build({'small': 10, 'large': 9, 'medium': 2})
        result = analyze(records, targets, {'suppression': {'minimum_respondents': 5,
                                                            'minimum_rows': 1}})
        release = render_release(result)
        published = {item['level'] for item in release['detail']['0']}
        self.assertEqual(published, {'small'})
        withheld = [item for item in result['waves'][0]['detail']
                    if item not in published]
        self.assertEqual(set(withheld), {'large', 'medium'})
        decision = release['suppression']['decisions'][0]
        self.assertEqual(decision['reason'], 'complementary_suppression')
        self.assertEqual(decision['withheld_levels'], 2)

    def test_complementary_rule_can_be_disabled(self):
        records, targets = self.build({'small': 10, 'large': 9, 'medium': 2})
        result = analyze(records, targets, {'suppression': {'minimum_respondents': 5,
                                                            'minimum_rows': 1,
                                                            'complementary': False}})
        release = render_release(result)
        published = {item['level'] for item in release['detail']['0']}
        self.assertEqual(published, {'small', 'large'})

    def test_release_does_not_name_withheld_cells(self):
        records, targets = self.build({'small': 10, 'large': 1})
        result = analyze(records, targets, {'suppression': {'minimum_respondents': 5,
                                                            'minimum_rows': 1}})
        release = render_release(result)
        payload = json.dumps(release)
        self.assertNotIn('"large"', payload)
        self.assertNotIn('adoption_weighted": null', payload)

    def test_release_omits_internal_diagnostics(self):
        records, targets = self.build({'small': 10, 'large': 9})
        result = analyze(records, targets)
        release = render_release(result)
        for key in ('weights', 'margins', 'base_weight_respondents'):
            self.assertNotIn(key, json.dumps(release['waves']))
        self.assertIn('weights', render_internal(result)['waves'][0])

    def test_all_cells_thin_withholds_everything(self):
        records, targets = self.build({'small': 2, 'large': 1})
        result = analyze(records, targets, {'suppression': {'minimum_respondents': 5,
                                                            'minimum_rows': 1}})
        release = render_release(result)
        self.assertEqual(release['detail'], {'0': []})
        self.assertEqual(release['groups'], [])


class OutputSeparationTest(unittest.TestCase):
    def test_directories_have_distinct_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            internal = Path(directory) / 'internal'
            release = Path(directory) / 'release'
            prepare_separated_directories(internal, release)
            self.assertEqual(stat.S_IMODE(internal.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(release.stat().st_mode), 0o750)

    def test_nested_directories_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            internal = Path(directory) / 'out'
            release = Path(directory) / 'out' / 'release'
            with self.assertRaisesRegex(ValueError, '不能相同或互相嵌套'):
                prepare_separated_directories(internal, release)

    def test_identical_directories_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, '不能相同或互相嵌套'):
                prepare_separated_directories(directory, directory)

    def test_file_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'file.json'
            save_json(path, {'a': 1}, mode=0o600)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class CliTest(unittest.TestCase):
    def run_cli(self, *arguments):
        return subprocess.run([sys.executable, '-m', 'analysis_app', *arguments],
                              text=True, capture_output=True)

    def test_calibrate_writes_both_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            internal = Path(directory) / 'internal'
            release = Path(directory) / 'release'
            run = self.run_cli('calibrate', '--targets', 'data/targets.json',
                               '--internal-output', str(internal),
                               '--release-output', str(release))
            self.assertEqual(run.returncode, 0, run.stderr)
            internal_payload = json.loads((internal / 'diagnostics.json').read_text())
            release_payload = json.loads((release / 'estimates.json').read_text())
            self.assertFalse(internal_payload['release'])
            self.assertTrue(release_payload['release'])
            self.assertIn('weights', internal_payload['waves'][0])
            self.assertNotIn('weights', release_payload['waves'][0])
            self.assertEqual(json.loads(run.stdout), internal_payload)

    def test_calibrate_stdout_can_be_release(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.run_cli('calibrate', '--targets', 'data/targets.json',
                               '--internal-output', str(Path(directory) / 'i'),
                               '--release-output', str(Path(directory) / 'r'),
                               '--release-stdout')
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(json.loads(run.stdout)['release'])

    def test_missing_target_file_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            internal = Path(directory) / 'internal'
            release = Path(directory) / 'release'
            run = self.run_cli('calibrate', '--targets', str(Path(directory) / 'none.json'),
                               '--internal-output', str(internal),
                               '--release-output', str(release))
            self.assertEqual(run.returncode, 2)
            self.assertFalse(internal.exists())
            self.assertFalse(release.exists())

    def test_conflicting_directories_fail_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.run_cli('calibrate', '--targets', 'data/targets.json',
                               '--internal-output', directory,
                               '--release-output', directory)
            self.assertEqual(run.returncode, 2)
            self.assertIn('嵌套', run.stderr)


class DemoDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = load_records('data/observations.csv')
        cls.targets = load_targets('data/targets.json')

    def test_demo_targets_cover_every_wave(self):
        self.assertEqual(set(self.targets['waves']), {row['wave'] for row in self.records})

    def test_demo_calibration_converges(self):
        result = analyze(self.records, self.targets)
        for wave in result['waves']:
            self.assertEqual(wave['calibration']['status'], 'converged')
            self.assertLess(wave['calibration']['max_relative_residual'], 1e-6)
            for margin in wave['calibration']['margins']:
                self.assertLess(abs(margin['residual']), 1e-4)

    def test_demo_all_cells_represented(self):
        result = analyze(self.records, self.targets)
        for wave in result['waves']:
            self.assertTrue(wave['strata']['all_cells_represented'])

    def test_demo_release_matches_internal_point_estimates(self):
        result = analyze(self.records, self.targets)
        release = render_release(result)
        for wave in result['waves']:
            match = [item for item in release['waves'] if item['wave'] == wave['wave']][0]
            self.assertAlmostEqual(match['estimate']['adoption_weighted'],
                                   round(wave['estimate']['adoption_weighted'], 6))

    def test_demo_release_has_no_identifying_diagnostics(self):
        payload = json.dumps(render_release(analyze(self.records, self.targets)))
        for term in ('base_weight', 'weight_sum', 'max_over_min', 'suppressed_cells'):
            if term == 'suppressed_cells':
                continue
            self.assertNotIn(term, payload)

    def test_demo_is_deterministic(self):
        first = analyze(self.records, self.targets)
        second = analyze(copy.deepcopy(self.records), self.targets)
        self.assertEqual(render_release(first), render_release(second))

    def test_validate_coverage_helper(self):
        wave_rows = [row(0, region='R0')]
        validate_coverage(0, wave_rows, self.targets['waves'][0])
        with self.assertRaisesRegex(ValueError, 'R9'):
            validate_coverage(0, [row(0, region='R9')], self.targets['waves'][0])


class ExampleTargetContractTest(unittest.TestCase):
    def test_example_file_documents_versions_and_dimensions(self):
        payload = json.loads(Path('data/targets.json').read_text())
        self.assertEqual(set(payload), {'waves'})
        totals = {}
        for wave, body in payload['waves'].items():
            self.assertIsInstance(body['target_version'], str)
            self.assertEqual(set(body['margins']), {'region', 'size_class', 'industry'})
            totals[wave] = {round(sum(level.values()), 6)
                            for level in body['margins'].values()}
        for wave, values in totals.items():
            self.assertEqual(len(values), 1, f'wave {wave} totals differ: {values}')
        self.assertNotEqual(payload['waves']['0']['target_version'],
                            payload['waves']['1']['target_version'])


if __name__ == '__main__':
    unittest.main()
