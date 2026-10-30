import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
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

    def test_crop_right_drag_moves_selection_without_changing_size_and_clamps(self):
        dialog = CropDialog(str(self.photo)); dialog.show(); self.app.processEvents()
        canvas = dialog.canvas; canvas.selection = QRectF(10, 20, 30, 40)
        rect=canvas.image_rect()
        def point(x,y):
            return QPointF(rect.left()+x*rect.width()/canvas.image.width(),
                           rect.top()+y*rect.height()/canvas.image.height()).toPoint()
        before=QRectF(canvas.selection)
        QTest.mousePress(canvas, Qt.MouseButton.RightButton, pos=point(20,30))
        QTest.mouseMove(canvas, point(35,50))
        QTest.mouseRelease(canvas, Qt.MouseButton.RightButton, pos=point(35,50))
        self.assertAlmostEqual(canvas.selection.width(), before.width(), places=4)
        self.assertAlmostEqual(canvas.selection.height(), before.height(), places=4)
        self.assertGreater(canvas.selection.left(), before.left())
        self.assertGreater(canvas.selection.top(), before.top())
        QTest.mousePress(canvas, Qt.MouseButton.RightButton, pos=point(35,50))
        QTest.mouseMove(canvas, point(79,119))
        QTest.mouseRelease(canvas, Qt.MouseButton.RightButton, pos=point(79,119))
        self.assertLessEqual(canvas.selection.right(), canvas.image.width())
        self.assertLessEqual(canvas.selection.bottom(), canvas.image.height())
        dialog.close()

    def test_crop_wheel_zoom_and_pan_preserve_selection(self):
        dialog=CropDialog(str(self.photo)); dialog.show(); self.app.processEvents()
        canvas=dialog.canvas;canvas.selection=QRectF(10,20,30,40)
        initial=QRectF(canvas.selection); pos=canvas.rect().center()
        wheel=QWheelEvent(QPointF(pos), QPointF(canvas.mapToGlobal(pos)), QPoint(0,0), QPoint(0,120),
                          Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(canvas,wheel)
        self.assertGreater(canvas.zoom_scale, 1); self.assertEqual(canvas.selection, initial)
        canvas.zoom(4)
        old=QPointF(canvas.offset)
        QTest.mousePress(canvas,Qt.MouseButton.MiddleButton,pos=pos)
        QTest.mouseMove(canvas,pos+QPoint(40,30))
        QTest.mouseRelease(canvas,Qt.MouseButton.MiddleButton,pos=pos+QPoint(40,30))
        self.assertNotEqual(canvas.offset,old); self.assertEqual(canvas.selection,initial)
        canvas.fit_image(); self.assertEqual(canvas.zoom_scale,1.0); self.assertEqual(canvas.offset,QPointF())
        dialog.close()

    def test_crop_grid_toggle_changes_overlay_and_is_remembered(self):
        parent=QWidget();parent.data_dir=self.root
        dialog=CropDialog(str(self.photo),parent); dialog.show();self.app.processEvents()
        dialog.canvas.selection=QRectF(5,5,60,80);dialog.canvas.update();self.app.processEvents()
        image_on=dialog.canvas.grab().toImage()
        dialog.grid_button.click();self.app.processEvents()
        image_off=dialog.canvas.grab().toImage()
        self.assertFalse(dialog.canvas.grid_enabled);self.assertNotEqual(image_on,image_off)
        dialog.close()
        second=CropDialog(str(self.photo),parent)
        self.assertFalse(second.grid_button.isChecked());self.assertFalse(second.canvas.grid_enabled)
        second.close();parent.close()

    def test_guide_is_categorized(self):
        dialog=HelpDialog(); dialog.show(); self.app.processEvents()
        from PySide6.QtWidgets import QTabWidget
        tabs=dialog.findChild(QTabWidget); self.assertEqual(tabs.count(), 4)
        dialog.close()
