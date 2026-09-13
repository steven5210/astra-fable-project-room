"""Explicit, hash-pinned workaround for the AO 0.13 Claude ACP error precedence bug.

Never run automatically during plugin installation or model dispatch. The caller
supplies a verified idle runtime module and a private backup/receipt directory.
Unknown/new upstream bytes refuse; updates require a fresh compatibility review.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

SOURCE_SHA256 = 'fdc6ca40b0316dc77b93013f4530fbe004806de15adb7bace56654fbde956d78'
OLD = b'if (message.stop_reason === "max_tokens") {'
NEW = b'if (message.stop_reason === "max_tokens" && !message.is_error) {'


def patched(raw):
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256 or raw.count(OLD) != 2:
        raise ValueError('ACP source differs from the reviewed AO 0.13 module; do not patch an unknown update')
    return raw.replace(OLD, NEW)


def apply(module_path, evidence_directory, confirmed_idle):
    if confirmed_idle is not True:
        raise ValueError('The operator must establish that the target runtime is idle before patching')
    path = Path(module_path)
    evidence = Path(evidence_directory)
    if (not path.is_absolute() or not evidence.is_absolute()
            or any(x.is_symlink() for x in (path, *path.parents, evidence, *evidence.parents))):
        raise ValueError('Use explicit absolute module/evidence paths without module symlinks')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('ACP source ownership is unsafe')
    raw = path.read_bytes()
    receipt_path = evidence / 'patch-intent.json'
    if receipt_path.exists():
        prior = json.loads(receipt_path.read_text())
        if prior.get('module') == str(path) and hashlib.sha256(raw).hexdigest() == prior.get('target_sha256'):
            if prior.get('backup') != str(evidence / (SOURCE_SHA256 + '.js')) or Path(prior['backup']).is_symlink():
                raise ValueError('ACP backup has no exact owned path')
            original = Path(prior['backup']).read_bytes()
            if patched(original) != raw or prior.get('source_sha256') != SOURCE_SHA256:
                raise ValueError('Patched ACP source/backup differs from its immutable intent')
            return prior
    updated = patched(raw)
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    source_hash = hashlib.sha256(raw).hexdigest()
    target_hash = hashlib.sha256(updated).hexdigest()
    backup = evidence / (source_hash + '.js')
    if backup.exists():
        if backup.read_bytes() != raw:
            raise ValueError('Existing ACP backup differs')
    else:
        with backup.open('xb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    receipt = {'source_sha256': source_hash, 'target_sha256': target_hash, 'module': str(path),
               'backup': str(backup), 'changes': 2, 'confirmed_idle': True, 'model_calls': 0,
               'native_controller_reload_required': True}
    if receipt_path.exists():
        if json.loads(receipt_path.read_text()) != receipt:
            raise ValueError('A different ACP patch intent exists')
    else:
        with receipt_path.open('x') as stream:
            json.dump(receipt, stream, indent=2); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    if path.read_bytes() != raw:
        raise ValueError('ACP module changed after backup')
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.acp-patch-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(updated); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(info.st_mode))
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module', required=True)
    parser.add_argument('--evidence-directory', required=True)
    parser.add_argument('--confirmed-idle', action='store_true')
    args = parser.parse_args()
    print(json.dumps(apply(args.module, args.evidence_directory, args.confirmed_idle), indent=2))
