import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from core import fingerprint
from photo_tools import CropDialog, HelpDialog, PropertyDialog, photo_properties, save_crop


class PhotoToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("KOMA_TEST_DIR"))
        self.root = Path(self.temp.name)
        self.photo = self.root / "sample.jpg"
        image = Image.new("RGB", (120, 80), "red")
        exif = Image.Exif(); exif[274] = 6; exif[36867] = "2026:10:30 12:34:56"; exif[271] = "Camera"
        image.save(self.photo, exif=exif, quality=98)
    def tearDown(self): self.temp.cleanup()

    def test_properties_include_oriented_size_and_available_exif(self):
        props = dict(photo_properties(self.photo))
        self.assertEqual(props["画像サイズ"], "80 × 120 px")
        self.assertEqual(props["撮影日時"], "2026:10:30 12:34:56")
        self.assertEqual(props["カメラ"], "Camera")
        dialog = PropertyDialog(str(self.photo)); dialog.show(); self.app.processEvents(); dialog.close()

    def test_crop_respects_orientation_keeps_original_and_avoids_name_collision(self):
        before = self.photo.read_bytes(); expected = fingerprint(self.photo)
        first = Path(save_crop(self.photo, (10, 20, 60, 90), expected))
        second = Path(save_crop(self.photo, (10, 20, 60, 90), expected))
        self.assertNotEqual(first, second)
        self.assertEqual(self.photo.read_bytes(), before)
        for target in (first, second):
            with Image.open(target) as image:
                self.assertEqual(image.size, (50, 70))
                self.assertNotIn(image.getexif().get(274), (3, 6, 8))
                self.assertEqual(image.getexif().get(36867), "2026:10:30 12:34:56")

    def test_crop_rejects_stale_or_invalid_image_without_writing(self):
        expected = fingerprint(self.photo)
        self.photo.write_bytes(self.photo.read_bytes() + b"changed")
        with self.assertRaises(OSError): save_crop(self.photo, (0, 0, 10, 10), expected)
        with self.assertRaises(ValueError): save_crop(self.photo, (-1, 0, 100, 100), fingerprint(self.photo))
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_incomplete_output_removed_on_failure(self):
        with patch("PIL.Image.Image.save", side_effect=OSError("write failure")):
            with self.assertRaises(OSError): save_crop(self.photo, (0, 0, 20, 20), fingerprint(self.photo))
        self.assertEqual(list(self.root.iterdir()), [self.photo])

    def test_crop_canvas_maps_drag_and_ratio_to_pixels(self):
        dialog = CropDialog(str(self.photo)); dialog.show(); self.app.processEvents()
        canvas = dialog.canvas; bounds = canvas.image_rect()
        start = bounds.topLeft() + (bounds.bottomRight() - bounds.topLeft()) * 0.1
        end = bounds.topLeft() + (bounds.bottomRight() - bounds.topLeft()) * 0.8
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start.toPoint())
        QTest.mouseMove(canvas, end.toPoint())
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end.toPoint())
        self.assertTrue(dialog.save_button.isEnabled())
        box=canvas.selected_pixels(); self.assertGreater(box[2]-box[0], 40); self.assertGreater(box[3]-box[1], 60)
        dialog.ratio.setCurrentIndex(1)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start.toPoint())
        QTest.mouseMove(canvas, end.toPoint())
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end.toPoint())
        box=canvas.selected_pixels(); self.assertLessEqual(abs((box[2]-box[0])-(box[3]-box[1])), 2)
        dialog.close()

    def test_guide_is_categorized(self):
        dialog=HelpDialog(); dialog.show(); self.app.processEvents()
        from PySide6.QtWidgets import QTabWidget
        tabs=dialog.findChild(QTabWidget); self.assertEqual(tabs.count(), 4)
        dialog.close()
