import json
import os
import tempfile
from pathlib import Path

INTERNAL_DIRECTORY_MODE = 0o700
RELEASE_DIRECTORY_MODE = 0o750
INTERNAL_FILE_MODE = 0o600
RELEASE_FILE_MODE = 0o640


def save_json(path, result, mode=None):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.analysis-', dir=destination.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _inside(path, other):
    return other in path.parents or path == other


def prepare_directory(path, mode):
    """Create (or tighten) one output directory and return its resolved path."""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.is_dir():
        raise ValueError(f'输出路径不是目录：{directory}')
    os.chmod(directory, mode)
    return directory.resolve()


def prepare_separated_directories(internal_path, release_path):
    """Keep internal diagnostics and published output in separate permission scopes.

    The overlap check runs before either directory is created, so a rejected
    layout leaves no partial output behind.
    """
    internal = Path(internal_path).resolve()
    release = Path(release_path).resolve()
    if _inside(internal, release) or _inside(release, internal):
        raise ValueError('内部诊断目录与发布目录不能相同或互相嵌套')
    internal_directory = prepare_directory(internal, INTERNAL_DIRECTORY_MODE)
    release_directory = prepare_directory(release, RELEASE_DIRECTORY_MODE)
    return internal_directory, release_directory
