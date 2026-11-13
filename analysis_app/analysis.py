"""Auditable design and non-response estimates per survey wave.

Each survey wave is calibrated against its own response records and its own
target-margin version.  Cells the response sample cannot support are reported
as such; they are never filled in, and missing outcomes are never read as
"did not adopt".
"""

import math
from collections import defaultdict

from .calibration import (DIMENSIONS, bootstrap_interval, calibrate,
                          estimate_from_weights, validate_coverage,
                          validate_response_semantics, weight_diagnostics)
from .schema import GROUP, MEASURE, PRODUCT, UNIT

DETAIL_DIMENSION = 'size_class'

DEFAULT_CONFIG = {
    'calibration': {'max_iterations': 200, 'tolerance': 1e-8,
                    'lower_bound': 0.0, 'upper_bound': 10.0},
    'bootstrap': {'replicates': 200, 'confidence': 0.95, 'seed': 20240601},
    'suppression': {'minimum_respondents': 5, 'minimum_rows': 10, 'complementary': True},
}


def merge_config(config):
    merged = {section: dict(values) if isinstance(values, dict) else values
              for section, values in DEFAULT_CONFIG.items()}
    for section, values in (config or {}).items():
        if section in ('calibration', 'bootstrap', 'suppression'):
            if not isinstance(values, dict):
                raise ValueError(f'配置段 {section} 必须是对象')
            merged[section].update(values)
        else:
            merged[section] = values
    validate_config(merged)
    return merged


def validate_config(config):
    calibration = config['calibration']
    if not isinstance(calibration['max_iterations'], int) or calibration['max_iterations'] < 1:
        raise ValueError('calibration.max_iterations 必须为正整数')
    if not isinstance(calibration['tolerance'], (int, float)) or calibration['tolerance'] <= 0:
        raise ValueError('calibration.tolerance 必须为正数')
    lower, upper = calibration['lower_bound'], calibration['upper_bound']
    if not isinstance(lower, (int, float)) or lower < 0:
        raise ValueError('calibration.lower_bound 必须为非负数')
    if upper is not None and (not isinstance(upper, (int, float)) or upper <= 0 or upper < lower):
        raise ValueError('calibration.upper_bound 必须为正数且不小于 lower_bound')
    bootstrap = config['bootstrap']
    if not isinstance(bootstrap['replicates'], int) or bootstrap['replicates'] < 2:
        raise ValueError('bootstrap.replicates 至少为 2')
    confidence = bootstrap['confidence']
    if not isinstance(confidence, (int, float)) or not 0 < confidence < 1:
        raise ValueError('bootstrap.confidence 必须在 (0, 1) 内')
    suppression = config['suppression']
    for key in ('minimum_respondents', 'minimum_rows'):
        if not isinstance(suppression[key], int) or suppression[key] < 1:
            raise ValueError(f'suppression.{key} 必须为正整数')
    if not isinstance(suppression['complementary'], bool):
        raise ValueError('suppression.complementary 必须为布尔值')


def _finish(value, digits=10):
    if value is None:
        return None
    rounded = round(float(value), digits)
    return 0.0 if rounded == 0 else rounded


def _cell_key(row):
    return tuple(row[dimension] for dimension in DIMENSIONS)


def _grouped(rows, dimensions):
    groups = defaultdict(list)
    for row in rows:
        groups[tuple(row[dimension] for dimension in dimensions)].append(row)
    return groups


def strata_report(observed, respondents):
    """List every observed stratum, flagging those no respondent can stand for."""
    cells = _grouped(observed, DIMENSIONS)
    responded = _grouped([row for row in respondents if row['responded'] == 1], DIMENSIONS)
    rows = []
    unrepresented = []
    for key in sorted(cells):
        members = cells[key]
        alive = responded.get(key, [])
        status = 'responded' if alive else 'population_observed_no_respondents'
        row = {'region': key[0], 'size_class': key[1], 'industry': key[2],
               'status': status, 'observed_rows': len(members), 'respondents': len(alive),
               'base_weight_observed': _finish(math.fsum(item['base_weight'] for item in members)),
               'base_weight_respondents': _finish(math.fsum(item['base_weight'] for item in alive))}
        rows.append(row)
        if status != 'responded':
            unrepresented.append({'region': key[0], 'size_class': key[1], 'industry': key[2],
                                  'status': status, 'observed_rows': len(members)})
    return {'cells': rows, 'unrepresented': unrepresented,
            'unrepresented_count': len(unrepresented),
            'all_cells_represented': not unrepresented}


def detail_aggregation(observed, respondents, weights, dimension):
    """Weighted adoption per segment level, with real observed/respondent counts.

    Counts are taken over all observed rows in the segment, so a published cell
    cannot claim a width the sample does not have.  Only respondents carry
    weights and therefore contribute to the estimate.
    """
    observed_rows = defaultdict(int)
    for row in observed:
        observed_rows[row[dimension]] += 1
    groups = defaultdict(list)
    for row, weight in zip(respondents, weights):
        groups[row[dimension]].append((row, weight))
    details = {}
    for level in sorted(observed_rows):
        members = groups.get(level, [])
        point = estimate_from_weights([row for row, _ in members],
                                      [weight for _, weight in members])
        details[level] = {'adoption_weighted': point['adoption_weighted'] if point else None,
                          'respondents': len(members),
                          'observed_rows': observed_rows[level],
                          'weight_sum': point['weight_sum'] if point else 0.0}
    return details


def analyze_wave(wave, observed, target, config):
    """Calibrate one wave and keep every diagnostic needed for audit."""
    respondents = [row for row in observed if row['responded'] == 1]
    calibration = config['calibration']
    validate_coverage(wave, observed, target)
    fitted = calibrate(respondents, target,
                       max_iterations=calibration['max_iterations'],
                       tolerance=calibration['tolerance'],
                       lower_bound=calibration['lower_bound'],
                       upper_bound=calibration['upper_bound'])
    weights = fitted['weights']
    point = estimate_from_weights(respondents, weights)
    details_weights = weight_diagnostics(respondents, weights,
                                         calibration['lower_bound'], calibration['upper_bound'])
    if respondents:
        interval = bootstrap_interval(
            observed, target,
            replicates=config['bootstrap']['replicates'],
            confidence=config['bootstrap']['confidence'],
            seed=f'{config["bootstrap"]["seed"]}:wave={wave}',
            max_iterations=calibration['max_iterations'],
            tolerance=calibration['tolerance'],
            lower_bound=calibration['lower_bound'],
            upper_bound=calibration['upper_bound'])
    else:
        interval = {'method': 'stratified_bootstrap_recalibrated',
                    'confidence': config['bootstrap']['confidence'],
                    'replicates_requested': config['bootstrap']['replicates'],
                    'replicates_valid': 0, 'replicates_converged': 0,
                    'replicates_invalid': 0, 'lower': None, 'upper': None,
                    'reason': '该调查期没有响应样本，无法估计区间'}
    return {
        'wave': wave,
        'target_version': target['target_version'],
        'target_total': target['total'],
        'observed_rows': len(observed),
        'respondents': len(respondents),
        'base_weight_observed': math.fsum(row['base_weight'] for row in observed),
        'base_weight_respondents': math.fsum(row['base_weight'] for row in respondents),
        'calibration': {'status': fitted['status'], 'converged': fitted['converged'],
                        'iterations': fitted['iterations'],
                        'max_iterations': calibration['max_iterations'],
                        'tolerance': calibration['tolerance'],
                        'lower_bound': calibration['lower_bound'],
                        'upper_bound': calibration['upper_bound'],
                        'max_absolute_residual': fitted['max_absolute_residual'],
                        'max_relative_residual': fitted['max_relative_residual'],
                        'reason': fitted.get('reason'),
                        'margins': fitted['margin_rows']},
        'weights': details_weights,
        'estimate': {'metric': MEASURE,
                     'adoption_weighted': point['adoption_weighted'] if point else None,
                     'adoption_unweighted': point['adoption_unweighted'] if point else None,
                     'weight_sum': point['weight_sum'] if point else None},
        'interval': interval,
        'strata': strata_report(observed, respondents),
        'detail': detail_aggregation(observed, respondents, weights, DETAIL_DIMENSION),
        'region_detail': detail_aggregation(observed, respondents, weights, GROUP),
    }


def analyze(records, target_file, config=None):
    """Analyze every wave in the records, each with its own response set and target."""
    config = merge_config(config)
    validate_response_semantics(records)
    target_waves = target_file['waves']
    sample_waves = sorted({row['wave'] for row in records})
    absent = [wave for wave in sample_waves if wave not in target_waves]
    if absent:
        raise ValueError('缺少调查期目标边际：' + ','.join(str(wave) for wave in absent))

    by_wave = defaultdict(list)
    for row in records:
        by_wave[row['wave']].append(row)
    waves = [analyze_wave(wave, sorted(by_wave[wave], key=lambda row: row['observation_id']),
                          target_waves[wave], config)
             for wave in sample_waves]
    return {'product': PRODUCT, 'schema_version': 2, 'unit': UNIT,
            'input_rows': len(records), 'waves': waves,
            'config': config, 'target_fingerprint': target_file['fingerprint']}
