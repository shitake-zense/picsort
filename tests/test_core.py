import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Store, apply_decision, fingerprint, scan_images, verified_transfer


class FileSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="koma-test-", dir=os.environ.get("KOMA_TEST_DIR"))
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def photo(self, name="camera-a/IMG_001.jpg", content=b"test photo original bytes"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def decision(self, source, action="file"):
        return {"action": action, "fingerprint": fingerprint(source), "destination": str(self.root / "sorted")}

    def test_move_collision_preserves_both_cameras_and_existing_photo(self):
        first = self.photo()
        second = self.photo("camera-b/IMG_001.jpg", b"second camera")
        existing = self.photo("sorted/IMG_001.jpg", b"already there")
        for source in (first, second):
            result = apply_decision(self.store, str(source), self.decision(source), "move")
            self.assertTrue(result["success"], result)
            self.assertFalse(source.exists())
        self.assertEqual(existing.read_bytes(), b"already there")
        self.assertEqual((self.root / "sorted/IMG_001 (1).jpg").read_bytes(), b"test photo original bytes")
        self.assertEqual((self.root / "sorted/IMG_001 (2).jpg").read_bytes(), b"second camera")

    def test_copy_preserves_original_and_metadata(self):
        source = self.photo()
        before = fingerprint(source)
        result = apply_decision(self.store, str(source), self.decision(source), "copy")
        self.assertTrue(result["success"], result)
        self.assertEqual(fingerprint(source), before)
        self.assertEqual(Path(result["target"]).read_bytes(), source.read_bytes())
        self.assertEqual(Path(result["target"]).stat().st_mtime_ns, source.stat().st_mtime_ns)

    def test_changed_source_is_never_moved(self):
        source = self.photo()
        decision = self.decision(source)
        source.write_bytes(b"changed externally")
        result = apply_decision(self.store, str(source), decision, "move")
        self.assertFalse(result["success"])
        self.assertEqual(source.read_bytes(), b"changed externally")
        self.assertFalse((self.root / "sorted").exists())

    def test_failed_transfer_keeps_source_and_cleans_partial_output(self):
        source = self.photo()
        decision = self.decision(source)
        with patch("core.shutil.copystat", side_effect=OSError("simulated failure")):
            result = apply_decision(self.store, str(source), decision, "move")
        self.assertFalse(result["success"])
        self.assertTrue(source.exists())
        self.assertEqual(list((self.root / "sorted").iterdir()), [])

    def test_unavailable_trash_does_not_fall_back_to_permanent_deletion(self):
        source = self.photo()
        with patch("core.safe_trash", side_effect=OSError("trash unavailable")):
            result = apply_decision(self.store, str(source), self.decision(source, "trash"), "move")
        self.assertFalse(result["success"])
        self.assertTrue(source.exists())
        self.assertEqual(self.store.history()[0][2], "error")

    def test_real_os_trash_on_disposable_fixture(self):
        source = self.photo("disposable-trash-fixture.jpg")
        result = apply_decision(self.store, str(source), self.decision(source, "trash"), "move")
        self.assertTrue(result["success"], result)
        self.assertFalse(source.exists())
        if sys.platform == "win32":
            self.assertIn("$recycle.bin", result["target"].lower())
            self.assertTrue(Path(result["target"]).exists(), "Fixture must be recoverable in Recycle Bin")

    def test_interrupted_operation_is_not_silently_repeated(self):
        source = self.photo()
        operation_id = self.store.begin_operation(str(source), "somewhere.jpg", "move")
        self.store.finish_operation(operation_id, "copied", "somewhere.jpg")
        result = apply_decision(self.store, str(source), self.decision(source), "move")
        self.assertFalse(result["success"])
        self.assertIn("中断", result["error"])
        self.assertTrue(source.exists())

    def test_scanner_excludes_destination_and_preserves_duplicate_names(self):
        a = self.photo()
        b = self.photo("camera-b/IMG_001.jpg")
        self.photo("sorted/IMG_001.jpg")
        self.photo(".hidden/hidden.jpg")
        self.photo("camera-a/not-an-image.txt")
        found = set(scan_images(self.root, [self.root / "sorted"], self.root / "state"))
        self.assertEqual(found, {str(a), str(b)})

    def test_session_roundtrip_preserves_queued_decisions_without_mutation(self):
        source = self.photo()
        state = self.store.load(str(self.root))
        state["decisions"][str(source)] = self.decision(source, "trash")
        self.store.save(str(self.root), state)
        self.assertEqual(self.store.load(str(self.root)), state)
        self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
