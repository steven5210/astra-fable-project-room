"""Bounded strict-JSON, identity and resource helpers for the change-set tool.

Every failure raises ChangesetError with a bounded code. Public error surfaces
carry only SHA-256 digests of opaque caller text, never the text itself.
"""

import base64
import hashlib
import json
import math
import struct

MAX_JSON_DEPTH = 64
MAX_INT_DIGITS = 4300
MAX_ID_BYTES = 128
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_AGGREGATE_BYTES = 64 * 1024 * 1024
MAX_TARGETS = 32
MAX_OPERATIONS = 4096
MAX_CONDITIONS = 256
MAX_PINNED_INPUTS = 64
MAX_PATH_DEPTH = 32
MAX_XATTRS = 64
MAX_XATTR_BYTES = 65536
LF = chr(10)
BACKSLASH = chr(92)
RENDERERS = ('json-2space-unicode-lf0', 'json-2space-unicode-lf1',
             'json-2space-ascii-lf0', 'json-2space-ascii-lf1')


class ChangesetError(Exception):
    def __init__(self, code, *, operation_id=None, **detail):
        super().__init__(code)
        self.code = code
        self.operation_id = operation_id
        self.detail = detail

    def public(self):
        out = {'code': self.code}
        digest = id_digest(self.operation_id)
        if digest is not None:
            out['operation_id_sha256'] = digest
        return out


def refuse(code, **kw):
    raise ChangesetError(code, **kw)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def id_digest(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        raw = value.encode('utf-8')
    except UnicodeEncodeError:
        return None
    return hashlib.sha256(raw).hexdigest()


def _no_floats(value):
    stack = [(value, 0)]
    while stack:
        v, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            refuse('record_too_deep')
        if isinstance(v, float):
            refuse('float_in_structural_record')
        if isinstance(v, dict):
            for key in v.keys():
                stack.append((key, depth + 1))
            for item in v.values():
                stack.append((item, depth + 1))
        elif isinstance(v, list):
            for item in v:
                stack.append((item, depth + 1))


def canonical(value):
    _no_floats(value)
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError):
        refuse('invalid_structural_record')


def digest_object(obj, field):
    copy = dict(obj)
    copy.pop(field, None)
    return sha256(canonical(copy).encode('utf-8'))


def check_id(value, what='id'):
    if not isinstance(value, str) or not value:
        refuse('invalid_' + what)
    try:
        raw = value.encode('utf-8')
    except UnicodeEncodeError:
        refuse('invalid_' + what)
    if len(raw) > MAX_ID_BYTES:
        refuse(what + '_too_long')
    return value


def _check_surrogates(value):
    stack = [(value, 0)]
    while stack:
        v, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            refuse('invalid_unicode_surrogate')
        if isinstance(v, str):
            for ch in v:
                if 0xD800 <= ord(ch) <= 0xDFFF:
                    refuse('invalid_unicode_surrogate')
        elif isinstance(v, dict):
            for key in v.keys():
                stack.append((key, depth + 1))
            for item in v.values():
                stack.append((item, depth + 1))
        elif isinstance(v, list):
            for item in v:
                stack.append((item, depth + 1))


def _depth_ok(text, limit):
    depth = 0
    instr = False
    esc = False
    for ch in text:
        if instr:
            if esc:
                esc = False
            elif ch == BACKSLASH:
                esc = True
            elif ch == chr(34):
                instr = False
        else:
            if ch == chr(34):
                instr = True
            elif ch in '[{':
                depth += 1
                if depth > limit:
                    return False
            elif ch in ']}':
                depth -= 1
    return True


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            refuse('duplicate_json_key')
        out[key] = value
    return out


def _bad_const(token):
    refuse('nonfinite_number')


def _int(token):
    body = token[1:] if token.startswith('-') else token
    if len(body) > MAX_INT_DIGITS:
        refuse('integer_too_long')
    try:
        return int(token)
    except ValueError:
        refuse('integer_too_long')


def _float(token):
    try:
        value = float(token)
    except ValueError:
        refuse('invalid_float_literal')
    if not math.isfinite(value):
        refuse('nonfinite_number')
    mant = token.lower().split('e')[0].lstrip('+-')
    if value == 0.0 and any(c in '123456789' for c in mant):
        refuse('float_underflow')
    return value


def strict_loads(data, *, limit=MAX_FILE_BYTES, depth=MAX_JSON_DEPTH):
    if isinstance(data, bytes):
        if len(data) > limit:
            refuse('input_too_large')
        if data.startswith(b'\xef\xbb\xbf'):
            refuse('utf8_bom')
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            refuse('invalid_utf8')
    elif isinstance(data, str):
        text = data
    else:
        refuse('invalid_json_input')
    try:
        encoded_size = len(text.encode('utf-8'))
    except UnicodeEncodeError:
        refuse('invalid_unicode_surrogate')
    if encoded_size > limit:
        refuse('input_too_large')
    if not _depth_ok(text, depth):
        refuse('json_too_deep')
    try:
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_bad_const,
                           parse_float=_float, parse_int=_int)
    except ChangesetError:
        raise
    except Exception:
        refuse('invalid_json')
    _check_surrogates(value)
    return value


def typed_equal(a, b):
    if type(a) is not type(b):
        return False
    if type(a) is float:
        return struct.pack('>d', a) == struct.pack('>d', b)
    if type(a) is dict:
        return set(a) == set(b) and all(typed_equal(a[k], b[k]) for k in a)
    if type(a) is list:
        return len(a) == len(b) and all(typed_equal(x, y) for x, y in zip(a, b))
    return a == b


def value_depth_ok(value, limit=MAX_JSON_DEPTH):
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            if depth + 1 > limit:
                return False
            for child in item.values():
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            if depth + 1 > limit:
                return False
            for child in item:
                stack.append((child, depth + 1))
    return True


def render_json(value, profile):
    if profile not in RENDERERS:
        refuse('invalid_renderer')
    try:
        text = json.dumps(value, indent=2, ensure_ascii=('-ascii-' in profile),
                          allow_nan=False, sort_keys=False)
    except ValueError:
        refuse('nonfinite_number')
    except TypeError:
        refuse('invalid_renderer_value')
    if profile.endswith('lf1'):
        text += LF
    try:
        return text.encode('utf-8')
    except UnicodeEncodeError:
        refuse('invalid_unicode_surrogate')


def decode_text(data):
    if not isinstance(data, bytes):
        refuse('invalid_json_input')
    if data.startswith(b'\xef\xbb\xbf'):
        refuse('utf8_bom')
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        refuse('invalid_utf8')
    _check_surrogates(text)
    return text


def b64e(data):
    return base64.b64encode(data).decode('ascii')


def b64d(text):
    try:
        return base64.b64decode(text.encode('ascii'), validate=True)
    except Exception:
        refuse('invalid_base64')
