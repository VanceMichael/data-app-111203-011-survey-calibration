"""Non-response calibration against local population margins.

Only respondents contribute adoption.  Missing adoption among non-respondents
stays missing; it is never interpreted as a zero outcome.
"""

import hashlib
import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path

DIMENSIONS = ('region', 'size_class', 'industry')


def _finite_nonnegative(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{where}必须是数值')
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f'{where}必须是有限的非负数')
    return number


def load_targets(path):
    """Read and validate a local target-margin JSON file.

    The file describes population margins for each survey wave::

        {"waves": {"0": {"target_version": "v1",
                         "margins": {"region": {...},
                                     "size_class": {...},
                                     "industry": {...}}}}}
    """
    try:
        with Path(path).open(encoding='utf-8') as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as error:
        raise ValueError(f'目标文件不是有效 JSON：{error}') from None

    if not isinstance(raw, dict) or not isinstance(raw.get('waves'), dict) or not raw['waves']:
        raise ValueError('目标文件缺少非空的 waves')

    waves = {}
    for raw_wave, body in raw['waves'].items():
        try:
            wave = int(raw_wave)
        except (TypeError, ValueError):
            raise ValueError(f'目标文件调查期无效：{raw_wave!r}') from None
        if str(wave) != raw_wave:
            raise ValueError(f'目标文件调查期必须写为整数：{raw_wave!r}')
        if wave in waves:
            raise ValueError(f'目标文件调查期重复：{wave}')
        if not isinstance(body, dict) or not isinstance(body.get('margins'), dict):
            raise ValueError(f'调查期 {wave} 缺少 margins')
        version = body.get('target_version')
        if not isinstance(version, str) or not version.strip():
            raise ValueError(f'调查期 {wave} 缺少非空的 target_version')
        margins = body['margins']
        if set(margins) != set(DIMENSIONS):
            raise ValueError(f'调查期 {wave} 的边际维度必须恰为：' + ','.join(DIMENSIONS))
        clean = {}
        for dimension in DIMENSIONS:
            levels = margins[dimension]
            if not isinstance(levels, dict) or not levels:
                raise ValueError(f'调查期 {wave} 的 {dimension} 边际为空')
            parsed = {}
            for level, value in levels.items():
                if not isinstance(level, str) or not level:
                    raise ValueError(f'调查期 {wave} 的 {dimension} 存在空分层名')
                parsed[level] = _finite_nonnegative(
                    value, f'调查期 {wave} 的 {dimension}/{level} 目标值')
            clean[dimension] = dict(sorted(parsed.items()))
        totals = {dimension: math.fsum(clean[dimension].values()) for dimension in DIMENSIONS}
        if len({round(total, 9) for total in totals.values()}) != 1:
            detail = '，'.join(f'{name}={total:g}' for name, total in totals.items())
            raise ValueError(f'调查期 {wave} 的目标边际总量不一致：{detail}')
        total = next(iter(totals.values()))
        if total <= 0:
            raise ValueError(f'调查期 {wave} 的目标总体为零')
        waves[wave] = {'target_version': version, 'margins': clean, 'total': total}

    canonical = json.dumps(
        {str(wave): {'target_version': body['target_version'], 'margins': body['margins']}
         for wave, body in sorted(waves.items())},
        ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))
    return {'waves': waves,
            'fingerprint': hashlib.sha256(canonical.encode()).hexdigest()}


def validate_response_semantics(records):
    """Reject response outcomes that would silently turn missing into 'no'."""
    for position, row in enumerate(records, 1):
        where = f'第{position}条记录'
        if row['responded'] not in (0, 1):
            raise ValueError(f'{where} responded 必须为 0 或 1')
        if row['base_weight'] < 0 or not math.isfinite(row['base_weight']):
            raise ValueError(f'{where} base_weight 必须是非负有限数')
        if row['responded'] == 0 and row['adoption'] is not None:
            raise ValueError(f'{where}未响应者不能带 adoption')
        if row['responded'] == 1 and row['adoption'] is None:
            raise ValueError(f'{where}响应者 adoption 不能为空')
        if row['adoption'] is not None and row['adoption'] not in (0, 1):
            raise ValueError(f'{where} adoption 必须为二元值')


def validate_coverage(wave, records, target):
    """Every observed level must be described by that wave's target."""
    for dimension in DIMENSIONS:
        observed = {row[dimension] for row in records}
        missing = sorted(observed - set(target['margins'][dimension]))
        if missing:
            raise ValueError(
                f'调查期 {wave} 的 {dimension} 层级在目标版本 '
                f'{target["target_version"]} 中缺失：' + ','.join(missing))


def _clamp(value, lower, upper):
    # A zero base weight means the case carries no representation by design.
    # Calibration must not manufacture representation for it via a lower bound.
    if value <= 0:
        return 0.0
    if value < lower:
        return lower
    if upper is not None and value > upper:
        return upper
    return value


def _margin_diagnostics(rows, weights, target):
    details = []
    max_abs = 0.0
    max_rel = 0.0
    for dimension in DIMENSIONS:
        current = defaultdict(float)
        for row, weight in zip(rows, weights):
            current[row[dimension]] += weight
        for level, goal in target['margins'][dimension].items():
            achieved = current.get(level, 0.0)
            residual = achieved - goal
            relative = abs(residual) / goal if goal > 0 else None
            details.append({'dimension': dimension, 'level': level,
                            'target': goal, 'estimate': achieved,
                            'residual': residual, 'relative_residual': relative})
            max_abs = max(max_abs, abs(residual))
            if relative is not None:
                max_rel = max(max_rel, relative)
    return details, max_abs, max_rel


def _converged(max_abs, max_rel, target_total, tolerance):
    return max_rel <= tolerance and max_abs <= tolerance * max(1.0, target_total)


def calibrate(respondents, target, *, max_iterations=200, tolerance=1e-8,
              lower_bound=0.0, upper_bound=None):
    """Run bounded iterative proportional fitting for one wave.

    The returned weights are the closest iterate observed.  A bound that makes
    the margins incompatible is reported as ``not_converged`` with the real
    residual; it is never normalised into a pretended exact match.
    """
    if max_iterations < 1:
        raise ValueError('max_iterations 必须为正整数')
    if tolerance <= 0:
        raise ValueError('tolerance 必须为正数')
    if lower_bound < 0:
        raise ValueError('lower_bound 不能为负')
    if upper_bound is not None and upper_bound < lower_bound:
        raise ValueError('upper_bound 不能小于 lower_bound')

    base = [row['base_weight'] for row in respondents]
    weights = [_clamp(weight, lower_bound, upper_bound) for weight in base]
    if math.fsum(weights) <= 0:
        details, max_abs, max_rel = _margin_diagnostics(respondents, weights, target)
        return {'status': 'infeasible', 'converged': False, 'iterations': 0,
                'margin_rows': details, 'max_absolute_residual': max_abs,
                'max_relative_residual': max_rel, 'weights': weights,
                'lower_bound': lower_bound, 'upper_bound': upper_bound,
                'reason': '响应样本的起始权重合计为零，无法校准'}

    best = None
    best_score = None

    def remember():
        nonlocal best, best_score
        details, max_abs, max_rel = _margin_diagnostics(respondents, weights, target)
        score = (max_rel, max_abs)
        if best_score is None or score < best_score:
            best_score = score
            best = (list(weights), details, max_abs, max_rel)
        return max_abs, max_rel

    max_abs, max_rel = remember()
    converged = _converged(max_abs, max_rel, target['total'], tolerance)
    iterations = 0
    while not converged and iterations < max_iterations:
        iterations += 1
        for dimension in DIMENSIONS:
            current = defaultdict(float)
            for row, weight in zip(respondents, weights):
                current[row[dimension]] += weight
            for level, goal in target['margins'][dimension].items():
                achieved = current.get(level, 0.0)
                if goal > 0 and achieved > 0:
                    factor = goal / achieved
                elif goal == 0 and achieved > 0:
                    factor = 0.0
                else:
                    # No respondent can carry this level; leave it missing and
                    # let the margin residual state that plainly.
                    continue
                if factor == 1.0:
                    continue
                for index, row in enumerate(respondents):
                    if row[dimension] == level:
                        weights[index] *= factor
            weights = [_clamp(weight, lower_bound, upper_bound) for weight in weights]
        max_abs, max_rel = remember()
        converged = _converged(max_abs, max_rel, target['total'], tolerance)

    weights, details, max_abs, max_rel = best
    return {'status': 'converged' if converged else 'not_converged',
            'converged': converged, 'iterations': iterations,
            'margin_rows': details, 'max_absolute_residual': max_abs,
            'max_relative_residual': max_rel, 'weights': weights,
            'lower_bound': lower_bound, 'upper_bound': upper_bound}


def weight_diagnostics(rows, weights, lower_bound, upper_bound):
    clean = [weight for weight in weights]
    total = math.fsum(clean)
    squares = math.fsum(weight * weight for weight in clean)
    ordered = sorted(clean)
    median = statistics.median(ordered) if ordered else None
    mean = total / len(ordered) if ordered else None
    variance = (statistics.fmean((weight - mean) ** 2 for weight in ordered)
                if len(ordered) > 1 else None)
    at_lower = sum(1 for weight in clean
                   if math.isclose(weight, lower_bound, rel_tol=1e-9, abs_tol=1e-12))
    at_upper = (sum(1 for weight in clean
                    if math.isclose(weight, upper_bound, rel_tol=1e-9, abs_tol=1e-12))
                if upper_bound is not None else 0)
    positive = [weight for weight in ordered if weight > 0]
    return {
        'sum': total,
        'minimum': ordered[0] if ordered else None,
        'p25': _percentile(ordered, 0.25) if ordered else None,
        'median': median,
        'p75': _percentile(ordered, 0.75) if ordered else None,
        'maximum': ordered[-1] if ordered else None,
        'mean': mean,
        'coefficient_of_variation': (math.sqrt(variance) / mean
                                     if variance is not None and mean else None),
        'max_over_min': (ordered[-1] / positive[0] if positive else None),
        'at_lower_bound': at_lower,
        'at_upper_bound': at_upper,
        'zero_base_weight_respondents': sum(1 for row, weight in zip(rows, weights)
                                            if row['base_weight'] == 0),
        'kish_effective_n': total * total / squares if squares > 0 else 0.0,
    }


def estimate_from_weights(rows, weights):
    weight_sum = math.fsum(weights)
    if weight_sum <= 0:
        return None
    numerator = math.fsum(row['adoption'] * weight for row, weight in zip(rows, weights))
    return {'adoption_weighted': numerator / weight_sum,
            'adoption_unweighted': (math.fsum(row['adoption'] for row in rows) / len(rows)
                                    if rows else None),
            'numerator': numerator, 'weight_sum': weight_sum}


def _percentile(ordered, quantile):
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def bootstrap_interval(observed, target, *, replicates, confidence, seed,
                       max_iterations=200, tolerance=1e-8,
                       lower_bound=0.0, upper_bound=None):
    """Percentile interval from replicate samples recalibrated every time.

    Each replicate draws the full observed sample within observed strata, keeps
    only the respondents of that draw, and reruns the bounded calibration
    against the same target.  Both non-response and the adjustment itself
    therefore vary across replicates; the final weights are never reused with a
    plain proportion formula.
    """
    if not 0 < confidence < 1:
        raise ValueError('confidence 必须在 (0, 1) 内')
    if replicates < 2:
        raise ValueError('replicates 至少为 2')

    cells = defaultdict(list)
    for row in observed:
        cells[tuple(row[dimension] for dimension in DIMENSIONS)].append(row)
    keys = sorted(cells)
    generator = random.Random(f'{seed}:{target["target_version"]}')
    estimates = []
    invalid = 0
    converged = 0
    for _ in range(replicates):
        sample = []
        for key in keys:
            pool = cells[key]
            sample.extend(pool[index] for index in generator.choices(range(len(pool)), k=len(pool)))
        respondents = [row for row in sample if row['responded'] == 1]
        if not respondents:
            invalid += 1
            continue
        result = calibrate(respondents, target, max_iterations=max_iterations,
                           tolerance=tolerance, lower_bound=lower_bound,
                           upper_bound=upper_bound)
        estimate = estimate_from_weights(respondents, result['weights'])
        if result['status'] == 'infeasible' or estimate is None:
            invalid += 1
            continue
        estimates.append(estimate['adoption_weighted'])
        if result['converged']:
            converged += 1
    estimates.sort()
    alpha = (1 - confidence) / 2
    return {
        'method': 'stratified_bootstrap_recalibrated',
        'confidence': confidence,
        'replicates_requested': replicates,
        'replicates_valid': len(estimates),
        'replicates_converged': converged,
        'replicates_invalid': invalid,
        'lower': _percentile(estimates, alpha),
        'upper': _percentile(estimates, 1 - alpha),
    }
