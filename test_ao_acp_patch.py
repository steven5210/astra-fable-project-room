"""Synthetic local modules only; never inspect or patch the host's AO installation."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ao_acp_patch as acp


class PinnedPatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.module = self.root / 'acp-agent.js'
        self.raw = b'// synthetic\n' + acp.OLD + b' first; }\n' + acp.OLD + b' second; }\n'
        self.module.write_bytes(self.raw)

    def test_unknown_versions_refuse_without_backup_or_mutation(self):
        with self.assertRaisesRegex(ValueError, 'differs'):
            acp.apply(str(self.module), str(self.root / 'backup'), True)
        self.assertEqual(self.module.read_bytes(), self.raw)
        self.assertFalse((self.root / 'backup').exists())

    def test_reviewed_shape_changes_only_both_precedence_conditions_and_is_idempotent(self):
        updated = self.raw.replace(acp.OLD, acp.NEW)
        table = {hashlib.sha256(self.raw).hexdigest(): hashlib.sha256(updated).hexdigest()}
        with patch.object(acp, 'PATCHED_SHA256', table):
            result = acp.apply(str(self.module), str(self.root / 'backup'), True)
            self.assertEqual(self.module.read_bytes().replace(acp.NEW, acp.OLD), self.raw)
            self.assertEqual(self.module.read_bytes().count(acp.NEW), 2)
            self.assertEqual(Path(result['backup']).read_bytes(), self.raw)
            self.assertEqual(acp.apply(str(self.module), str(self.root / 'backup'), True), result)
            self.assertTrue(result['native_controller_reload_required'])

    def test_reviewed_sources_and_patched_digests_are_pinned_literals(self):
        self.assertEqual(acp.REVIEWED_SOURCES, {
            '0.13.0/0.13.1': acp.SOURCE_SHA256,
            '0.13.3': '64cce3640aba406202e2fd1f4b6f65191ccea4db3b2cb5cb69edc97f94c5d8e4'})
        self.assertEqual(acp.PATCHED_SHA256, {
            acp.SOURCE_SHA256: acp.PRECEDENCE_SHA256,
            acp.REVIEWED_SOURCES['0.13.3']:
                'af5179e7a87f4eab3e9f8c114c148801aafe00a1fcdaca450395e8d2d1174477'})

    def test_the_second_reviewed_source_patches_through_the_table_and_idempotently(self):
        raw = (b'// synthetic 0.13.3 with the additive context-usage block\n' + acp.OLD + b' first; }\n'
               + acp.OLD + b' second; }\n'
               + b'const contextUsage = await session.getContextUsage({ signal: AbortSignal.timeout(2000) });\n')
        module = self.root / 'acp-agent-0.13.3.js'
        module.write_bytes(raw)
        updated = raw.replace(acp.OLD, acp.NEW)
        source, target = hashlib.sha256(raw).hexdigest(), hashlib.sha256(updated).hexdigest()
        with patch.object(acp, 'PATCHED_SHA256', {**acp.PATCHED_SHA256, source: target}):
            result = acp.apply(str(module), str(self.root / 'backup'), True)
            self.assertEqual(result['source_sha256'], source)
            self.assertEqual(result['target_sha256'], target)
            self.assertEqual(module.read_bytes(), updated)
            self.assertEqual(acp.apply(str(module), str(self.root / 'backup'), True), result)
        with self.assertRaisesRegex(ValueError, 'differs'):
            acp.patched(raw)  # outside the reviewed table the same bytes stay unknown
        with patch.object(acp, 'PATCHED_SHA256', {**acp.PATCHED_SHA256, source: target}):
            with self.assertRaisesRegex(ValueError, 'differs'):
                acp.patched(raw, completion_barrier=True)  # the barrier stays 0.13.0/0.13.1-only
        with patch.object(acp, 'PATCHED_SHA256', {source: 'f' * 64}):
            with self.assertRaisesRegex(ValueError, 'patched output'):
                acp.patched(raw)

    def test_idle_acknowledgment_symlinks_and_incomplete_shapes_refuse(self):
        with self.assertRaisesRegex(ValueError, 'idle'): acp.apply(str(self.module), str(self.root / 'backup'), False)
        link = self.root / 'link.js'; link.symlink_to(self.module)
        with self.assertRaisesRegex(ValueError, 'symlink'): acp.apply(str(link), str(self.root / 'backup'), True)
        with patch.object(acp, 'PATCHED_SHA256', {hashlib.sha256(acp.OLD).hexdigest(): 'ab' * 32}):
            with self.assertRaises(ValueError): acp.patched(acp.OLD)
