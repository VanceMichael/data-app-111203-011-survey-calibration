"""Command line entry points.

``summary`` keeps the original design-weighted description.  ``calibrate`` adds
the non-response calibration and writes two deliberately separate outputs: an
internal diagnostic directory and a published directory.
"""

import argparse
import json
import sys
from pathlib import Path

from .analysis import analyze
from .calibration import load_targets
from .io import load_records
from .output import (INTERNAL_FILE_MODE, RELEASE_FILE_MODE, prepare_separated_directories,
                     save_json)
from .release import render_internal, render_release
from .summary import summarize


def _print(result):
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))


def _load_config(path):
    if path is None:
        return None
    try:
        with Path(path).open(encoding='utf-8') as handle:
            config = json.load(handle)
    except json.JSONDecodeError as error:
        raise ValueError(f'配置文件不是有效 JSON：{error}') from None
    if not isinstance(config, dict):
        raise ValueError('配置文件顶层必须是对象')
    return config


def _calibrate(args):
    records = load_records(args.input)
    targets = load_targets(args.targets)
    config = _load_config(args.config)
    result = analyze(records, targets, config)
    internal = render_internal(result)
    release = render_release(result)
    internal_path, release_path = prepare_separated_directories(
        args.internal_output, args.release_output)
    save_json(internal_path / 'diagnostics.json', internal, mode=INTERNAL_FILE_MODE)
    save_json(release_path / 'estimates.json', release, mode=RELEASE_FILE_MODE)
    if args.output:
        save_json(args.output, release, mode=RELEASE_FILE_MODE)
    _print(release if args.release_stdout else internal)
    return 0


def main():
    parser = argparse.ArgumentParser(description='本地观测数据分析')
    commands = parser.add_subparsers(dest='command', required=True)

    summary = commands.add_parser('summary', help='按业务维度核对观测口径')
    summary.add_argument('--input', type=Path, default=Path('data/observations.csv'))
    summary.add_argument('--output', type=Path)

    calibrate = commands.add_parser('calibrate', help='按调查期做非响应校准与发布抑制')
    calibrate.add_argument('--input', type=Path, default=Path('data/observations.csv'))
    calibrate.add_argument('--targets', type=Path, default=Path('data/targets.json'))
    calibrate.add_argument('--config', type=Path)
    calibrate.add_argument('--internal-output', type=Path,
                           default=Path('output/internal'))
    calibrate.add_argument('--release-output', type=Path,
                           default=Path('output/release'))
    calibrate.add_argument('--output', type=Path, help='额外写入一份发布结果副本')
    calibrate.add_argument('--release-stdout', action='store_true',
                           help='标准输出打印发布视图而不是内部诊断')
    args = parser.parse_args()
    try:
        if args.command == 'summary':
            result = summarize(load_records(args.input))
            if args.output:
                save_json(args.output, result)
            _print(result)
            return 0
        return _calibrate(args)
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
