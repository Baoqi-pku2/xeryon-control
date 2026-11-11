"""A read-only Windows monitor must not abort the hardware stress run."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.stress_control import write_progress_report


class StressReportingTests(unittest.TestCase):
    def test_reader_lock_preserves_valid_report_and_next_write_recovers(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            path = Path(directory)/'progress.json'
            self.assertTrue(write_progress_report(path, {'count': 490}))
            with patch.object(Path, 'replace', side_effect=PermissionError('reader lock')):
                self.assertFalse(write_progress_report(path, {'count': 491}))
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'count': 490})
            self.assertTrue(write_progress_report(path, {'count': 492}))
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'count': 492})

    def test_real_write_failure_is_not_silently_suppressed(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            with patch.object(Path, 'write_text', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    write_progress_report(Path(directory)/'progress.json', {'count': 1})
