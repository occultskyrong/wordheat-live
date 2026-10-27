"""Strict offline file whitelist verification before any model code is loaded."""
from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath
import re

from .protocol import canonical, parse_json


class ManifestFailure(ValueError):
    pass


@dataclass(frozen=True)
class Manifest:
    data: dict
    sha256: str
    root: Path

    @property
    def manifest_id(self): return self.data['manifest_id']
    @property
    def dimension(self): return self.data['dimension']
    @property
    def normalize(self): return self.data['normalize']


def load_manifest(path: Path, model_root: Path):
    try:
        if path.is_symlink() or model_root.is_symlink(): raise ManifestFailure('SYMLINK_REJECTED')
        root = model_root.resolve(strict=True)
        if not root.is_dir(): raise ManifestFailure('MODEL_ROOT_INVALID')
        data = parse_json(path.read_bytes())
        for key in ('manifest_id', 'model_id', 'revision', 'license', 'dimension', 'normalize', 'pooling', 'precision', 'files'):
            if key not in data: raise ManifestFailure('MANIFEST_FIELD_MISSING')
        if any(not isinstance(data[k], str) or not data[k] for k in ('manifest_id', 'model_id', 'license')):
            raise ManifestFailure('MANIFEST_IDENTITY_INVALID')
        if not isinstance(data['revision'], str) or not re.fullmatch('[0-9a-f]{40}', data['revision']):
            raise ManifestFailure('REVISION_NOT_FROZEN')
        if type(data['dimension']) is not int or not 1 <= data['dimension'] <= 4096: raise ManifestFailure('DIMENSION_INVALID')
        if type(data['normalize']) is not bool or data['precision'] != 'float32' or data['pooling'] not in ('cls', 'mean'):
            raise ManifestFailure('MODEL_SETTINGS_INVALID')
        if not isinstance(data['files'], list) or not data['files']: raise ManifestFailure('FILE_LIST_EMPTY')
        seen = set()
        for item in data['files']:
            name = item['path']
            if not isinstance(name, str) or not name or '\\' in name or ':' in name or name.startswith('/') or any(part in ('', '.', '..') for part in name.split('/')):
                raise ManifestFailure('MODEL_PATH_UNSAFE')
            if name in seen: raise ManifestFailure('MODEL_PATH_DUPLICATE')
            seen.add(name)
            relative = PurePosixPath(name)
            target = root.joinpath(*relative.parts)
            if any(part.is_symlink() for part in [target, *target.parents] if part != root.parent):
                raise ManifestFailure('SYMLINK_REJECTED')
            if not target.resolve(strict=True).is_relative_to(root) or not target.is_file(): raise ManifestFailure('MODEL_PATH_UNSAFE')
            if type(item['size']) is not int or item['size'] < 0 or target.stat().st_size != item['size']:
                raise ManifestFailure('MODEL_SIZE_MISMATCH')
            if not re.fullmatch('[0-9a-f]{64}', item['sha256']): raise ManifestFailure('MODEL_SHA_INVALID')
            digest = hashlib.sha256()
            with target.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''): digest.update(chunk)
            if digest.hexdigest() != item['sha256']: raise ManifestFailure('MODEL_SHA_MISMATCH')
        # Any unlisted file could be consumed by the model loader; reject it too.
        for target in root.rglob('*'):
            if target.is_symlink(): raise ManifestFailure('SYMLINK_REJECTED')
            if target.is_file() and target.relative_to(root).as_posix() not in seen:
                raise ManifestFailure('MODEL_FILE_NOT_LISTED')
        return Manifest(data, hashlib.sha256(canonical(data)).hexdigest(), root)
    except ManifestFailure:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ManifestFailure('MANIFEST_INVALID') from exc
