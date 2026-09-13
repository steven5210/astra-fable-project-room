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
        with patch.object(acp, 'SOURCE_SHA256', hashlib.sha256(self.raw).hexdigest()):
            result = acp.apply(str(self.module), str(self.root / 'backup'), True)
            self.assertEqual(self.module.read_bytes().replace(acp.NEW, acp.OLD), self.raw)
            self.assertEqual(self.module.read_bytes().count(acp.NEW), 2)
            self.assertEqual(Path(result['backup']).read_bytes(), self.raw)
            self.assertEqual(acp.apply(str(self.module), str(self.root / 'backup'), True), result)
            self.assertTrue(result['native_controller_reload_required'])

    def test_idle_acknowledgment_symlinks_and_incomplete_shapes_refuse(self):
        with self.assertRaisesRegex(ValueError, 'idle'): acp.apply(str(self.module), str(self.root / 'backup'), False)
        link = self.root / 'link.js'; link.symlink_to(self.module)
        with self.assertRaisesRegex(ValueError, 'symlink'): acp.apply(str(link), str(self.root / 'backup'), True)
        with patch.object(acp, 'SOURCE_SHA256', hashlib.sha256(acp.OLD).hexdigest()):
            with self.assertRaises(ValueError): acp.patched(acp.OLD)
