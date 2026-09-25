"""Bounded no-follow descriptor reads for the Project Room evidence Read audit (#58).

Every open below a trusted root walks components with O_NOFOLLOW from an already-bound
descriptor; every read is bounded by the frozen version-1 ceilings and every byte actually
read from an admitted extent is charged to the current pass. The operator-authorized
evidence root is never opened: path classification is lexical only. No network, model,
subprocess, lock, database write or mutable controller state is touched here.
"""

import hashlib
import json
import math
import os
import stat


MAX_TRANSCRIPT_BYTES = 67_108_864
MAX_AGGREGATE_BYTES = 268_435_456
MAX_RECORD_BYTES = 4_194_304
MAX_RECORDS = 100_000
MAX_RECORD_IDS = 100_000
MAX_TOOL_IDS = 50_000
MAX_CHILD_ACTORS = 32
MAX_CHILD_FILES = 32
MAX_DIRECTORY_ENTRIES = 256
MAX_JSON_DEPTH = 64
MAX_PATH_BYTES = 16_384
MAX_IDENTITY_BYTES = 16_384
MAX_BINDING_BYTES = 8_388_608
MAX_TIMESTAMP_BYTES = 64
READ_CHUNK = 262_144

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
LEAF_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
ROOT_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class SourceError(Exception):
    """Closed-code evidence read refusal; never rendered into the public report."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _uid():
    return os.getuid()


def _open_component(name, flags, dir_fd):
    """The single descriptor-relative open used for every below-root read; tests inject races here."""
    return os.open(name, flags, dir_fd=dir_fd)


def _open_root(path, flags):
    """The single path-based open of '/'; all named roots are descriptor-walked below it."""
    return os.open(path, flags)


def signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def identity(info):
    return (info.st_dev, info.st_ino)


def bounded_bytes(text):
    if not isinstance(text, str):
        return None
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError:
        return None


def valid_parts(parts):
    return (isinstance(parts, (tuple, list)) and bool(parts)
            and all(isinstance(part, str) and part not in ("", ".", "..")
                    and "/" not in part and "\0" not in part
                    and bounded_bytes(part) is not None for part in parts)
            and sum(len(part.encode("utf-8")) + 1 for part in parts) <= MAX_PATH_BYTES)


def _absolute_parts(path):
    text = os.fspath(path)
    size = bounded_bytes(text)
    if (size is None or not 0 < size <= MAX_PATH_BYTES or not text.startswith("/")
            or "\0" in text):
        raise ValueError("unsafe absolute path")
    parts = tuple(part for part in text.split("/") if part)
    if parts and not valid_parts(parts):
        raise ValueError("unsafe absolute path")
    return text, parts


def _owned_directory(info):
    return stat.S_ISDIR(info.st_mode) and info.st_uid == _uid()


def _owned_regular(info):
    return stat.S_ISREG(info.st_mode) and info.st_uid == _uid()


class DirectoryChain:
    """Retain every ancestor until its descriptor/name relationship has been rechecked."""

    def __init__(self, parent_fd=None, root=None):
        self.fd = parent_fd
        self.root = root
        self.fds = []
        self.links = []

    def append(self, name, owned):
        parent = self.fd
        child = _open_component(name, DIRECTORY_FLAGS, parent)
        self.fds.append(child)
        info = os.fstat(child)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (not stat.S_ISDIR(info.st_mode) or not stat.S_ISDIR(named.st_mode)
                or identity(info) != identity(named) or (owned and not _owned_directory(info))):
            raise SourceError("source_unsafe")
        self.links.append((parent, name, child, identity(info), owned))
        self.fd = child

    def verify(self):
        try:
            if self.root is not None:
                self.root.recheck_identity("source_changed")
            for parent, name, fd, expected, owned in self.links:
                held = os.fstat(fd)
                named = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (identity(held) != expected or identity(named) != expected
                        or not stat.S_ISDIR(held.st_mode) or not stat.S_ISDIR(named.st_mode)
                        or (owned and (not _owned_directory(held) or not _owned_directory(named)))):
                    return False
            return True
        except (OSError, ValueError, SourceError):
            return False

    def close(self):
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds.clear()
        self.fd = None


def _absolute_directory(path):
    text, parts = _absolute_parts(path)
    chain = DirectoryChain()
    try:
        chain.fd = _open_root("/", ROOT_FLAGS)
        chain.fds.append(chain.fd)
        for index, part in enumerate(parts):
            chain.append(part, owned=index == len(parts) - 1)
        if not _owned_directory(os.fstat(chain.fd)) or not chain.verify():
            raise SourceError("source_unsafe")
        return text, chain
    except BaseException:
        chain.close()
        raise


class Opened:
    """Unbuffered owned leaf and its complete retained no-follow ancestor chain."""

    def __init__(self, handle, info, parent_fd, owns_parent, name, chain=None, root=None):
        self.handle, self.info = handle, info
        self.parent_fd, self.owns_parent, self.name = parent_fd, owns_parent, name
        self.chain, self.root = chain, root

    def verify(self, expected_signature=None):
        try:
            info = os.fstat(self.handle.fileno())
            named = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
            if (not _owned_regular(info) or not _owned_regular(named)
                    or signature(named) != signature(info)
                    or (expected_signature is not None and signature(info) != expected_signature)):
                return False
            if self.chain is not None and not self.chain.verify():
                return False
            if self.root is not None:
                self.root.recheck_identity("source_changed")
            return True
        except (OSError, ValueError, SourceError):
            return False

    def close(self):
        try:
            self.handle.close()
        finally:
            if self.chain is not None:
                self.chain.close()
                self.chain = None
            elif self.parent_fd is not None and self.owns_parent:
                os.close(self.parent_fd)
            self.parent_fd = None


class Root:
    """One owned root with retained no-follow ancestors; system ancestors need not be owned."""

    def __init__(self, path, missing_reason, unsafe_reason):
        self.fd = None
        self.chain = None
        self.bindings = {}
        try:
            self.path, self.chain = _absolute_directory(path)
            self.fd = self.chain.fd
            self.identity = identity(os.fstat(self.fd))
        except FileNotFoundError as exc:
            raise SourceError(missing_reason) from exc
        except (OSError, ValueError, TypeError, SourceError) as exc:
            raise SourceError(unsafe_reason) from exc

    def close(self):
        if self.chain is not None:
            self.chain.close()
            self.chain = None
        self.fd = None

    def recheck_identity(self, changed_reason):
        if self.fd is None or self.chain is None or not self.chain.verify():
            raise SourceError(changed_reason)
        try:
            info = os.fstat(self.fd)
        except OSError as exc:
            raise SourceError(changed_reason) from exc
        if identity(info) != self.identity or not _owned_directory(info):
            raise SourceError(changed_reason)

    def _directory(self, parts, missing_reason, unsafe_reason):
        if parts and not valid_parts(parts):
            raise SourceError(unsafe_reason)
        chain = DirectoryChain(self.fd, self)
        try:
            self.recheck_identity(unsafe_reason)
            for part in parts:
                chain.append(part, owned=True)
            return chain
        except FileNotFoundError as exc:
            chain.close()
            raise SourceError(missing_reason) from exc
        except (OSError, SourceError, ValueError) as exc:
            chain.close()
            raise SourceError(unsafe_reason) from exc

    def open_file(self, parts, limit, missing_reason, unsafe_reason, oversize_reason):
        if not valid_parts(parts):
            raise SourceError(unsafe_reason)
        chain = self._directory(parts[:-1], missing_reason, unsafe_reason)
        fd, handle, opened = None, None, None
        transferred = False
        try:
            fd = _open_component(parts[-1], LEAF_FLAGS, chain.fd)
            info = os.fstat(fd)
            if not _owned_regular(info):
                raise SourceError(unsafe_reason)
            if info.st_size > limit:
                raise SourceError(oversize_reason)
            # FileIO has no read-ahead: charged bytes are actual descriptor bytes.
            handle = os.fdopen(fd, "rb", buffering=0)
            fd = None
            opened = Opened(handle, info, chain.fd, False, parts[-1], chain, self)
            if not opened.verify(signature(info)):
                raise SourceError(unsafe_reason)
            transferred = True
            return opened
        except FileNotFoundError as exc:
            raise SourceError(missing_reason) from exc
        except OSError as exc:
            raise SourceError(unsafe_reason) from exc
        finally:
            if fd is not None:
                os.close(fd)
            if not transferred:
                if opened is not None:
                    opened.close()
                else:
                    if handle is not None:
                        handle.close()
                    chain.close()

    def list_dir(self, parts, limit, missing_reason, unsafe_reason):
        chain = self._directory(parts, missing_reason, unsafe_reason)
        names, overflow = [], False
        try:
            before = identity(os.fstat(chain.fd))
            with os.scandir(chain.fd) as entries:
                for entry in entries:
                    if len(names) >= limit:
                        overflow = True
                        break
                    names.append(entry.name)
            if not chain.verify():
                raise SourceError(unsafe_reason)
            return sorted(names), before, overflow
        except OSError as exc:
            raise SourceError(unsafe_reason) from exc
        finally:
            chain.close()

    def find_exact(self, directory_names, filename):
        """Metadata only: exact native filename under the already bounded project names."""
        if not valid_parts((filename,)) or len(directory_names) > MAX_DIRECTORY_ENTRIES:
            raise SourceError("source_unsafe")
        projects = self._directory(("projects",), "source_missing", "source_unsafe")
        matches = []
        try:
            for name in directory_names:
                if not valid_parts((name,)):
                    raise SourceError("source_unsafe")
                try:
                    info = os.stat(name, dir_fd=projects.fd, follow_symlinks=False)
                except FileNotFoundError as exc:
                    raise SourceError("source_changed") from exc
                if stat.S_ISLNK(info.st_mode):
                    raise SourceError("source_unsafe")
                if not stat.S_ISDIR(info.st_mode):
                    continue
                child = self._directory(("projects", name), "source_changed", "source_unsafe")
                try:
                    try:
                        leaf = os.stat(filename, dir_fd=child.fd, follow_symlinks=False)
                    except FileNotFoundError:
                        if not child.verify():
                            raise SourceError("source_changed")
                        continue
                    if not _owned_regular(leaf):
                        raise SourceError("source_unsafe")
                    if not child.verify():
                        raise SourceError("source_changed")
                    matches.append(name)
                finally:
                    child.close()
            if not projects.verify():
                raise SourceError("source_changed")
            return matches
        except OSError as exc:
            raise SourceError("source_unsafe") from exc
        finally:
            projects.close()

    def recheck_file(self, parts, expected, reason="source_changed"):
        opened = self.open_file(parts, expected[5], reason, reason, reason)
        try:
            if not opened.verify(expected):
                raise SourceError(reason)
        finally:
            opened.close()

    def recheck_bindings(self, reason):
        for parts, expected in self.bindings.items():
            self.recheck_file(parts, expected, reason)
        self.recheck_identity(reason)


class ScanResult:
    __slots__ = ("digest", "size", "signature", "records", "torn")

    def __init__(self, digest, size, signature, records, torn):
        self.digest, self.size, self.signature = digest, size, signature
        self.records, self.torn = records, torn


def _admit_extent(opened, collector, limits, expected=None):
    info = opened.info
    if expected is not None and signature(info) != expected:
        raise SourceError("source_changed")
    if info.st_size > limits.get("bytes", MAX_TRANSCRIPT_BYTES):
        raise SourceError("transcript_bytes_limit")
    if info.st_size > limits["aggregate"] - collector.aggregate:
        raise SourceError("aggregate_bytes_limit")
    return info.st_size


def scan_source(opened, on_record, collector, limits):
    """Parse and hash the admitted extent, charging even bytes from a refused source."""
    hasher = hashlib.sha256()
    records, torn = 0, False
    pending = b""
    try:
        left = _admit_extent(opened, collector, limits)
        size = left
        while left:
            allowance = min(READ_CHUNK, left, limits["record"] + 2 - len(pending))
            chunk = opened.handle.read(allowance)
            if not chunk:
                raise SourceError("source_changed")
            collector.charge(len(chunk))
            left -= len(chunk)
            hasher.update(chunk)
            pending += chunk
            start = 0
            while True:
                end = pending.find(b"\n", start)
                if end < 0:
                    pending = pending[start:]
                    break
                payload = pending[start:end]
                if len(payload) > limits["record"]:
                    raise SourceError("record_bytes_limit")
                records += 1
                on_record(payload, True, records)
                start = end + 1
            if len(pending) > limits["record"]:
                raise SourceError("record_bytes_limit")
        if pending:
            records += 1
            torn = True
            on_record(pending, False, records)
        if not opened.verify(signature(opened.info)):
            raise SourceError("source_changed")
        return ScanResult(hasher.hexdigest(), size, signature(opened.info), records, torn)
    finally:
        opened.close()


def rehash_source(opened, result, collector, limits):
    """Hash only the original extent; fstat/name checks prove length without an extra byte read."""
    hasher = hashlib.sha256()
    try:
        left = _admit_extent(opened, collector, limits, result.signature)
        while left:
            chunk = opened.handle.read(min(READ_CHUNK, left))
            if not chunk:
                raise SourceError("source_changed")
            collector.charge(len(chunk))
            left -= len(chunk)
            hasher.update(chunk)
        if not opened.verify(result.signature) or hasher.hexdigest() != result.digest:
            raise SourceError("source_changed")
        return True
    finally:
        opened.close()


def read_binding_file(root, parts, limit, missing_reason, unsafe_reason, oversize_reason, changed_reason):
    opened = root.open_file(parts, limit, missing_reason, unsafe_reason, oversize_reason)
    try:
        chunks, left = [], opened.info.st_size
        while left:
            chunk = opened.handle.read(min(READ_CHUNK, left))
            if not chunk:
                raise SourceError(changed_reason)
            chunks.append(chunk)
            left -= len(chunk)
        expected = signature(opened.info)
        if not opened.verify(expected):
            raise SourceError(changed_reason)
        previous = root.bindings.get(tuple(parts))
        if previous is not None and previous != expected:
            raise SourceError(changed_reason)
        # Binding reads follow a fixed initial/epoch/adoption call graph. The transcript
        # discovery-entry ceiling does not limit this unrelated retained identity set.
        root.bindings[tuple(parts)] = expected
        return b"".join(chunks)
    finally:
        opened.close()


def owned_file_identity(path, unsafe_reason):
    """Metadata-only check of the explicitly supplied SQLite path and all no-follow ancestors."""
    root = None
    opened = None
    try:
        text, parts = _absolute_parts(path)
        if not parts:
            raise SourceError(unsafe_reason)
        parent = text.rsplit("/", 1)[0] or "/"
        root = Root(parent, unsafe_reason, unsafe_reason)
        opened = root.open_file((parts[-1],), 2**63 - 1, unsafe_reason, unsafe_reason, unsafe_reason)
        if opened.info.st_mode & 0o022 or not opened.verify(signature(opened.info)):
            raise SourceError(unsafe_reason)
        return identity(opened.info)
    except (OSError, ValueError, TypeError) as exc:
        raise SourceError(unsafe_reason) from exc
    finally:
        if opened is not None:
            opened.close()
        if root is not None:
            root.close()


def parse_json(data, depth_limit, reason):
    """Strict bounded JSON: no duplicate keys, no nonfinite numbers, bounded depth, strict UTF-8."""
    def no_duplicates(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise SourceError(reason)
            value[key] = item
        return value

    def reject_constant(_name):
        raise SourceError(reason)

    def finite_float(text):
        value = float(text)
        if not math.isfinite(value):
            raise SourceError(reason)
        return value

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceError(reason) from exc
    try:
        value = json.loads(text, object_pairs_hook=no_duplicates, parse_constant=reject_constant,
                           parse_float=finite_float)
    except SourceError:
        raise
    except RecursionError as exc:
        raise SourceError("json_depth_limit") from exc
    except (ValueError, TypeError) as exc:
        raise SourceError(reason) from exc
    check_depth(value, depth_limit, reason)
    return value


def parse_record(payload, depth_limit, reason):
    return parse_json(payload, depth_limit, reason)


def check_depth(value, limit, reason="source_malformed"):
    """Bound container depth and reject escaped invalid Unicode before attribution/hashing."""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            if depth > limit:
                raise SourceError("json_depth_limit")
            if any(bounded_bytes(key) is None for key in item):
                raise SourceError(reason)
            for child in item.values():
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            if depth > limit:
                raise SourceError("json_depth_limit")
            for child in item:
                stack.append((child, depth + 1))
        elif isinstance(item, str) and bounded_bytes(item) is None:
            raise SourceError(reason)
