"""Unit proofs for the one shared finite-capacity streaming transcript reader itself.

test_ao_native_transcript_capacity.py proves the repair end-to-end through the three production
readers at realistic (>64,000,000 byte) scale. This module instead proves
``ao_native_outcome.iter_transcript_records`` and ``ao_native_outcome.read_complete_transcript`` in
isolation, against small, explicit, hand-checked budgets: terminator handling, chunk-boundary
splitting, the per-record and per-file record-count budgets, malformed/non-dict rows, identity and
mutation-during-read defenses, and descriptor closure on every path. Further classes prove: a parse
refusal (malformed row, record-bytes or record-count budget) is itself rechecked against descriptor/
named-path identity before it is let through, so a concurrent mutation that manufactures the refusal
is reported as changed rather than malformed; terminator-run scanning is fast on a blank-line flood
and exactly matches the legacy ``bytes.splitlines()``-based parse over a large seeded fuzz of random
chunkings; ``read_complete_transcript``'s budget/chunk defaults resolve the current module constant
at call time, not at import time; and descriptor close() is observed directly (not inferred from a
post-close ``fstat`` failure) on every refusal path. A final small class proves the two production
call sites that needed their own new logic on top of the shared reader:
``ao_engineering_transition._read_transcript``'s own dict-shape refusal and
``ao_spec_verdict_recovery._allowed_tool_uses``'s prefix/straddle/suffix boundary classification.

All paths, identifiers and content below are synthetic and created under ``tempfile`` for this
process only; nothing here reads or depends on any real account, credential or other checkout.
"""
import array
import hashlib
import json
import os
import random
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import ao_native_outcome as ano
import ao_spec_verdict_recovery as svr
from room import RoomError


def _tempdir(case):
    holder = tempfile.TemporaryDirectory(prefix='ao-reader-')
    case.addCleanup(holder.cleanup)
    os.chmod(holder.name, 0o700)
    # Resolve away any symlink in the platform tempdir tree itself (e.g. macOS /var -> /private/var)
    # so that symlink-sensitive paths below it are not mistaken for a symlinked transcript.
    return Path(holder.name).resolve()


def _write(path, data):
    path.write_bytes(data)
    return path


def _patch_read_chunk(case, replacement):
    """Install ``replacement`` as ``ano._read_chunk`` (the module's test seam) for ``case``'s
    duration, restoring the original on cleanup; returns the original for a wrapping spy to delegate
    to."""
    original = ano._read_chunk
    ano._read_chunk = replacement
    case.addCleanup(lambda: setattr(ano, '_read_chunk', original))
    return original


# -------------------------------------------------------------------------------------------------
# iter_transcript_records: pure parsing/offset-tracking generator, no file I/O.
# -------------------------------------------------------------------------------------------------

class IterTranscriptRecordsTests(unittest.TestCase):

    def _records(self, chunks, **kwargs):
        return list(ano.iter_transcript_records(iter(chunks), **kwargs))

    def test_single_unterminated_record(self):
        records = self._records([b'{"a":1}'])
        self.assertEqual(records, [(0, 7, {'a': 1})])

    def test_lf_terminator(self):
        records = self._records([b'{"a":1}\n{"b":2}\n'])
        self.assertEqual(records, [(0, 7, {'a': 1}), (8, 15, {'b': 2})])

    def test_cr_terminator(self):
        records = self._records([b'{"a":1}\r{"b":2}\r'])
        self.assertEqual(records, [(0, 7, {'a': 1}), (8, 15, {'b': 2})])

    def test_crlf_terminator(self):
        records = self._records([b'{"a":1}\r\n{"b":2}\r\n'])
        self.assertEqual(records, [(0, 7, {'a': 1}), (9, 16, {'b': 2})])

    def test_mixed_terminators_and_blank_lines_match_bytes_splitlines(self):
        # Hand-checked against bytes.splitlines(): b'{"a":1}', b'', b'{"b":2}', b'{"c":3}', b'{"d":4}'
        # -- the one blank line (between the \r\n and the next \r\n) is skipped and never counted.
        data = b'{"a":1}\r\n\r\n{"b":2}\n{"c":3}\r{"d":4}'
        records = self._records([data])
        self.assertEqual(records, [(0, 7, {'a': 1}), (11, 18, {'b': 2}),
                                    (19, 26, {'c': 3}), (27, 34, {'d': 4})])
        for start, end, row in records:
            self.assertEqual(json.loads(data[start:end]), row)

    def test_blank_and_whitespace_only_lines_are_skipped_and_not_counted(self):
        records = self._records([b'\n   \n{"a":1}\n\t\n{"b":2}\n'], max_records=2)
        self.assertEqual(records, [(5, 12, {'a': 1}), (15, 22, {'b': 2})])

    def test_chunk_boundary_split_mid_record(self):
        data = b'{"abcdefg":1}\n'
        for cut in range(1, len(data)):
            with self.subTest(cut=cut):
                records = self._records([data[:cut], data[cut:]])
                self.assertEqual(records, [(0, 13, {'abcdefg': 1})])

    def test_chunk_boundary_split_inside_crlf_terminator(self):
        data = b'{"a":1}\r\n{"b":2}\r\n'
        split = data.index(b'\r\n') + 1  # cut between the \r and the \n of the first terminator
        records = self._records([data[:split], data[split:]])
        self.assertEqual(records, [(0, 7, {'a': 1}), (9, 16, {'b': 2})])

    def test_record_exactly_at_max_record_bytes_is_admitted(self):
        payload = b'{"k":"' + b'x' * 90 + b'"}'  # 98 content bytes
        records = self._records([payload + b'\n'], max_record_bytes=len(payload))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][:2], (0, len(payload)))

    def test_record_one_over_max_record_bytes_refuses_single_chunk(self):
        payload = b'{"k":"' + b'x' * 91 + b'"}'  # 99 content bytes
        with self.assertRaises(RoomError) as ctx:
            self._records([payload + b'\n'], max_record_bytes=len(payload) - 1)
        self.assertEqual(ctx.exception.args[0], ano.TRANSCRIPT_RECORD_BYTES_REFUSAL)

    def test_record_one_over_max_record_bytes_refuses_when_built_across_many_small_chunks(self):
        payload = b'{"k":"' + b'x' * 91 + b'"}'
        chunks = [payload[i:i + 3] for i in range(0, len(payload), 3)] + [b'\n']
        with self.assertRaises(RoomError) as ctx:
            self._records(chunks, max_record_bytes=len(payload) - 1)
        self.assertEqual(ctx.exception.args[0], ano.TRANSCRIPT_RECORD_BYTES_REFUSAL)

    def test_unterminated_final_record_one_over_budget_refuses(self):
        payload = b'{"k":"' + b'x' * 91 + b'"}'  # no trailing terminator at all
        with self.assertRaises(RoomError) as ctx:
            self._records([payload], max_record_bytes=len(payload) - 1)
        self.assertEqual(ctx.exception.args[0], ano.TRANSCRIPT_RECORD_BYTES_REFUSAL)

    def test_record_count_exactly_at_cap_is_admitted(self):
        data = b''.join(b'{"i":%d}\n' % i for i in range(5))
        records = self._records([data], max_records=5)
        self.assertEqual(len(records), 5)

    def test_record_count_one_over_cap_refuses(self):
        data = b''.join(b'{"i":%d}\n' % i for i in range(6))
        with self.assertRaises(RoomError) as ctx:
            self._records([data], max_records=5)
        self.assertEqual(ctx.exception.args[0], ano.TRANSCRIPT_RECORD_COUNT_REFUSAL)

    def test_blank_lines_never_count_against_the_record_cap(self):
        data = b'\n\n\n{"a":1}\n\n\n{"b":2}\n\n\n'
        records = self._records([data], max_records=2)
        self.assertEqual(len(records), 2)

    def test_malformed_json_raises_with_the_given_message(self):
        with self.assertRaises(RoomError) as ctx:
            self._records([b'{not valid json\n'], malformed_message='custom malformed message')
        self.assertEqual(ctx.exception.args[0], 'custom malformed message')

    def test_malformed_json_default_message(self):
        with self.assertRaises(RoomError) as ctx:
            self._records([b'{not valid json\n'])
        self.assertEqual(ctx.exception.args[0], ano.TRANSCRIPT_MALFORMED_REFUSAL)

    def test_pathological_nesting_raises_recursion_error_as_malformed(self):
        # CPython's C-accelerated json scanner recurses once per array-nesting level without
        # going through the ordinary per-call Python frame machinery, so the nesting depth
        # needed to trip its own recursion guard is not governed by sys.setrecursionlimit()
        # and has moved sharply across CPython versions: measured directly, a fixed depth of
        # 3000 reliably overflowed it on CPython 3.11 (threshold ~996, scales with
        # sys.setrecursionlimit()) but parsed clean through on CPython 3.12 (threshold ~9998,
        # unaffected by sys.setrecursionlimit()) -- the CI failure this guards against. A
        # single hardcoded depth is therefore not a reliable probe across interpreters. Grow
        # the nesting in-process until this interpreter's own json.loads actually refuses it,
        # then prove iter_transcript_records converts that refusal into the caller's
        # RoomError, the same way it does for any other malformed row.
        depth = 1000
        while True:
            blob = b'[' * depth + b']' * depth
            try:
                json.loads(blob)
            except RecursionError:
                break
            depth *= 2
            self.assertLess(depth, 1 << 22, 'json.loads never refused any nesting depth tried')
        blob += b'\n'
        with self.assertRaises(RoomError) as ctx:
            self._records([blob], max_record_bytes=len(blob), malformed_message='too deep')
        self.assertEqual(ctx.exception.args[0], 'too deep')

    def test_non_dict_rows_are_yielded_unchanged(self):
        data = b'42\n"text"\n[1,2]\ntrue\nnull\n'
        records = self._records([data])
        self.assertEqual([row for _, _, row in records], [42, 'text', [1, 2], True, None])

    def test_empty_input_yields_nothing(self):
        self.assertEqual(self._records([]), [])
        self.assertEqual(self._records([b'']), [])

    def test_all_blank_input_yields_nothing(self):
        self.assertEqual(self._records([b'\n\n   \n\t\n']), [])

    # -- H3: chunk_bytes/budgets must be positive ints; a whitespace-only line is still bounded --

    def test_whitespace_only_line_longer_than_max_record_bytes_refuses(self):
        # The budget is checked against every physical line's raw length before ``settle`` ever
        # looks at whether it is blank: a whitespace-only line longer than the budget still refuses,
        # even though a shorter whitespace-only line is silently skipped and never counted.
        line = b' ' * 50 + b'\n'
        with self.assertRaises(RoomError) as ctx:
            self._records([line], max_record_bytes=49)
        self.assertEqual(ctx.exception.args[0], ano.TRANSCRIPT_RECORD_BYTES_REFUSAL)

    def test_whitespace_only_line_at_or_under_the_budget_is_still_skipped(self):
        line = b' ' * 49 + b'\n' + b'{"a":1}\n'
        records = self._records([line], max_record_bytes=49, max_records=1)
        self.assertEqual([row for _, _, row in records], [{'a': 1}])

    def test_non_positive_or_non_integer_budgets_raise_value_error(self):
        for kwargs in ({'max_record_bytes': 0}, {'max_record_bytes': -1}, {'max_record_bytes': 1.5},
                       {'max_records': 0}, {'max_records': -1}, {'max_records': 1.5}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    self._records([b'{"a":1}\n'], **kwargs)


# -------------------------------------------------------------------------------------------------
# read_complete_transcript: the complete file-identity-checked, streamed, hashed reader.
# -------------------------------------------------------------------------------------------------

class ReadCompleteTranscriptTests(unittest.TestCase):

    def setUp(self):
        self.dir = _tempdir(self)
        self.path = self.dir / 'x.jsonl'

    def test_basic_read_returns_the_exact_contract_keys(self):
        data = b'{"a":1}\n{"b":2}\n'
        _write(self.path, data)
        reading = ano.read_complete_transcript(self.path, unsafe_message='unsafe', changed_message='changed')
        self.assertEqual(set(reading), {'sha256', 'size', 'records', 'events', 'offsets', 'ends', 'prefix_sha256'})
        self.assertEqual(reading['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(reading['size'], len(data))
        self.assertEqual(reading['records'], 2)
        self.assertEqual(reading['events'], [{'a': 1}, {'b': 2}])
        self.assertIsInstance(reading['offsets'], array.array)
        self.assertIsInstance(reading['ends'], array.array)
        self.assertEqual(list(reading['offsets']), [0, 8])
        self.assertEqual(list(reading['ends']), [7, 15])
        self.assertIsNone(reading['prefix_sha256'])

    def test_prefix_bytes_none_leaves_prefix_sha256_none(self):
        _write(self.path, b'{"a":1}\n')
        reading = ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c',
                                                prefix_bytes=None)
        self.assertIsNone(reading['prefix_sha256'])

    def test_prefix_bytes_zero_is_the_empty_digest(self):
        _write(self.path, b'{"a":1}\n')
        reading = ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c',
                                                prefix_bytes=0)
        self.assertEqual(reading['prefix_sha256'], hashlib.sha256(b'').hexdigest())

    def test_prefix_bytes_mid_file(self):
        data = b'{"a":1}\n{"b":2}\n'
        _write(self.path, data)
        for cut in (0, 1, 7, 8, 9, len(data) - 1, len(data)):
            with self.subTest(cut=cut):
                reading = ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c',
                                                        prefix_bytes=cut)
                self.assertEqual(reading['prefix_sha256'], hashlib.sha256(data[:cut]).hexdigest())

    def test_max_bytes_boundary_exact_is_admitted_one_over_refuses(self):
        _write(self.path, b'{"a":1}\n')
        exact = self.path.stat().st_size
        reading = ano.read_complete_transcript(self.path, unsafe_message='unsafe', changed_message='c',
                                                max_bytes=exact)
        self.assertEqual(reading['size'], exact)
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='unsafe', changed_message='c',
                                          max_bytes=exact - 1)
        self.assertEqual(ctx.exception.args[0], 'unsafe')

    def test_missing_file_without_unavailable_message_raises_raw_oserror(self):
        with self.assertRaises(OSError):
            ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c')

    def test_missing_file_with_unavailable_message_raises_room_error(self):
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c',
                                          unavailable_message='gone')
        self.assertEqual(ctx.exception.args[0], 'gone')

    def test_symlink_without_unavailable_message_raises_raw_oserror(self):
        real = self.dir / 'real.jsonl'
        _write(real, b'{}\n')
        link = self.dir / 'link.jsonl'
        link.symlink_to(real)
        with self.assertRaises(OSError):
            ano.read_complete_transcript(link, unsafe_message='u', changed_message='c')

    def test_symlink_with_unavailable_message_raises_room_error(self):
        real = self.dir / 'real.jsonl'
        _write(real, b'{}\n')
        link = self.dir / 'link.jsonl'
        link.symlink_to(real)
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(link, unsafe_message='u', changed_message='c',
                                          unavailable_message='gone')
        self.assertEqual(ctx.exception.args[0], 'gone')

    def test_directory_at_path_raises_unsafe_message_not_isadirectoryerror(self):
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.dir, unsafe_message='unsafe-dir', changed_message='c')
        self.assertEqual(ctx.exception.args[0], 'unsafe-dir')

    def test_group_writable_mode_refuses(self):
        _write(self.path, b'{}\n')
        os.chmod(self.path, 0o664)
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='unsafe-mode', changed_message='c')
        self.assertEqual(ctx.exception.args[0], 'unsafe-mode')

    def test_mode_0o644_is_admitted(self):
        _write(self.path, b'{}\n')
        os.chmod(self.path, 0o644)
        reading = ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c')
        self.assertEqual(reading['records'], 1)

    def test_malformed_record_uses_the_given_message(self):
        _write(self.path, b'{not valid json\n')
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c',
                                          malformed_message='custom malformed')
        self.assertEqual(ctx.exception.args[0], 'custom malformed')

    def test_record_byte_budget_propagates(self):
        _write(self.path, b'{"k":"xxxxxxxxxx"}\n')
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c', max_record_bytes=8)
        self.assertEqual(ctx.exception.args[0], ano.TRANSCRIPT_RECORD_BYTES_REFUSAL)

    def test_record_count_budget_propagates(self):
        _write(self.path, b'{"a":1}\n{"b":2}\n{"c":3}\n')
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c', max_records=2)
        self.assertEqual(ctx.exception.args[0], ano.TRANSCRIPT_RECORD_COUNT_REFUSAL)

    def test_chunk_bytes_smaller_than_file_still_reads_correctly(self):
        data = b''.join(b'{"i":%d}\n' % i for i in range(200))
        _write(self.path, data)
        reading = ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c',
                                                chunk_bytes=17)
        self.assertEqual(reading['records'], 200)
        self.assertEqual(reading['sha256'], hashlib.sha256(data).hexdigest())

    # -- mutation-during-read and descriptor-closure, via the _read_chunk test seam --

    def _patch_read_chunk(self, replacement):
        return _patch_read_chunk(self, replacement)

    def test_append_during_read_raises_changed_message(self):
        _write(self.path, b'{"a":1}\n' * 5)
        original = self._patch_read_chunk(None)
        grown = []

        def spy(fd, n):
            chunk = original(fd, n)
            if chunk and not grown:
                grown.append(True)
                with open(self.path, 'ab') as fh:
                    fh.write(b'{"extra":1}\n')
            return chunk
        ano._read_chunk = spy
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='grew-mid-read',
                                          chunk_bytes=8)
        self.assertEqual(ctx.exception.args[0], 'grew-mid-read')

    def test_truncate_mid_record_during_read_raises_changed_not_malformed(self):
        # Regression guard for the exact bug this unit found and fixed: a shrink that lands inside a
        # record's bytes (not on a record boundary) must surface as the mutation refusal, never be
        # misread as a malformed/partial JSON record.
        _write(self.path, b'{"abcdefghijklmnop":1}\n' * 5)
        original = self._patch_read_chunk(None)
        cut = []

        def spy(fd, n):
            chunk = original(fd, n)
            if chunk and not cut:
                cut.append(True)
                os.truncate(str(self.path), 11)  # mid-way through the first record, no terminator
            return chunk
        ano._read_chunk = spy
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='shrank-mid-read',
                                          chunk_bytes=8)
        self.assertEqual(ctx.exception.args[0], 'shrank-mid-read')

    def test_path_swapped_for_a_different_file_during_read_raises_changed_message(self):
        _write(self.path, b'{"a":1}\n' * 5)
        original = self._patch_read_chunk(None)
        swapped = []

        def spy(fd, n):
            chunk = original(fd, n)
            if chunk and not swapped:
                swapped.append(True)
                other = self.path.with_suffix('.other')
                _write(other, b'{"a":1}\n' * 5)  # same size, different identity
                os.replace(str(other), str(self.path))
            return chunk
        ano._read_chunk = spy
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='swapped-mid-read',
                                          chunk_bytes=8)
        self.assertEqual(ctx.exception.args[0], 'swapped-mid-read')

    # -- F6d: close() is observed directly (mock.patch.object on os.open/os.close as imported into
    # ano), not inferred from a post-close fstat failure, on every refusal path plus success. --

    def _patch_open_close(self):
        """Wrap ``ano.os.open``/``ano.os.close`` (the ``os`` module as imported into ``ano``) so every
        fd this call opens, and every fd this call closes, is recorded while still delegating to the
        real syscalls; returns the two recording lists."""
        real_open, real_close = ano.os.open, ano.os.close
        opened, closed = [], []

        def open_recorder(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        def close_recorder(fd):
            closed.append(fd)
            return real_close(fd)

        patch_open = mock.patch.object(ano.os, 'open', side_effect=open_recorder)
        patch_close = mock.patch.object(ano.os, 'close', side_effect=close_recorder)
        patch_open.start()
        patch_close.start()
        self.addCleanup(patch_open.stop)
        self.addCleanup(patch_close.stop)
        return opened, closed

    def test_descriptor_close_called_exactly_once_on_success(self):
        _write(self.path, b'{"a":1}\n')
        opened, closed = self._patch_open_close()
        ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c')
        self.assertEqual(len(opened), 1)
        self.assertEqual(closed, opened)

    def test_descriptor_close_called_exactly_once_on_unsafe_refusal(self):
        _write(self.path, b'{"a":1}\n')
        os.chmod(self.path, 0o664)
        opened, closed = self._patch_open_close()
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='unsafe', changed_message='c')
        self.assertEqual(ctx.exception.args[0], 'unsafe')
        self.assertEqual(len(opened), 1)
        self.assertEqual(closed, opened)

    def test_descriptor_close_called_exactly_once_on_changed_refusal(self):
        _write(self.path, b'{"a":1}\n' * 5)
        original = self._patch_read_chunk(None)
        opened, closed = self._patch_open_close()
        grown = []

        def spy(fd, n):
            chunk = original(fd, n)
            if chunk and not grown:
                grown.append(True)
                with open(self.path, 'ab') as fh:
                    fh.write(b'{"extra":1}\n')
            return chunk
        ano._read_chunk = spy
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='grew-mid-read',
                                          chunk_bytes=8)
        self.assertEqual(ctx.exception.args[0], 'grew-mid-read')
        self.assertEqual(len(opened), 1)
        self.assertEqual(closed, opened)

    def test_descriptor_close_called_exactly_once_on_malformed_refusal(self):
        _write(self.path, b'{not valid json\n')
        opened, closed = self._patch_open_close()
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c')
        self.assertEqual(ctx.exception.args[0], ano.TRANSCRIPT_MALFORMED_REFUSAL)
        self.assertEqual(len(opened), 1)
        self.assertEqual(closed, opened)

    def test_descriptor_close_called_exactly_once_on_record_bytes_budget_refusal(self):
        _write(self.path, b'{"k":"xxxxxxxxxx"}\n')
        opened, closed = self._patch_open_close()
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c', max_record_bytes=8)
        self.assertEqual(ctx.exception.args[0], ano.TRANSCRIPT_RECORD_BYTES_REFUSAL)
        self.assertEqual(len(opened), 1)
        self.assertEqual(closed, opened)

    def test_descriptor_close_called_exactly_once_on_record_count_budget_refusal(self):
        _write(self.path, b'{"a":1}\n{"b":2}\n{"c":3}\n')
        opened, closed = self._patch_open_close()
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c', max_records=2)
        self.assertEqual(ctx.exception.args[0], ano.TRANSCRIPT_RECORD_COUNT_REFUSAL)
        self.assertEqual(len(opened), 1)
        self.assertEqual(closed, opened)

    # -- uid ownership at the reader --

    def test_uid_mismatch_refuses_unsafe_and_reads_nothing(self):
        _write(self.path, b'{"a":1}\n')
        original = _patch_read_chunk(self, None)
        calls = []

        def spy(fd, n):
            calls.append(n)
            return original(fd, n)
        ano._read_chunk = spy
        with mock.patch('os.getuid', return_value=os.getuid() + 1):
            with self.assertRaises(RoomError) as ctx:
                ano.read_complete_transcript(self.path, unsafe_message='wrong-uid', changed_message='c')
        self.assertEqual(ctx.exception.args[0], 'wrong-uid')
        self.assertEqual(calls, [])  # the ownership check refuses before a single chunk is read

    # -- H3: chunk_bytes and every budget must be a positive int; prefix_bytes past EOF yields None --

    def test_chunk_bytes_zero_raises_value_error(self):
        _write(self.path, b'{"a":1}\n')
        with self.assertRaises(ValueError):
            ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c', chunk_bytes=0)

    def test_non_positive_or_non_integer_budgets_raise_value_error(self):
        _write(self.path, b'{"a":1}\n')
        for kwargs in ({'max_bytes': 0}, {'max_bytes': -1}, {'max_record_bytes': 0}, {'max_records': 0},
                       {'chunk_bytes': 0}, {'chunk_bytes': -1}, {'chunk_bytes': 1.5}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c', **kwargs)

    def test_prefix_bytes_greater_than_file_size_yields_none(self):
        data = b'{"a":1}\n'
        _write(self.path, data)
        reading = ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c',
                                                prefix_bytes=len(data) + 1000)
        self.assertIsNone(reading['prefix_sha256'])


# -------------------------------------------------------------------------------------------------
# F3: a parse refusal from iter_transcript_records (malformed, record-bytes or record-count) is
# itself rechecked against descriptor/named-path identity before it reaches the caller, so a
# concurrent mutation that manufactures the refusal is reported as changed, not malformed.
# -------------------------------------------------------------------------------------------------

class ParseRefusalIdentityRecheckTests(unittest.TestCase):

    def setUp(self):
        self.dir = _tempdir(self)
        self.path = self.dir / 'x.jsonl'

    def test_same_size_in_place_rewrite_turning_a_row_malformed_raises_changed_not_malformed(self):
        # The inode itself is edited in place (not swapped), so the open descriptor's own fstat
        # moves. A same-size rewrite that turns the still-unread tail into invalid JSON must be
        # reported as changed, never malformed: the bytes the read finishes consuming were never all
        # part of the one consistent file ``before`` described.
        original = b'{"a":1}\n{"b":2}\n'  # 16 bytes, read in two 8-byte chunks below
        _write(self.path, original)
        before_mtime_ns = self.path.stat().st_mtime_ns
        original_read = _patch_read_chunk(self, None)
        rewritten = []

        def spy(fd, n):
            chunk = original_read(fd, n)
            if chunk and not rewritten:
                rewritten.append(True)
                with open(self.path, 'r+b') as fh:
                    fh.seek(8)
                    fh.write(b'NOT-JSON')  # same 8-byte length; no longer valid/terminated JSON
                # Force a distinct mtime_ns deterministically instead of depending on the host
                # filesystem's clock resolution happening to tick between the two writes.
                os.utime(self.path, ns=(before_mtime_ns + 1_000_000, before_mtime_ns + 1_000_000))
            return chunk
        ano._read_chunk = spy
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='rewritten-mid-read',
                                          chunk_bytes=8)
        self.assertEqual(ctx.exception.args[0], 'rewritten-mid-read')

    def test_torn_final_row_completed_by_writer_after_eof_read_raises_changed_not_malformed(self):
        # The file ends with an unterminated (torn) last row when ``before`` is captured; the chunk
        # loop itself only ever consumes exactly ``before.st_size`` bytes (no growth is visible
        # mid-stream), but by the time the leftover tail is parsed as the final record, a concurrent
        # writer has already appended the bytes that complete it.
        torn = b'{"a":1}\n{"b":2'  # one complete record, one torn/unterminated tail, 15 bytes
        _write(self.path, torn)
        original_read = _patch_read_chunk(self, None)
        completed = []

        def spy(fd, n):
            chunk = original_read(fd, n)
            if not chunk and not completed:
                completed.append(True)
                with open(self.path, 'ab') as fh:
                    fh.write(b'}\n')  # completes the torn row after our EOF read observed 15 bytes
            return chunk
        ano._read_chunk = spy
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='completed-after-eof',
                                          chunk_bytes=64)
        self.assertEqual(ctx.exception.args[0], 'completed-after-eof')

    def test_truncation_mid_read_raises_changed_not_malformed(self):
        _write(self.path, b'{"abcdefghijklmnop":1}\n' * 5)
        original_read = _patch_read_chunk(self, None)
        cut = []

        def spy(fd, n):
            chunk = original_read(fd, n)
            if chunk and not cut:
                cut.append(True)
                os.truncate(str(self.path), 11)  # mid-way through the first record, no terminator
            return chunk
        ano._read_chunk = spy
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='shrank-mid-read',
                                          chunk_bytes=8)
        self.assertEqual(ctx.exception.args[0], 'shrank-mid-read')

    def test_unchanged_file_with_an_early_malformed_row_still_raises_malformed_message(self):
        # The malformed row is near the start, so the refusal fires long before the read reaches EOF
        # (total well short of before.st_size at the moment of failure): an unchanged file must still
        # be reported as malformed, not misclassified as changed merely because the read had not yet
        # finished consuming the rest of the (untouched) file.
        data = b'{not valid json\n' + b'{"pad":1}\n' * 5000
        _write(self.path, data)
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c',
                                          malformed_message='early-malformed', chunk_bytes=64)
        self.assertEqual(ctx.exception.args[0], 'early-malformed')

    def test_unchanged_file_malformed_at_eof_still_raises_malformed_message(self):
        _write(self.path, b'{not valid json\n')
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c',
                                          malformed_message='static-malformed')
        self.assertEqual(ctx.exception.args[0], 'static-malformed')

    def test_record_count_budget_refusal_during_a_touched_but_otherwise_unmutated_read_prefers_changed(self):
        # Only mtime moves (os.utime; no byte of content or size changes) and the record-count
        # budget genuinely is exceeded. The fix applies uniformly to every iter_transcript_records
        # refusal type, not just malformed, so the touched identity must still win.
        _write(self.path, b'{"a":1}\n{"b":2}\n{"c":3}\n')
        before_mtime_ns = self.path.stat().st_mtime_ns
        original_read = _patch_read_chunk(self, None)
        touched = []

        def spy(fd, n):
            chunk = original_read(fd, n)
            if chunk and not touched:
                touched.append(True)
                os.utime(self.path, ns=(before_mtime_ns + 1_000_000, before_mtime_ns + 1_000_000))
            return chunk
        ano._read_chunk = spy
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='touched-mid-read',
                                          max_records=2, chunk_bytes=8)
        self.assertEqual(ctx.exception.args[0], 'touched-mid-read')


# -------------------------------------------------------------------------------------------------
# H1: the identity recheck also compares st_ctime_ns, st_uid and st_mode (not just inode/size/mtime)
# between the before-fstat and both the after-fstat and the named lstat.
# -------------------------------------------------------------------------------------------------

class IdentityRecheckCtimeOwnerModeTests(unittest.TestCase):

    def setUp(self):
        self.dir = _tempdir(self)
        self.path = self.dir / 'x.jsonl'

    def test_chmod_during_read_raises_changed_message(self):
        _write(self.path, b'{"a":1}\n' * 5)
        os.chmod(self.path, 0o644)
        original = _patch_read_chunk(self, None)
        chmodded = []

        def spy(fd, n):
            chunk = original(fd, n)
            if chunk and not chmodded:
                chmodded.append(True)
                os.chmod(self.path, 0o600)  # same owner/content/size; only mode (and ctime) move
            return chunk
        ano._read_chunk = spy
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='chmodded-mid-read',
                                          chunk_bytes=8)
        self.assertEqual(ctx.exception.args[0], 'chmodded-mid-read')

    def test_same_size_rewrite_with_mtime_restored_by_os_utime_raises_changed_message(self):
        # The writer rewrites the file's bytes in place (identical content, same total size) and then
        # calls os.utime to restore the exact original mtime_ns -- hiding from an mtime-only identity
        # check. st_ctime_ns still moves, because both the write and the utime call themselves bump
        # the inode's own change time, and this recheck compares ctime explicitly.
        original_content = b'{"a":1}\n' * 5
        _write(self.path, original_content)
        before_stat = self.path.stat()
        original = _patch_read_chunk(self, None)
        rewritten = []

        def spy(fd, n):
            chunk = original(fd, n)
            if chunk and not rewritten:
                rewritten.append(True)
                with open(self.path, 'r+b') as fh:
                    fh.seek(0)
                    fh.write(original_content)
                os.utime(self.path, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
                after_stat = self.path.stat()
                # Sanity: the restore worked (mtime back to the original) and ctime still moved --
                # otherwise this test would not actually be exercising the ctime comparison at all.
                if after_stat.st_mtime_ns != before_stat.st_mtime_ns or after_stat.st_ctime_ns == before_stat.st_ctime_ns:
                    raise AssertionError('fixture did not isolate mtime-restored/ctime-moved as expected')
            return chunk
        ano._read_chunk = spy
        with self.assertRaises(RoomError) as ctx:
            ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='ctime-moved-mid-read',
                                          chunk_bytes=8)
        self.assertEqual(ctx.exception.args[0], 'ctime-moved-mid-read')

    def test_unchanged_file_recheck_still_passes_with_the_new_ctime_uid_mode_fields(self):
        _write(self.path, b'{"a":1}\n')
        descriptor = os.open(self.path, os.O_RDONLY)
        try:
            before = os.fstat(descriptor)
            self.assertTrue(ano._transcript_identity_recheck(descriptor, self.path, before, before.st_size))
        finally:
            os.close(descriptor)
        reading = ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c')
        self.assertEqual(reading['records'], 1)


# -------------------------------------------------------------------------------------------------
# F4: terminator RUNS are matched and skipped in one step. A blank-line flood no longer costs one
# Python-level loop iteration per character, and the run-collapsing is exactly splitlines()-exact.
# -------------------------------------------------------------------------------------------------

class TerminatorRunPerformanceTests(unittest.TestCase):

    def test_4mib_newline_flood_parses_fast_with_zero_records(self):
        data = b'\n' * (4 * 1024 * 1024)
        t0 = time.perf_counter()
        records = list(ano.iter_transcript_records(iter([data])))
        elapsed = time.perf_counter() - t0
        self.assertEqual(records, [])
        self.assertLess(elapsed, 1.0)

    def test_4mib_crlf_flood_parses_fast_with_zero_records(self):
        data = b'\r\n' * (2 * 1024 * 1024)  # 4 MiB total
        t0 = time.perf_counter()
        records = list(ano.iter_transcript_records(iter([data])))
        elapsed = time.perf_counter() - t0
        self.assertEqual(records, [])
        self.assertLess(elapsed, 1.0)

    def test_newline_flood_delivered_in_single_byte_chunks_still_parses_fast(self):
        # Exercises the carried-forward scan position across many tiny chunks, not just one big one.
        data = b'\n' * (64 * 1024)
        chunks = [data[i:i + 1] for i in range(len(data))]
        t0 = time.perf_counter()
        records = list(ano.iter_transcript_records(iter(chunks)))
        elapsed = time.perf_counter() - t0
        self.assertEqual(records, [])
        self.assertLess(elapsed, 1.0)


_SPECIAL_ROW_BYTES = (b'\x0b', b'\x0c', b'\x1c', b'\x1d', b'\x1e', b'\x85', b'\xef\xbb\xbf')
_FUZZ_TERMINATORS = (b'\n', b'\r', b'\r\n')
_FUZZ_BLANKISH_LINES = (b'', b'   ', b'\t', b' \t ')


def _fuzz_row(rng):
    """One line's content: ordinary JSON, a non-dict JSON row, a row with a non-terminator special
    byte spliced in, or deliberate garbage -- never containing a literal ``\\r``/``\\n`` itself."""
    kind = rng.randrange(4)
    if kind == 0:
        return b'{"i":%d}' % rng.randrange(1000)
    if kind == 1:
        return rng.choice((b'42', b'true', b'false', b'null', b'[1,2,3]', b'"text"'))
    if kind == 2:
        return b'{"k":"a' + rng.choice(_SPECIAL_ROW_BYTES) + b'b"}'
    return b'{garbage' + rng.choice(_SPECIAL_ROW_BYTES)  # deliberately unparseable


def _fuzz_document(rng, n_rows=24):
    parts = []
    for _ in range(n_rows):
        parts.append(rng.choice(_FUZZ_BLANKISH_LINES) if rng.randrange(3) == 0 else _fuzz_row(rng))
        parts.append(rng.choice(_FUZZ_TERMINATORS))
    if parts and rng.random() < 0.5:
        parts.pop()  # occasionally leave the final record unterminated
    return b''.join(parts)


def _fuzz_random_chunks(rng, raw):
    chunks = []
    i = 0
    while i < len(raw):
        size = rng.randrange(1, 65)
        chunks.append(raw[i:i + size])
        i += size
    return chunks or [b'']


class TerminatorRunFuzzTests(unittest.TestCase):
    """Seeded fuzz: iter_transcript_records over random chunkings must match the legacy
    ``[json.loads(l) for l in raw.splitlines() if l.strip()]`` per-line parse exactly, including the
    precise byte offsets of every yielded record."""

    def test_fuzz_matches_legacy_splitlines_parse_over_3000_random_chunkings(self):
        rng = random.Random(0)
        for case_index in range(3000):
            raw = _fuzz_document(rng)
            try:
                baseline_rows = [json.loads(l) for l in raw.splitlines() if l.strip()]
                baseline_failed = False
            except Exception:
                baseline_rows = None
                baseline_failed = True

            try:
                records = list(ano.iter_transcript_records(iter(_fuzz_random_chunks(rng, raw))))
                impl_failed = False
            except RoomError:
                records = None
                impl_failed = True

            with self.subTest(case=case_index, raw=raw):
                self.assertEqual(baseline_failed, impl_failed)
                if not baseline_failed:
                    self.assertEqual([row for _, _, row in records], baseline_rows)
                    nonblank = [line for line in raw.splitlines() if line.strip()]
                    self.assertEqual(len(records), len(nonblank))
                    for (start, end, row), line in zip(records, nonblank):
                        self.assertEqual(raw[start:end], line)
                        self.assertEqual(json.loads(raw[start:end]), row)


# -------------------------------------------------------------------------------------------------
# F10: read_complete_transcript's budget/chunk defaults resolve the current module constant at call
# time (mock.patch.object), not at import/definition time.
# -------------------------------------------------------------------------------------------------

class LateBoundModuleConstantDefaultsTests(unittest.TestCase):

    def setUp(self):
        self.dir = _tempdir(self)
        self.path = self.dir / 'x.jsonl'

    def test_patching_max_transcript_bytes_moves_the_size_gate_default(self):
        _write(self.path, b'{"a":1}\n')
        size = self.path.stat().st_size
        with mock.patch.object(ano, 'MAX_TRANSCRIPT_BYTES', size - 1):
            with self.assertRaises(RoomError) as ctx:
                ano.read_complete_transcript(self.path, unsafe_message='too-big', changed_message='c')
            self.assertEqual(ctx.exception.args[0], 'too-big')
        # Unpatched again: the real (large) module default takes back over on the very next call.
        reading = ano.read_complete_transcript(self.path, unsafe_message='too-big', changed_message='c')
        self.assertEqual(reading['size'], size)

    def test_patching_max_transcript_record_bytes_moves_the_record_budget_default(self):
        _write(self.path, b'{"k":"xxxxxxxxxx"}\n')
        with mock.patch.object(ano, 'MAX_TRANSCRIPT_RECORD_BYTES', 8):
            with self.assertRaises(RoomError) as ctx:
                ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c')
            self.assertEqual(ctx.exception.args[0], ano.TRANSCRIPT_RECORD_BYTES_REFUSAL)
        reading = ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c')
        self.assertEqual(reading['records'], 1)

    def test_patching_max_transcript_records_moves_the_record_count_budget_default(self):
        _write(self.path, b'{"a":1}\n{"b":2}\n{"c":3}\n')
        with mock.patch.object(ano, 'MAX_TRANSCRIPT_RECORDS', 2):
            with self.assertRaises(RoomError) as ctx:
                ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c')
            self.assertEqual(ctx.exception.args[0], ano.TRANSCRIPT_RECORD_COUNT_REFUSAL)
        reading = ano.read_complete_transcript(self.path, unsafe_message='u', changed_message='c')
        self.assertEqual(reading['records'], 3)


# -------------------------------------------------------------------------------------------------
# H2: validate_registered_source's transcript_size_limit resolves MAX_TRANSCRIPT_BYTES at call time,
# not at this function's own definition time, exactly like read_complete_transcript's None defaults
# above; an explicit None still lifts the limit entirely.
# -------------------------------------------------------------------------------------------------

class ValidateRegisteredSourceLateBoundSizeLimitTests(unittest.TestCase):
    """Minimal hand-built state/source: read_owner and validate_preparation are the only two
    cross-module calls validate_registered_source makes, so both are stubbed directly rather than
    standing up a real AO owner database and git worktree, keeping this module's own tests isolated
    and fast (see the module docstring)."""

    def setUp(self):
        self.dir = _tempdir(self)
        self.native = 'native-h2'
        self.transcript = self.dir / (self.native + '.jsonl')
        _write(self.transcript, b'{"a":1}\n' * 20)  # 160 bytes; content is never read by this function
        self.workspace = str(self.dir)
        self.state = {
            'bindings': {'engineer': {'harness': 'claude-code', 'session_id': 'sess-1',
                                      'conversation_id': 'conv-1', 'branch_id': 'branch-1'}},
            'ao_project_id': 'proj-1',
            'native_outcome_source': {'database': str(self.dir / 'owner.sqlite'),
                                      'transcript': str(self.transcript), 'session_id': 'sess-1',
                                      'native_session_id': self.native},
        }
        self.owner = {'project_id': 'proj-1', 'ao_conversation_id': 'conv-1', 'active_branch_id': 'branch-1',
                      'provider_conversation_id': self.native, 'workspace_path': self.workspace}
        self.size = self.transcript.stat().st_size
        patch_owner = mock.patch('ao_native_identity.read_owner', return_value=self.owner)
        patch_prep = mock.patch('ao_delegates.validate_preparation', return_value={'worktree': self.workspace})
        patch_owner.start()
        patch_prep.start()
        self.addCleanup(patch_owner.stop)
        self.addCleanup(patch_prep.stop)

    def test_read_it_first_the_registered_source_is_admitted_at_the_real_default(self):
        result = ano.validate_registered_source(self.dir, self.state, 'engineer')
        self.assertTrue(result['transcript_present'])

    def test_patched_max_transcript_bytes_refuses_the_already_registered_transcript(self):
        with mock.patch.object(ano, 'MAX_TRANSCRIPT_BYTES', self.size - 1):
            with self.assertRaises(RoomError) as ctx:
                ano.validate_registered_source(self.dir, self.state, 'engineer')
            self.assertEqual(ctx.exception.args[0], 'Prospective native transcript file is unsafe')
        # Unpatched again: the real (large) module default takes back over on the very next call,
        # proving the bound happened at call time, not when this function (or its default) was defined.
        result = ano.validate_registered_source(self.dir, self.state, 'engineer')
        self.assertTrue(result['transcript_present'])

    def test_explicit_none_still_lifts_the_limit_even_when_the_module_default_is_patched_small(self):
        with mock.patch.object(ano, 'MAX_TRANSCRIPT_BYTES', self.size - 1):
            result = ano.validate_registered_source(self.dir, self.state, 'engineer', transcript_size_limit=None)
        self.assertTrue(result['transcript_present'])


# -------------------------------------------------------------------------------------------------
# The two production call sites that layer their own logic on top of the shared reader.
# -------------------------------------------------------------------------------------------------

class AllowedToolUsesBoundaryTests(unittest.TestCase):
    """ao_spec_verdict_recovery._allowed_tool_uses's prefix/straddle/suffix classification."""

    @staticmethod
    def _reading(events, offsets, ends):
        return {'events': events, 'offsets': array.array('q', offsets), 'ends': array.array('q', ends)}

    def test_clean_split_counts_only_the_new_tool_use(self):
        prefix_row = {'uuid': 'p1', 'type': 'assistant', 'message': {'content': []}}
        suffix_row = {'uuid': 's1', 'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'tool-1', 'name': 'TaskCreate', 'input': {}}]}}
        reading = self._reading([prefix_row, suffix_row], [0, 8], [8, 20])
        allowed = svr._allowed_tool_uses(reading, {'bytes': 8})
        self.assertEqual(allowed, {'tool-1'})

    def test_boundary_exact_record_is_prefix_not_suffix(self):
        # end == boundary exactly (the previously-unterminated last prefix row gained its terminator):
        # it must be treated as already-retained, contributing nothing to "allowed".
        row = {'uuid': 'p1', 'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'tool-1', 'name': 'TaskCreate', 'input': {}}]}}
        reading = self._reading([row], [0], [8])
        allowed = svr._allowed_tool_uses(reading, {'bytes': 8})
        self.assertEqual(allowed, set())

    def test_straddling_record_refuses(self):
        # start < boundary < end: part of its bytes were already retained, part are new.
        row = {'uuid': 'z1', 'type': 'assistant', 'message': {'content': []}}
        reading = self._reading([row], [3], [12])
        with self.assertRaises(RoomError) as ctx:
            svr._allowed_tool_uses(reading, {'bytes': 8})
        self.assertEqual(ctx.exception.args[0], svr.TOOL_REFUSAL)

    def test_suffix_row_reusing_a_prefix_uuid_is_not_double_counted(self):
        prefix_row = {'uuid': 'p1', 'type': 'assistant', 'message': {'content': []}}
        reused = {'uuid': 'p1', 'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'tool-2', 'name': 'TaskCreate', 'input': {}}]}}
        reading = self._reading([prefix_row, reused], [0, 8], [8, 20])
        allowed = svr._allowed_tool_uses(reading, {'bytes': 8})
        self.assertEqual(allowed, set())

    def test_suffix_disallowed_tool_name_refuses(self):
        row = {'uuid': 's1', 'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'tool-1', 'name': 'Bash', 'input': {}}]}}
        reading = self._reading([row], [8], [20])
        with self.assertRaises(RoomError) as ctx:
            svr._allowed_tool_uses(reading, {'bytes': 8})
        self.assertEqual(ctx.exception.args[0], svr.TOOL_REFUSAL)

    def test_suffix_sidechain_row_refuses(self):
        row = {'uuid': 's1', 'type': 'assistant', 'isSidechain': True, 'message': {'content': []}}
        reading = self._reading([row], [8], [20])
        with self.assertRaises(RoomError) as ctx:
            svr._allowed_tool_uses(reading, {'bytes': 8})
        self.assertEqual(ctx.exception.args[0], svr.TOOL_REFUSAL)

    def test_zero_prior_bytes_treats_every_record_as_suffix(self):
        row = {'uuid': 's1', 'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'tool-1', 'name': 'TaskCreate', 'input': {}}]}}
        reading = self._reading([row], [0], [12])
        allowed = svr._allowed_tool_uses(reading, {'bytes': 0})
        self.assertEqual(allowed, {'tool-1'})

    def test_unchanged_total_yields_no_allowed_tool_uses(self):
        row = {'uuid': 'p1', 'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'tool-1', 'name': 'TaskCreate', 'input': {}}]}}
        reading = self._reading([row], [0], [8])
        allowed = svr._allowed_tool_uses(reading, {'bytes': 8})
        self.assertEqual(allowed, set())

    # -- H4: an unhashable uuid on a suffix row must refuse, not escape as a bare TypeError --

    def test_unhashable_uuid_in_suffix_row_refuses(self):
        for bad_uuid in (['unhashable'], {'nested': 'dict'}):
            with self.subTest(uuid_type=type(bad_uuid).__name__):
                row = {'uuid': bad_uuid, 'type': 'assistant', 'message': {'content': []}}
                reading = self._reading([row], [8], [20])
                with self.assertRaises(RoomError) as ctx:
                    svr._allowed_tool_uses(reading, {'bytes': 8})
                self.assertEqual(ctx.exception.args[0], svr.TOOL_REFUSAL)

    def test_none_uuid_in_suffix_row_behaves_as_before(self):
        # A hashable uuid (including None, when a row simply carries none) must keep today's exact
        # behaviour: no raise, and its tool_use block is still counted normally.
        row = {'uuid': None, 'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'tool-1', 'name': 'TaskCreate', 'input': {}}]}}
        reading = self._reading([row], [8], [20])
        allowed = svr._allowed_tool_uses(reading, {'bytes': 8})
        self.assertEqual(allowed, {'tool-1'})


class ReadTranscriptDictShapeTests(unittest.TestCase):
    """ao_engineering_transition._read_transcript's own non-dict row refusal."""

    def setUp(self):
        import ao_engineering_transition as et
        self.et = et
        self.dir = _tempdir(self)
        self.native = 'native-reader-unit'
        self.path = self.dir / (self.native + '.jsonl')

    def test_non_dict_row_refuses_even_without_calling_child_evidence(self):
        _write(self.path, b'{"uuid":"a","type":"user","sessionId":"x","cwd":"/y",'
                          b'"message":{"role":"user","content":"hi"}}\n42\n')
        with self.assertRaises(RoomError) as ctx:
            self.et._read_transcript(str(self.path), self.native)
        self.assertEqual(ctx.exception.args[0], self.et.MALFORMED_CHILD_EVIDENCE)

    def test_all_dict_rows_read_successfully(self):
        _write(self.path, b'{"uuid":"a","type":"user","sessionId":"x","cwd":"/y",'
                          b'"message":{"role":"user","content":"hi"}}\n')
        reading = self.et._read_transcript(str(self.path), self.native)
        self.assertEqual(reading['records'], 1)
        self.assertTrue(all(isinstance(row, dict) for row in reading['events']))

    def test_child_evidence_accepts_bytes_list_and_rejects_other_types(self):
        row = {'uuid': 'a', 'type': 'user', 'sessionId': self.native, 'cwd': '/workspace',
               'message': {'role': 'user', 'content': 'hi'}}
        raw_bytes = (json.dumps(row) + '\n').encode()
        from_bytes = self.et.child_evidence(raw_bytes, self.native, '/workspace')
        from_list = self.et.child_evidence([row], self.native, '/workspace')
        self.assertEqual(from_bytes, from_list)
        with self.assertRaises(RoomError) as ctx:
            self.et.child_evidence(12345, self.native, '/workspace')
        self.assertEqual(ctx.exception.args[0], 'The retained native transcript is unreadable')

    # -- H5: child_evidence's bytes and list branches each get their own finite-capacity budget --

    def test_bytes_over_max_transcript_bytes_refuses_unsafe_message(self):
        row = {'uuid': 'a', 'type': 'user', 'sessionId': self.native, 'cwd': '/workspace',
               'message': {'role': 'user', 'content': 'hi'}}
        raw_bytes = (json.dumps(row) + '\n').encode()
        with mock.patch.object(ano, 'MAX_TRANSCRIPT_BYTES', len(raw_bytes) - 1):
            with self.assertRaises(RoomError) as ctx:
                self.et.child_evidence(raw_bytes, self.native, '/workspace')
            self.assertEqual(ctx.exception.args[0], 'Native transcript ownership, type or size is unsafe')

    def test_list_over_max_transcript_records_refuses_count_message(self):
        rows = [{'uuid': 'a', 'type': 'user', 'sessionId': self.native, 'cwd': '/workspace',
                 'message': {'role': 'user', 'content': 'hi'}}]
        with mock.patch.object(ano, 'MAX_TRANSCRIPT_RECORDS', 0):
            with self.assertRaises(RoomError) as ctx:
                self.et.child_evidence(rows, self.native, '/workspace')
            self.assertEqual(ctx.exception.args[0], ano.TRANSCRIPT_RECORD_COUNT_REFUSAL)


if __name__ == '__main__':
    unittest.main()
