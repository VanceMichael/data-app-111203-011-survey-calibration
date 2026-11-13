"""Suppression rules for published segment output.

Small cells are withheld before publication.  The published output must not
reveal a withheld cell, either directly or by letting a reader subtract a
published total.  Internal diagnostics live in a separate output and keep the
suppressed detail.

The rule for subtraction protection is the standard complementary rule: for
each published partition (the detail levels of one wave, the region groups),
either every cell is published or at least two cells are withheld.  With two
withheld cells no single published total determines either missing value.
"""

from .schema import GROUP

DETAIL = 'size_class'


def _qualifies(values, suppression):
    return (values['respondents'] >= suppression['minimum_respondents']
            and values['observed_rows'] >= suppression['minimum_rows'])


def _partition_decision(qualifying, total_levels, suppression, empty_reason, weight_of):
    """Decide what a partition may publish, preventing a single hidden remainder.

    ``qualifying`` levels are those that pass the thresholds.  If exactly one
    cell would be withheld, one more published cell is withdrawn so that at
    least two cells are hidden and no single published total determines either
    missing value.  The smallest qualifying cell is withdrawn first, to lose as
    little information as possible.  If nothing qualifies, nothing is published.
    """
    publish = list(qualifying)
    complementary = []
    if suppression['complementary'] and len(total_levels) - len(publish) == 1 and publish:
        smallest = min(publish, key=lambda level: (weight_of(level), level))
        publish.remove(smallest)
        complementary.append(smallest)
    if publish:
        return {'publish': publish, 'complementary': complementary,
                'action': 'publish',
                'reason': 'complementary_suppression' if complementary else None}
    if complementary:
        return {'publish': [], 'complementary': complementary, 'action': 'withheld',
                'reason': 'complementary_suppression'}
    return {'publish': [], 'complementary': [], 'action': 'withheld', 'reason': empty_reason}


def _withheld_reason(decision, level):
    if level in decision['complementary']:
        return 'complementary_suppression'
    if decision['action'] == 'withheld':
        return decision['reason']
    return 'below_suppression_threshold'


def _suppressed_row(wave_id, dimension, level, respondents, reason):
    return {'wave': wave_id, 'dimension': dimension, 'level': level,
            'respondents': respondents, 'reason': reason}


def suppress_detail(waves, config):
    suppression = config['suppression']
    internal = {}
    release = {}
    decisions = []
    suppressed = []
    for wave in waves:
        wave_id = wave['wave']
        levels = sorted(wave['detail'])
        internal[wave_id] = {level: dict(wave['detail'][level]) for level in levels}
        qualifying = [level for level in levels if _qualifies(wave['detail'][level], suppression)]
        decision = _partition_decision(
            qualifying, levels, suppression, 'no_publishable_cells',
            weight_of=lambda level: wave['detail'][level]['respondents'])
        decisions.append({'wave': wave_id, 'dimension': DETAIL,
                          'action': decision['action'], 'reason': decision['reason'],
                          'published_levels': len(decision['publish']),
                          'withheld_levels': len(levels) - len(decision['publish'])})
        release[wave_id] = {level: dict(wave['detail'][level]) for level in decision['publish']}
        for level in levels:
            if level in decision['publish']:
                continue
            suppressed.append(_suppressed_row(wave_id, DETAIL, level,
                                              wave['detail'][level]['respondents'],
                                              _withheld_reason(decision, level)))
    return {'internal': internal, 'release': release,
            'suppressed': sorted(suppressed, key=lambda row: (row['wave'], row['level'])),
            'decisions': decisions}


def suppress_groups(waves, config):
    """Decide which region groups may appear.

    Groups are grouped by wave.  A group is eligible only when its value in
    that wave clears both thresholds; groups missing from a wave are not
    compared.  The complementary rule then applies to each wave's eligible set
    exactly as for detail: a lone withheld group next to published groups is
    recoverable from the all-region total, so one more group is withheld.
    """
    suppression = config['suppression']
    levels = sorted({level for wave in waves for level in wave['region_detail']})
    eligible = {}
    for wave in waves:
        wave_id = wave['wave']
        eligible[wave_id] = [level for level in levels
                             if level in wave['region_detail']
                             and _qualifies(wave['region_detail'][level], suppression)]

    published = set()
    decisions = []
    withheld = {}
    for wave in waves:
        wave_id = wave['wave']
        present = [level for level in levels if level in wave['region_detail']]
        decision = _partition_decision(
            eligible[wave_id], present, suppression, 'no_publishable_groups',
            weight_of=lambda level: wave['region_detail'][level]['respondents'])
        decisions.append({'wave': wave_id, 'dimension': GROUP, 'action': decision['action'],
                          'reason': decision['reason'],
                          'published_groups': len(decision['publish']),
                          'withheld_groups': len(present) - len(decision['publish'])})
        for level in decision['publish']:
            published.add((wave_id, level))
        for level in present:
            if level in decision['publish']:
                continue
            withheld[(wave_id, level)] = _withheld_reason(decision, level)

    internal = []
    release = []
    suppressed = []
    for level in levels:
        for wave in waves:
            row = wave['region_detail'].get(level)
            if row is None:
                continue
            entry = {'group': level, 'wave': wave['wave'],
                     'respondents': row['respondents'], 'observed_rows': row['observed_rows'],
                     'adoption_weighted': row['adoption_weighted']}
            internal.append(entry)
            if (wave['wave'], level) in published:
                release.append(entry)
            else:
                suppressed.append({'group': level, 'wave': wave['wave'],
                                   'respondents': row['respondents'],
                                   'reason': withheld[(wave['wave'], level)]})
    return {'internal': internal, 'release': release, 'suppressed': suppressed,
            'decisions': decisions}


def render_release(result):
    """Published view: no weight sums, no internal residuals, no small cells.

    Suppressed levels are not named here, because naming a cell (even without
    its value) still discloses which segment was too thin.
    """
    waves = []
    for wave in result['waves']:
        interval = wave['interval']
        waves.append({'wave': wave['wave'], 'target_version': wave['target_version'],
                      'respondents': wave['respondents'],
                      'estimate': {'metric': wave['estimate']['metric'],
                                   'adoption_weighted': _round(wave['estimate']['adoption_weighted'])},
                      'interval': {'confidence': interval['confidence'],
                                   'lower': _round(interval['lower']),
                                   'upper': _round(interval['upper']),
                                   'method': interval['method'],
                                   'replicates_valid': interval['replicates_valid'],
                                   'replicates_converged': interval['replicates_converged']},
                      'calibration': {'converged': wave['calibration']['converged'],
                                      'status': wave['calibration']['status'],
                                      'max_relative_residual': _round(
                                          wave['calibration']['max_relative_residual'])},
                      'unrepresented_strata': wave['strata']['unrepresented_count']})
    detail = suppress_detail(result['waves'], result['config'])
    groups = suppress_groups(result['waves'], result['config'])
    suppressed_total = len(detail['suppressed']) + len(groups['suppressed'])
    return {
        'product': result['product'], 'schema_version': result['schema_version'],
        'unit': result['unit'], 'release': True,
        'target_fingerprint': result['target_fingerprint'],
        'waves': waves,
        'detail': {str(wave): [{'level': level,
                                'adoption_weighted': _round(values['adoption_weighted']),
                                'respondents': values['respondents']}
                               for level, values in sorted(levels.items())]
                   for wave, levels in detail['release'].items()},
        'groups': [{'group': row['group'], 'wave': row['wave'],
                    'adoption_weighted': _round(row['adoption_weighted']),
                    'respondents': row['respondents']}
                   for row in groups['release']],
        'suppression': {
            'minimum_respondents': result['config']['suppression']['minimum_respondents'],
            'minimum_rows': result['config']['suppression']['minimum_rows'],
            'complementary': result['config']['suppression']['complementary'],
            'suppressed_cells': suppressed_total,
            'decisions': detail['decisions'] + groups['decisions'],
        },
    }


def render_internal(result):
    """Full diagnostics including every suppressed cell, for authorised staff."""
    waves = []
    for wave in result['waves']:
        waves.append({
            'wave': wave['wave'], 'target_version': wave['target_version'],
            'target_total': _clean(wave['target_total']),
            'observed_rows': wave['observed_rows'], 'respondents': wave['respondents'],
            'base_weight_observed': _clean(wave['base_weight_observed']),
            'base_weight_respondents': _clean(wave['base_weight_respondents']),
            'calibration': {**{key: value for key, value in wave['calibration'].items()
                               if key != 'margins'},
                            'margins': [_clean_margin(row) for row in wave['calibration']['margins']]},
            'weights': {key: _clean(value) for key, value in wave['weights'].items()},
            'estimate': {'metric': wave['estimate']['metric'],
                         'adoption_weighted': _clean(wave['estimate']['adoption_weighted']),
                         'adoption_unweighted': _clean(wave['estimate']['adoption_unweighted']),
                         'weight_sum': _clean(wave['estimate']['weight_sum'])},
            'interval': {**{key: value for key, value in wave['interval'].items()
                            if key not in ('lower', 'upper')},
                         'lower': _clean(wave['interval']['lower']),
                         'upper': _clean(wave['interval']['upper'])},
            'strata': {'unrepresented': wave['strata']['unrepresented'],
                       'unrepresented_count': wave['strata']['unrepresented_count'],
                       'all_cells_represented': wave['strata']['all_cells_represented'],
                       'cells': [_clean_values(row) for row in wave['strata']['cells']]},
            'detail': {level: _clean_values(values) for level, values in wave['detail'].items()},
            'region_detail': {level: _clean_values(values)
                              for level, values in wave['region_detail'].items()},
        })
    detail = suppress_detail(result['waves'], result['config'])
    groups = suppress_groups(result['waves'], result['config'])
    return {'product': result['product'], 'schema_version': result['schema_version'],
            'unit': result['unit'], 'release': False,
            'input_rows': result['input_rows'],
            'target_fingerprint': result['target_fingerprint'],
            'config': result['config'], 'waves': waves,
            'suppression': {'withheld_detail': detail['suppressed'],
                            'withheld_groups': groups['suppressed'],
                            'decisions': detail['decisions'] + groups['decisions']}}


def _clean(value):
    if value is None:
        return None
    if isinstance(value, float):
        rounded = round(value, 10)
        return 0.0 if rounded == 0 else rounded
    return value


def _clean_values(values):
    return {key: _clean(value) for key, value in values.items()}


def _clean_margin(row):
    return {'dimension': row['dimension'], 'level': row['level'],
            'target': _clean(row['target']), 'estimate': _clean(row['estimate']),
            'residual': _clean(row['residual']),
            'relative_residual': _clean(row['relative_residual'])}


def _round(value):
    if value is None:
        return None
    return round(float(value), 6)
