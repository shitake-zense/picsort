import os
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QDrag, QDragEnterEvent, QDropEvent, QFontDatabase, QImage, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from app import ASSETS, STYLE, ApplyPreviewDialog, KeyCaptureDialog, Window, migrate_previous_work, register_open_with, default_data_directory
from core import Store


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        if not QFontDatabase.families() and Path("C:/Windows/Fonts/YuGothR.ttc").exists():
            QFontDatabase.addApplicationFont("C:/Windows/Fonts/YuGothR.ttc")
        cls.application.setStyle("Fusion")
        cls.application.setStyleSheet(STYLE)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="koma-ui-test-", dir=os.environ.get("KOMA_TEST_DIR"))
        self.root = Path(self.temp.name)
        self.photos = self.root / "event"
        shutil.copytree(Path(__file__).parent / "fixtures", self.photos)
        self.window = Window(self.root / "data")
        self.window.show()
        self.window.activateWindow()
        self.window.open_folder(str(self.photos))
        self.wait_for(lambda: not self.window.scanning)
        self.wait_for(lambda: not self.window.canvas.image.isNull())
        self.paths = list(self.window.entries)
        # Explicit test destinations; new folders no longer get implicit categories.
        for name in ("集合写真", "ステージ", "スナップ"):
            self.window.register_destination(str(self.photos / "整理後" / name))

    def tearDown(self):
        self.window.close()
        self.application.processEvents()
        self.temp.cleanup()

    def wait_for(self, condition, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if condition():
                return
            time.sleep(.01)
        self.fail("Timed out waiting for UI operation")

    def test_fullscreen_status_follows_each_photo_and_clear(self):
        destination = self.window.state["destinations"][0]["path"]
        self.assertTrue(self.window.make_decisions([self.paths[0]], "file", destination))
        self.assertTrue(self.window.make_decisions([self.paths[1]], "trash"))
        self.window.show_compare()
        self.assertEqual(self.window.canvas.overlay_status, "")
        self.window.toggle_photo_fullscreen()
        self.assertEqual(self.window.canvas.overlay_status, "振り分け予定：集合写真")
        self.assertEqual(self.window.compare_canvas.overlay_status, "削除予定")
        # Clear the left photo without leaving fullscreen; only its label disappears.
        self.window.clear_clicked_path(self.paths[0])
        self.assertEqual(self.window.canvas.overlay_status, "")
        self.assertEqual(self.window.compare_canvas.overlay_status, "削除予定")
        self.window.exit_photo_fullscreen()
        self.assertEqual(self.window.compare_canvas.overlay_status, "")
        self.window.toggle_photo_fullscreen()
        self.assertEqual(self.window.compare_canvas.overlay_status, "削除予定")
        self.window.show_single()
        self.window.position = 1
        self.window.refresh_current()
        self.assertEqual(self.window.canvas.path, self.paths[1])
        self.assertEqual(self.window.canvas.overlay_status, "削除予定")
        self.window.position = 2
        self.window.refresh_current()
        self.assertEqual(self.window.canvas.overlay_status, "")

    def test_app_fullscreen_also_shows_status_and_restores_it_from_photo_mode(self):
        self.assertTrue(self.window.make_decisions([self.paths[0]], "trash"))
        self.window.refresh_current()
        self.window.toggle_app_fullscreen()
        self.assertEqual(self.window.canvas.overlay_status, "削除予定")
        self.window.toggle_photo_fullscreen()
        self.window.exit_photo_fullscreen()
        self.assertTrue(self.window.app_fullscreen)
        self.assertEqual(self.window.canvas.overlay_status, "削除予定")
        self.window.toggle_app_fullscreen()
        self.assertEqual(self.window.canvas.overlay_status, "")

    def test_fullscreen_overlay_paints_without_intercepting_clicks(self):
        self.assertTrue(self.window.make_decisions([self.paths[0]], "trash"))
        self.window.refresh_current()
        self.window.toggle_photo_fullscreen()
        self.application.processEvents()
        canvas = self.window.canvas
        self.assertEqual(canvas.overlay_status, "削除予定")
        # Label is painted on the canvas, so clicking over it still navigates.
        QTest.mouseClick(canvas, Qt.MouseButton.RightButton, pos=QPoint(24, 24))
        self.assertEqual(canvas.path, self.paths[1])
        self.assertEqual(canvas.overlay_status, "")

    def test_zoom_control_explains_its_action(self):
        self.assertEqual(self.window.zoom_button.text(), "原寸で確認")
        self.assertIn("ピント", self.window.zoom_button.toolTip())
        self.window.toggle_zoom()
        self.assertEqual(self.window.zoom_button.text(), "画面に合わせる")
        self.window.toggle_zoom()
        self.assertEqual(self.window.zoom_button.text(), "原寸で確認")

    def test_mac_work_data_is_saved_in_user_library(self):
        home = self.root / "user"
        self.assertEqual(default_data_directory("darwin", home),
                         home / "Library" / "Application Support" / "PixSort")

    def test_compact_window_keeps_footer_and_apply_visible(self):
        self.window.resize(740, 450)
        self.application.processEvents()
        bottom = self.window.apply_button.mapTo(self.window, self.window.apply_button.rect().bottomRight())
        self.assertLess(bottom.y(), self.window.height())
        self.assertGreaterEqual(self.window.width(), 740)
        self.assertEqual(self.window.height(), 450)

    def test_compact_comparison_canvas_fits_inside_photo_area(self):
        self.window.show_compare()
        self.window.resize(740, 450)
        self.application.processEvents()
        for canvas in (self.window.canvas, self.window.compare_canvas):
            point = canvas.mapTo(self.window.photo_page, canvas.rect().bottomRight())
            self.assertLess(point.y(), self.window.photo_page.height())
            self.assertLess(point.x(), self.window.photo_page.width())

    def test_compact_confirmation_keeps_execute_button_visible(self):
        self.window.make_decisions([self.paths[0]], "trash")
        dialog = ApplyPreviewDialog(self.window, self.window.pending())
        dialog.resize(650, 400); dialog.show(); self.application.processEvents()
        bottom = dialog.go_button.mapTo(dialog, dialog.go_button.rect().bottomRight())
        self.assertLess(bottom.y(), dialog.height())
        self.assertLessEqual(dialog.height(), 400)
        dialog.close()

    def test_similarity_review_marks_and_clears_only_checked_candidates(self):
        from similar_ui import SimilarDialog
        duplicate = self.photos / "DEMO_001_copy.jpg"
        shutil.copy2(self.photos / "DEMO_001.jpg", duplicate)
        self.window.refresh_folder()
        self.wait_for(lambda: not self.window.scanning)
        original = str((self.photos / "DEMO_001.jpg").resolve())
        duplicate = str(duplicate.resolve())
        original_bytes = Path(original).read_bytes(); duplicate_bytes = Path(duplicate).read_bytes()
        dialog = SimilarDialog(self.window, list(self.window.visible))
        dialog.show(); dialog.mode.setCurrentIndex(1); dialog.start_search()
        self.wait_for(lambda: not dialog.running)
        index = next(i for i,g in enumerate(dialog.groups) if original in g and duplicate in g)
        dialog.group_list.setCurrentRow(index)
        self.wait_for(lambda: dialog.items.count() >= 2)
        for i in range(dialog.items.count()):
            item = dialog.items.item(i)
            if item.data(Qt.ItemDataRole.UserRole) in (original, duplicate):
                item.setCheckState(Qt.CheckState.Checked)
        self.assertEqual(dialog.checked, {original, duplicate})
        self.assertTrue(dialog.compare_button.isEnabled())
        dialog.mark_trash()
        self.assertEqual(set(self.window.pending()), {original, duplicate})
        self.assertEqual(Path(original).read_bytes(), original_bytes)
        self.assertEqual(Path(duplicate).read_bytes(), duplicate_bytes)
        for i in range(dialog.items.count()):
            item = dialog.items.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == duplicate: item.setCheckState(Qt.CheckState.Checked)
        dialog.clear_candidates()
        self.assertEqual(set(self.window.pending()), {original})
        self.assertEqual(Path(duplicate).read_bytes(), duplicate_bytes)
        dialog.reject()

    def test_similarity_card_click_toggles_check_and_keeps_preview(self):
        from similar_ui import SimilarDialog
        duplicate = self.photos / "DEMO_001_copy.jpg"
        shutil.copy2(self.photos / "DEMO_001.jpg", duplicate)
        self.window.refresh_folder(); self.wait_for(lambda: not self.window.scanning)
        dialog = SimilarDialog(self.window, list(self.window.visible))
        dialog.show(); dialog.mode.setCurrentIndex(1); dialog.start_search()
        self.wait_for(lambda: not dialog.running)
        self.assertGreater(dialog.items.count(), 0)
        item = dialog.items.item(0); rect = dialog.items.visualItemRect(item)
        path = item.data(Qt.ItemDataRole.UserRole)
        QTest.mouseClick(dialog.items.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        self.assertIn(path, dialog.checked)
        self.assertEqual(dialog.items.currentItem(), item)
        QTest.mouseClick(dialog.items.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        self.assertNotIn(path, dialog.checked)
        dialog.reject()

    def test_similarity_card_shift_click_checks_range(self):
        from similar_ui import SimilarDialog
        for i in range(3): shutil.copy2(self.photos / "DEMO_001.jpg", self.photos / f"DEMO_001_copy{i}.jpg")
        self.window.refresh_folder(); self.wait_for(lambda: not self.window.scanning)
        dialog = SimilarDialog(self.window, list(self.window.visible))
        dialog.show(); dialog.mode.setCurrentIndex(1); dialog.start_search()
        self.wait_for(lambda: not dialog.running)
        self.assertGreaterEqual(dialog.items.count(), 4)
        first = dialog.items.visualItemRect(dialog.items.item(0)).center()
        last = dialog.items.visualItemRect(dialog.items.item(2)).center()
        QTest.mouseClick(dialog.items.viewport(), Qt.MouseButton.LeftButton, pos=first)
        QTest.mouseClick(dialog.items.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, pos=last)
        self.assertEqual(len(dialog.checked), 3)
        dialog.reject()

    def test_similar_trash_does_not_replace_existing_destination_when_declined(self):
        from similar_ui import SimilarDialog
        from PySide6.QtWidgets import QMessageBox
        duplicate = self.photos / "DEMO_001_copy.jpg"
        shutil.copy2(self.photos / "DEMO_001.jpg", duplicate)
        self.window.refresh_folder(); self.wait_for(lambda: not self.window.scanning)
        target = str((self.photos / "DEMO_001.jpg").resolve())
        destination = self.window.state["destinations"][0]["path"]
        self.window.make_decisions([target], "file", destination)
        dialog = SimilarDialog(self.window, list(self.window.visible))
        dialog.show(); dialog.mode.setCurrentIndex(1); dialog.start_search()
        self.wait_for(lambda: not dialog.running)
        index = next(i for i,g in enumerate(dialog.groups) if target in g)
        dialog.group_list.setCurrentRow(index)
        for i in range(dialog.items.count()):
            item = dialog.items.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == target: item.setCheckState(Qt.CheckState.Checked)
        with patch("similar_ui.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
            dialog.mark_trash()
        self.assertEqual(self.window.state["decisions"][target]["action"], "file")
        self.assertIn(target, dialog.checked)
        dialog.reject()

    def _similar_dialog_with_copies(self):
        from similar_ui import SimilarDialog
        for i in range(3): shutil.copy2(self.photos / "DEMO_001.jpg", self.photos / f"DEMO_001_copy{i}.jpg")
        self.window.refresh_folder(); self.wait_for(lambda: not self.window.scanning)
        dialog = SimilarDialog(self.window, list(self.window.visible))
        dialog.show(); dialog.mode.setCurrentIndex(1); dialog.start_search()
        self.wait_for(lambda: not dialog.running)
        self.wait_for(lambda: dialog.items.count() >= 4 and not dialog.preview.image.isNull())
        return dialog

    def test_similarity_cards_keep_size_before_and_after_thumbnail_loading(self):
        dialog = self._similar_dialog_with_copies()
        self.application.processEvents()
        for i in range(dialog.items.count()):
            rect = dialog.items.visualItemRect(dialog.items.item(i))
            self.assertGreaterEqual(rect.height(), 130)
            self.assertGreaterEqual(rect.width(), 140)
        dialog.reject()

    def test_similarity_preview_zoom_fullscreen_and_restore(self):
        dialog = self._similar_dialog_with_copies()
        initial = dialog.preview.path
        dialog.zoom_active(1.25)
        self.assertGreater(dialog.preview.zoom_scale, 1.0)
        self.wait_for(lambda: not dialog.preview.image.isNull() and (initial, "full") in dialog.image_cache)
        dialog.toggle_dialog_fullscreen()
        self.assertTrue(dialog.isFullScreen()); self.assertTrue(dialog.dialog_fullscreen)
        dialog.toggle_photo_fullscreen()
        self.assertTrue(dialog.photo_only); self.assertFalse(dialog.center_panel.isVisible())
        self.assertTrue(dialog.preview.isVisible()); self.assertTrue(dialog.exit_photo_button.isVisible())
        dialog.reject()
        self.assertFalse(dialog.photo_only); self.assertTrue(dialog.isFullScreen())
        self.assertTrue(dialog.center_panel.isVisible()); self.assertEqual(dialog.preview.path, initial)
        dialog.reject(); self.assertFalse(dialog.dialog_fullscreen)
        dialog.reject()

    def test_similarity_compare_stays_in_results_and_keeps_checks(self):
        dialog = self._similar_dialog_with_copies()
        selected=[]
        for i in (0, 1):
            item = dialog.items.item(i); selected.append(item.data(Qt.ItemDataRole.UserRole))
            item.setCheckState(Qt.CheckState.Checked)
        dialog.compare_photos()
        self.assertTrue(dialog.compare_mode); self.assertTrue(dialog.isVisible())
        self.assertEqual([dialog.preview.path, dialog.compare_canvas.path], selected)
        self.wait_for(lambda: not dialog.compare_canvas.image.isNull())
        dialog.select_compare_side(1)
        self.assertIs(dialog.active_canvas, dialog.compare_canvas)
        before = dialog.preview.path; dialog.navigate_independent(1)
        self.assertEqual(dialog.preview.path, before)
        dialog.exit_compare()
        self.assertFalse(dialog.compare_mode); self.assertEqual(dialog.checked, set(selected))
        self.assertGreater(len(dialog.groups), 0); dialog.reject()

    def test_similarity_compare_navigation_stays_inside_current_group(self):
        dialog = self._similar_dialog_with_copies()
        group = dialog.groups[dialog.group_list.currentRow()]
        for i in (len(group)-2, len(group)-1): dialog.items.item(i).setCheckState(Qt.CheckState.Checked)
        dialog.compare_photos()
        before = [dialog.preview.path, dialog.compare_canvas.path]
        dialog.navigate_preview(1, independent=False)
        self.assertEqual([dialog.preview.path, dialog.compare_canvas.path], before)
        dialog.select_compare_side(1); dialog.navigate_independent(1)
        self.assertEqual(dialog.compare_canvas.path, group[-1])
        dialog.reject()

    def test_similarity_preview_controls_fit_and_mouse_wheel_zooms(self):
        dialog = self._similar_dialog_with_copies()
        dialog.resize(740, 450); self.application.processEvents()
        for control in (dialog.prev_button, dialog.next_button, dialog.zoom_out_button,
                        dialog.zoom_in_button, dialog.actual_button, dialog.properties_button):
            self.assertTrue(control.isVisible())
            self.assertGreaterEqual(control.width(), 28)
            point = control.mapTo(dialog, control.rect().bottomRight())
            self.assertLess(point.x(), dialog.width()); self.assertLess(point.y(), dialog.height())
        pos = dialog.preview.rect().center()
        wheel = QWheelEvent(QPointF(pos), QPointF(dialog.preview.mapToGlobal(pos)), QPoint(0, 0), QPoint(0, 120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(dialog.preview, wheel)
        self.assertGreater(dialog.preview.zoom_scale, 1.0)
        dialog.reject()

    def test_similarity_destination_key_acts_on_preview_and_advances(self):
        dialog = self._similar_dialog_with_copies()
        original = dialog.preview.path
        dialog.activateWindow(); dialog.preview.setFocus(); self.application.processEvents()
        QTest.keyClick(dialog.preview, Qt.Key.Key_1)
        self.application.processEvents()
        self.assertEqual(self.window.state["decisions"][original]["action"], "file")
        self.assertNotEqual(dialog.preview.path, original)
        self.assertTrue(Path(original).exists())
        dialog.reject()

    def test_similarity_preview_and_batch_actions_remain_pending(self):
        dialog = self._similar_dialog_with_copies()
        original = dialog.preview.path; data = Path(original).read_bytes()
        dialog.mark_one(original, "trash", advance=True)
        self.assertEqual(self.window.state["decisions"][original]["action"], "trash")
        self.assertNotEqual(dialog.preview.path, original)
        dialog.clear_one(original); self.assertNotIn(original, self.window.pending())
        dialog.undo(); self.assertEqual(self.window.state["decisions"][original]["action"], "trash")
        for i in (1, 2): dialog.items.item(i).setCheckState(Qt.CheckState.Checked)
        selected = set(dialog.checked); dialog.assign_checked(0)
        destination = self.window.state["destinations"][0]["path"]
        self.assertTrue(all(self.window.state["decisions"][p]["destination"] == destination for p in selected))
        self.assertEqual(Path(original).read_bytes(), data)
        dialog.reject()

    def test_main_toolbar_is_one_row_at_small_and_large_widths(self):
        for width in (740, 1300):
            self.window.resize(width, 650); self.application.processEvents()
            controls = [self.window.filter_combo, self.window.single_button,
                        self.window.compare_button, self.window.grid_button, self.window.mode_combo]
            if width < 1200: controls.append(self.window.tools_button)
            else: controls.extend((self.window.property_button, self.window.crop_button, self.window.similar_button))
            coords = [widget.mapTo(self.window, QPoint(0, 0)).y() for widget in controls]
            self.assertLess(max(coords) - min(coords), 5)
            for widget in controls:
                self.assertTrue(widget.isVisible())
                self.assertLess(widget.mapTo(self.window, widget.rect().bottomRight()).x(), self.window.width())

    def test_similar_two_photo_comparison_restores_filtered_photos(self):
        self.window.make_decisions([self.paths[0]], "trash")
        self.window.filter_combo.setCurrentIndex(1)
        class FakeDialog:
            chosen_paths = [self.paths[0], self.paths[2]]
            def __init__(self, *_): pass
            def exec(self):
                from PySide6.QtWidgets import QDialog
                return QDialog.DialogCode.Accepted
        with patch("app.SimilarDialog", FakeDialog): self.window.show_similar()
        self.assertEqual(self.window.filter_combo.currentIndex(), 0)
        self.assertTrue(self.window.compare_mode)
        self.assertEqual(self.window.independent_paths, [self.paths[0], self.paths[2]])
        self.assertEqual(self.window.canvas.path, self.paths[0])
        self.assertEqual(self.window.compare_canvas.path, self.paths[2])

    def test_folder_navigation_and_destinations_are_scoped(self):
        nested = self.photos / "既存フォルダ"
        nested.mkdir()
        shutil.copy2(self.photos / "DEMO_001.jpg", nested / "child.jpg")
        self.window.refresh_folder_list()
        self.assertEqual(self.window.folder_list.count(), 1)
        self.window.folder_list.setCurrentRow(0)
        self.window.use_selected_folder()
        self.assertEqual(self.window.state["destinations"][-1]["path"], str(nested.resolve()))
        self.window.enter_selected_folder()
        self.wait_for(lambda: not self.window.scanning)
        self.assertEqual(self.window.root, str(nested.resolve()))
        self.assertEqual(self.window.state["destinations"], [])
        self.assertEqual([Path(p).name for p in self.window.entries], ["child.jpg"])
        with patch("app.QInputDialog.getText", return_value=("新規", True)):
            self.window.create_folder()
        self.assertTrue((nested / "新規").is_dir())
        self.assertEqual(self.window.folder_list.count(), 1)
        self.window.go_parent()
        self.wait_for(lambda: not self.window.scanning)
        self.assertEqual(len(self.window.entries), 8)
        self.assertEqual(len(self.window.state["destinations"]), 4)
        self.assertTrue((nested / "child.jpg").exists())

    def test_empty_parent_shows_subfolder_and_legacy_candidates(self):
        parent = self.root / "parent"
        child = parent / "child"
        child.mkdir(parents=True)
        photo = child / "child.jpg"
        shutil.copy2(self.photos / "DEMO_001.jpg", photo)
        self.window.open_folder(str(parent))
        self.wait_for(lambda: not self.window.scanning)
        self.assertEqual(self.window.entries, [])
        self.assertEqual(self.window.folder_list.count(), 1)
        self.assertEqual(self.window.state["destinations"], [])
        from core import fingerprint
        self.window.state["decisions"][str(photo)] = {"action": "trash", "fingerprint": fingerprint(photo)}
        self.window.save()
        self.window.open_folder(str(parent))
        self.wait_for(lambda: not self.window.scanning)
        self.assertIn(str(photo), self.window.pending())
        self.assertIn("旧版で指定", self.window.status_label.text())

    def test_invalid_folder_name_does_not_escape_current_directory(self):
        with patch("app.QInputDialog.getText", return_value=("../escape", True)), \
             patch.object(self.window, "show_error") as error:
            self.window.create_folder()
        error.assert_called_once()
        self.assertFalse((self.root / "escape").exists())

    def test_reopening_folder_in_grid_drops_old_film_items(self):
        self.assertTrue(self.window.film_items)
        self.window.set_view(True)
        self.window.open_folder(str(self.photos))
        self.wait_for(lambda: not self.window.scanning)
        self.assertEqual(self.window.film_items, {})
        # A new thumbnail for a previously displayed path must not access a deleted Qt item.
        self.window.image_ready({"key": (self.paths[0], "thumb"), "image": QImage(10, 10, QImage.Format.Format_RGB32)})
        self.assertTrue(self.window.make_decisions([self.paths[0]], "file", self.window.state["destinations"][0]["path"]))
        self.window.show_single()
        self.assertIsNotNone(self.window.film_item(self.paths[0]))

    def test_destination_and_trash_status_are_visible_and_survive_restart(self):
        self.window.assign(0)
        self.assertEqual(self.window.state["decisions"][self.paths[0]]["action"], "file")
        self.assertEqual(self.window.grid.item(0).data(Qt.ItemDataRole.UserRole + 2), "振り分け予定：集合写真")
        self.assertEqual(self.window.grid.item(0).data(Qt.ItemDataRole.UserRole + 3), "file")
        self.window.position = 0
        self.window.refresh_current()
        self.assertIn("集合写真（予定）", self.window.file_label.text())
        self.assertIn("[集]", self.window.film.item(3).text())
        self.assertIn(self.window.state["destinations"][0]["path"], self.window.grid.item(0).toolTip())
        self.window.mark_path(self.paths[1], "trash")
        self.assertEqual(self.window.grid.item(1).data(Qt.ItemDataRole.UserRole + 2), "削除候補")
        self.window.open_folder(str(self.photos))
        self.wait_for(lambda: not self.window.scanning)
        self.assertEqual(self.window.grid.item(0).data(Qt.ItemDataRole.UserRole + 2), "振り分け予定：集合写真")
        self.window.state["destinations"][0]["label"] = "新しい表示名"
        self.window.rebuild_views()
        self.assertEqual(self.window.grid.item(0).data(Qt.ItemDataRole.UserRole + 2), "振り分け予定：新しい表示名")
        self.window.state["destinations"].pop(0)
        self.window.rebuild_views()
        self.assertEqual(self.window.grid.item(0).data(Qt.ItemDataRole.UserRole + 2), "振り分け予定：集合写真")

    def test_shift_click_checks_forward_and_backward_ranges_without_losing_other_checks(self):
        self.window.set_view(True)
        self.application.processEvents()
        def click(index, shift=False):
            rect = self.window.grid.visualItemRect(self.window.grid.item(index))
            QTest.mouseClick(self.window.grid.viewport(), Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.ShiftModifier if shift else Qt.KeyboardModifier.NoModifier,
                             rect.center())
        click(0)
        click(2)
        click(5, True)
        self.assertEqual(self.window.checked, {self.paths[i] for i in [0, 2, 3, 4, 5]})
        self.window.uncheck_all()
        click(5)
        click(2, True)
        self.assertEqual(self.window.checked, set(self.paths[2:6]))
        click(3)
        self.assertNotIn(self.paths[3], self.window.checked)
        self.window.open_folder(str(self.photos))
        self.wait_for(lambda: not self.window.scanning)
        self.assertEqual(self.window.grid.anchor_path, "")

    def test_grid_sizes_resize_cards_and_are_remembered(self):
        self.window.set_view(True)
        self.window.grid_size_combo.setCurrentIndex(3)
        self.application.processEvents()
        self.assertEqual(self.window.grid.gridSize().width(), 320)
        self.assertEqual(self.window.grid.iconSize().width(), 300)
        self.assertEqual(self.window.grid.item(0).sizeHint().width(), 318)
        self.assertEqual(json.loads((self.root / "data/settings.json").read_text(encoding="utf-8"))["grid_size"], 3)
        self.window.open_folder(str(self.photos))
        self.wait_for(lambda: not self.window.scanning)
        self.assertEqual(self.window.grid.item(0).sizeHint().width(), 318)
        self.window.close()
        self.window = Window(self.root / "data")
        self.window.show()
        self.assertEqual(self.window.grid_size_combo.currentIndex(), 3)
        self.assertEqual(self.window.grid.gridSize().width(), 320)

    def test_app_fullscreen_preserves_photo_only_fullscreen_and_escape(self):
        self.window.set_view(True)
        QTest.keyClick(self.window, Qt.Key.Key_F11)
        self.assertTrue(self.window.app_fullscreen)
        self.assertTrue(self.window.isFullScreen())
        self.assertTrue(self.window.header_frame.isVisible())
        self.assertTrue(self.window.grid.isVisible())
        self.window.show_single()
        self.window.toggle_photo_fullscreen()
        self.assertTrue(self.window.photo_fullscreen)
        self.assertFalse(self.window.header_frame.isVisible())
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.assertTrue(self.window.app_fullscreen)
        self.assertTrue(self.window.isFullScreen())
        self.assertTrue(self.window.header_frame.isVisible())
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.assertFalse(self.window.app_fullscreen)
        self.assertFalse(self.window.isFullScreen())
        self.assertIn("全画面", self.window.fullscreen_button.text())
        self.assertIn("全画面", self.window.shortcut_conflict("F11", 0))

    def test_existing_f11_destination_is_reassigned_with_notice(self):
        self.window.state["destinations"][0]["key"] = "F11"
        self.window.save()
        self.window.open_folder(str(self.photos))
        self.wait_for(lambda: not self.window.scanning)
        self.assertNotEqual(self.window.state["destinations"][0]["key"], "F11")
        self.assertIn("新しい操作キーとの重複", self.window.status_label.text())
        self.assertIn("集合写真", self.window.status_label.text())

    def test_parent_destination_move_and_created_folder_auto_register(self):
        with patch("app.QInputDialog.getText", return_value=("分類", True)):
            self.window.create_folder()
        self.assertTrue((self.photos / "分類").is_dir())
        self.assertTrue(any(d["path"] == str((self.photos / "分類").resolve()) for d in self.window.state["destinations"]))
        self.window.register_destination(str(self.root))
        index = next(i for i, d in enumerate(self.window.state["destinations"]) if d["path"] == str(self.root.resolve()))
        self.window.assign(index)
        photo = Path(self.paths[0])
        self.window.start_apply(self.window.pending())
        self.wait_for(lambda: not self.window.applying)
        self.assertFalse(photo.exists())
        self.assertTrue((self.root / photo.name).exists())

    def test_preview_dialog_shows_all_actions_and_deleted_photo_first(self):
        self.window.assign(0)
        self.window.mark_current("trash")
        dialog = ApplyPreviewDialog(self.window, self.window.pending())
        dialog.show()
        self.application.processEvents()
        self.assertEqual(dialog.tabs.count(), 4)
        self.assertEqual(dialog.items.count(), 1)
        self.assertEqual(dialog.preview_path, self.paths[1])
        self.assertFalse(dialog.preview_image.isNull())
        self.assertIn("ごみ箱", dialog.detail.text())
        dialog.tabs.setCurrentIndex(1)
        self.assertEqual(dialog.preview_path, self.paths[0])
        self.assertIn(self.window.state["destinations"][0]["path"], dialog.detail.text())
        dialog.reject()
        self.assertTrue(all(Path(p).exists() for p in self.paths))

    def test_compare_independent_crossing_and_keyboard_side_selection(self):
        self.window.show_compare()
        self.window.select_side(1)
        self.window.navigate_independent(1)
        self.window.navigate_independent(1)
        self.assertEqual((self.window.canvas.path, self.window.compare_canvas.path), (self.paths[0], self.paths[3]))
        self.window.select_side(0)
        for _ in range(5):
            self.window.navigate_independent(1)
        self.assertEqual((self.window.canvas.path, self.window.compare_canvas.path), (self.paths[5], self.paths[3]))
        self.window.navigate(-1)
        self.assertEqual((self.window.canvas.path, self.window.compare_canvas.path), (self.paths[4], self.paths[2]))
        self.window.select_side(1)
        self.window.navigate_independent(-1)
        self.assertEqual((self.window.canvas.path, self.window.compare_canvas.path), (self.paths[4], self.paths[1]))
        self.window.toggle_photo_fullscreen()
        self.window.select_side(0)
        self.window.navigate_independent(-1)
        self.assertEqual((self.window.canvas.path, self.window.compare_canvas.path), (self.paths[3], self.paths[1]))

    def test_mouse_hold_repeats_navigation_without_extra_release_click(self):
        canvas = self.window.canvas
        QTest.mousePress(canvas, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, QPoint(100, 100))
        QTest.qWait(690)
        self.application.processEvents()
        position = self.window.position
        self.assertGreaterEqual(position, 2)
        QTest.mouseRelease(canvas, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, QPoint(100, 100))
        self.assertEqual(self.window.position, position)
        QTest.qWait(200)
        self.assertEqual(self.window.position, position)

    def test_open_photo_selects_it_without_discarding_pending(self):
        self.window.mark_current("trash")
        self.window.open_folder(str(self.photos), self.paths[5])
        self.wait_for(lambda: not self.window.scanning)
        self.assertEqual(self.window.current_path(), self.paths[5])
        self.assertIn(self.paths[0], self.window.pending())

    def test_open_with_registration_uses_only_user_classes_and_keeps_defaults(self):
        class FakeRegistry:
            HKEY_CURRENT_USER = "current_user"
            REG_SZ, REG_NONE = 1, 0
            entries = []
            class Key:
                def __init__(self, path): self.path = path
                def __enter__(self): return self
                def __exit__(self, *_): pass
            @classmethod
            def CreateKey(cls, root, path):
                self.assertEqual(root, cls.HKEY_CURRENT_USER)
                return cls.Key(path)
            @classmethod
            def SetValueEx(cls, key, name, reserved, kind, value):
                cls.entries.append((key.path, name, kind, value))
        executable = self.root / "Koma v0.8.exe"
        register_open_with(executable, FakeRegistry)
        commands = [v for p, n, t, v in FakeRegistry.entries if p.endswith("shell\\open\\command")]
        self.assertEqual(len(commands), 2)
        self.assertTrue(all(v.endswith('"%1"') and str(executable) in v for v in commands))
        for ext in (".jpg", ".jpeg", ".jpe", ".png"):
            self.assertTrue(any(("\\"+ext+"\\OpenWithProgids") in p for p, _, _, _ in FakeRegistry.entries))
        self.assertFalse(any("UserChoice" in p for p, _, _, _ in FakeRegistry.entries))

    def test_keyboard_shortcuts_operate_comparison_and_modes(self):
        self.window.activateWindow()
        self.window.setFocus()
        self.application.processEvents()
        QTest.keyClick(self.window, Qt.Key.Key_2, Qt.KeyboardModifier.AltModifier)
        self.assertTrue(self.window.compare_mode)
        QTest.keyClick(self.window, Qt.Key.Key_Right, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.window.active_side, 1)
        QTest.keyClick(self.window, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual((self.window.canvas.path, self.window.compare_canvas.path), (self.paths[0], self.paths[2]))
        QTest.keyClick(self.window, Qt.Key.Key_Left, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.window.active_side, 0)
        QTest.keyClick(self.window, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual((self.window.canvas.path, self.window.compare_canvas.path), (self.paths[1], self.paths[2]))
        QTest.keyClick(self.window, Qt.Key.Key_3, Qt.KeyboardModifier.AltModifier)
        self.assertEqual(self.window.stack.currentIndex(), 1)
        QTest.keyClick(self.window, Qt.Key.Key_1, Qt.KeyboardModifier.AltModifier)
        self.assertEqual(self.window.stack.currentIndex(), 0)
        self.assertFalse(self.window.compare_mode)

    def test_grid_can_check_multiple_cards_using_keyboard_only(self):
        self.window.set_view(True)
        self.window.activateWindow()
        self.window.grid.setFocus()
        self.application.processEvents()
        self.window.grid.setCurrentRow(0)
        QTest.keyClick(self.window.grid, Qt.Key.Key_Space)
        QTest.keyClick(self.window.grid, Qt.Key.Key_Right)
        QTest.keyClick(self.window.grid, Qt.Key.Key_Space)
        self.assertEqual(self.window.checked, set(self.paths[:2]))
        self.window.assign(0)
        self.assertEqual(set(self.window.state["decisions"]), set(self.paths[:2]))

    def test_fullscreen_sidebar_does_not_clip_with_long_destinations(self):
        for i in range(6):
            self.window.register_destination(str(self.root / ("とても長いフォルダ名" * 4 + str(i))))
        self.window.toggle_app_fullscreen()
        self.application.processEvents()
        self.assertLessEqual(self.window.side_content.width(), self.window.side_scroll.viewport().width())
        self.assertEqual(self.window.side_scroll.horizontalScrollBar().maximum(), 0)
        self.assertGreaterEqual(self.window.parent_button.mapTo(self.window.side_scroll.viewport(), QPoint(0, 0)).x(), 0)

    def test_keyboard_zoom_shortcuts_and_alternatives(self):
        self.window.activateWindow()
        self.window.setFocus()
        self.application.processEvents()
        QTest.keyClick(self.window, Qt.Key.Key_Up, Qt.KeyboardModifier.ControlModifier)
        self.assertGreater(self.window.canvas.zoom_scale, 1.0)
        QTest.keyClick(self.window, Qt.Key.Key_Down, Qt.KeyboardModifier.ControlModifier)
        self.assertAlmostEqual(self.window.canvas.zoom_scale, 1.0)
        QTest.keyClick(self.window, Qt.Key.Key_Plus, Qt.KeyboardModifier.ControlModifier)
        self.assertGreater(self.window.canvas.zoom_scale, 1.0)
        QTest.keyClick(self.window, Qt.Key.Key_Minus, Qt.KeyboardModifier.ControlModifier)
        self.assertAlmostEqual(self.window.canvas.zoom_scale, 1.0)
        QTest.keyClick(self.window, Qt.Key.Key_Plus, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        self.assertGreater(self.window.canvas.zoom_scale, 1.0)

    def test_checked_trash_candidates_can_be_cleared_and_undone(self):
        self.window.make_decisions([self.paths[0], self.paths[1], self.paths[2]], "trash")
        self.window.make_decisions([self.paths[3]], "file", self.window.state["destinations"][0]["path"])
        self.window.set_view(True)
        for index in (0, 2, 3):
            self.window.grid.item(index).setCheckState(Qt.CheckState.Checked)
        self.assertTrue(self.window.batch_clear_trash_button.isEnabled())
        self.window.clear_current()  # 0 key also clears checked trash in grid view
        decisions = self.window.state["decisions"]
        self.assertNotIn(self.paths[0], decisions)
        self.assertNotIn(self.paths[2], decisions)
        self.assertEqual(decisions[self.paths[1]]["action"], "trash")
        self.assertEqual(decisions[self.paths[3]]["action"], "file")
        self.assertIn(self.paths[3], self.window.checked)
        self.assertTrue(all(Path(p).exists() for p in self.paths))
        self.window.undo()
        self.assertEqual({p for p,d in decisions.items() if d["action"] == "trash"}, set(self.paths[:3]))

    def test_film_destination_initials_distinguish_collisions(self):
        self.window.make_decisions([self.paths[0]], "file", self.window.state["destinations"][1]["path"])
        self.window.make_decisions([self.paths[1]], "file", self.window.state["destinations"][2]["path"])
        self.window.position = 0
        self.window.refresh_current()
        self.assertIn("[ス1]", self.window.dest_box.itemAt(1).widget().layout().itemAt(0).widget().text())
        self.assertIn("[ス2]", self.window.dest_box.itemAt(2).widget().layout().itemAt(0).widget().text())
        self.assertIn("[ス1]", self.window.film.item(3).text())
        self.assertIn("[ス2]", self.window.film.item(4).text())
        self.assertIn("ステージ", self.window.film.item(3).toolTip())
        self.window.make_decisions([self.paths[0]], "trash")
        self.assertIn("⌫", self.window.film.item(3).text())

    def test_confirmation_grid_loads_thumbnails_and_large_preview(self):
        self.window.make_decisions(self.paths[:4], "trash")
        dialog = ApplyPreviewDialog(self.window, self.window.pending())
        dialog.show()
        self.wait_for(lambda: not dialog.items.item(0).icon().isNull())
        self.assertEqual(dialog.items.viewMode(), dialog.items.ViewMode.IconMode)
        self.assertEqual(dialog.preview_path, self.paths[0])
        dialog.items.setCurrentRow(2)
        self.assertEqual(dialog.preview_path, self.paths[2])
        self.assertFalse(dialog.preview_image.isNull())
        dialog.reject()
        self.assertTrue(all(Path(p).exists() for p in self.paths))

    def test_first_button_only_at_end_and_home_in_fullscreen(self):
        self.assertFalse(self.window.first_button.isVisible())
        self.window.position = len(self.paths) - 1
        self.window.refresh_current()
        self.assertTrue(self.window.first_button.isVisible())
        QTest.mouseClick(self.window.first_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.window.current_path(), self.paths[0])
        self.assertFalse(self.window.first_button.isVisible())
        self.window.show_compare()
        self.window.position = len(self.paths) - 2
        self.window.refresh_current()
        self.assertTrue(self.window.first_button.isVisible())
        self.window.toggle_photo_fullscreen()
        self.application.processEvents()
        self.assertFalse(self.window.first_button.isVisible())
        self.assertTrue(self.window.first_overlay.isVisible())
        QTest.mouseClick(self.window.first_overlay, Qt.MouseButton.LeftButton)
        self.assertEqual(self.window.canvas.path, self.paths[0])
        self.assertEqual(self.window.compare_canvas.path, self.paths[1])
        self.assertFalse(self.window.first_overlay.isVisible())
        self.window.position = len(self.paths) - 2
        self.window.refresh_current()
        self.assertTrue(self.window.first_overlay.isVisible())
        QTest.keyClick(self.window, Qt.Key.Key_Home)
        self.assertEqual(self.window.canvas.path, self.paths[0])

    def test_control_click_clears_candidate_on_clicked_side_and_is_undoable(self):
        self.window.make_decisions([self.paths[0]], "file", self.window.state["destinations"][0]["path"])
        self.window.make_decisions([self.paths[1]], "trash")
        self.window.show_compare()
        QTest.mouseClick(self.window.compare_canvas, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.ControlModifier, QPoint(100, 100))
        self.assertNotIn(self.paths[1], self.window.state["decisions"])
        self.assertIn(self.paths[0], self.window.state["decisions"])
        self.assertEqual(self.window.compare_canvas.path, self.paths[1])
        self.window.undo()
        self.assertEqual(self.window.state["decisions"][self.paths[1]]["action"], "trash")
        self.window.toggle_photo_fullscreen()
        QTest.mouseClick(self.window.canvas, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.ControlModifier, QPoint(100, 100))
        self.assertNotIn(self.paths[0], self.window.state["decisions"])
        self.assertTrue(all(Path(p).exists() for p in self.paths))

    def test_control_click_clears_the_clicked_grid_photo_without_checking(self):
        self.window.make_decisions([self.paths[1]], "file", self.window.state["destinations"][0]["path"])
        self.window.set_view(True)
        self.application.processEvents()
        rect = self.window.grid.visualItemRect(self.window.grid.item(1))
        QTest.mouseClick(self.window.grid.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.ControlModifier, rect.center())
        self.assertNotIn(self.paths[1], self.window.state["decisions"])
        self.assertFalse(self.window.checked)
        self.window.undo()
        self.assertEqual(self.window.state["decisions"][self.paths[1]]["action"], "file")

    def test_control_click_clears_clicked_film_photo(self):
        self.window.make_decisions([self.paths[1]], "trash")
        self.window.refresh_current()
        self.application.processEvents()
        rect = self.window.film.visualItemRect(self.window.film.item(4))
        QTest.mouseClick(self.window.film.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.ControlModifier, rect.center())
        self.assertNotIn(self.paths[1], self.window.state["decisions"])
        self.assertEqual(self.window.current_path(), self.paths[0])
        self.window.undo()
        self.assertEqual(self.window.state["decisions"][self.paths[1]]["action"], "trash")

    def test_confirmation_tabs_and_click_toggle_changes_without_file_operations(self):
        self.window.make_decisions(self.paths[:2], "trash")
        self.window.make_decisions([self.paths[2]], "file", self.window.state["destinations"][0]["path"])
        self.window.make_decisions([self.paths[3]], "file", self.window.state["destinations"][1]["path"])
        dialog = ApplyPreviewDialog(self.window, self.window.pending())
        dialog.show()
        self.application.processEvents()
        self.assertEqual(dialog.tabs.count(), 4)
        self.assertIn("削除", dialog.tabs.tabText(0))
        self.assertIn("集合写真", dialog.tabs.tabText(1))
        self.assertIn("ステージ", dialog.tabs.tabText(2))
        rect = dialog.items.visualItemRect(dialog.items.item(0))
        QTest.mouseClick(dialog.items.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
        self.assertIn(self.paths[0], dialog.cancelled_paths)
        self.assertIn("解除済", dialog.items.item(0).text())
        self.assertIn("反映対象外", dialog.detail.text())
        QTest.mouseClick(dialog.items.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
        self.assertNotIn(self.paths[0], dialog.cancelled_paths)
        dialog.tabs.setCurrentIndex(2)
        self.assertEqual(dialog.preview_path, self.paths[3])
        rect = dialog.items.visualItemRect(dialog.items.item(0))
        QTest.mouseClick(dialog.items.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
        self.assertIn(self.paths[3], dialog.cancelled_paths)
        dialog.reject()
        self.assertTrue(all(Path(p).exists() for p in self.paths))

    def test_confirmation_shows_empty_destination_tabs_and_selects_first_nonempty(self):
        self.window.make_decisions([self.paths[2]], "file", self.window.state["destinations"][1]["path"])
        dialog = ApplyPreviewDialog(self.window, self.window.pending())
        dialog.show()
        self.application.processEvents()
        self.assertEqual(dialog.tabs.count(), 4)
        self.assertEqual(dialog.tabs.currentIndex(), 2)
        self.assertEqual(dialog.preview_path, self.paths[2])
        self.assertIn("(0)", dialog.tabs.tabText(0))
        self.assertIn("(0)", dialog.tabs.tabText(1))
        dialog.tabs.setCurrentIndex(1)
        self.assertEqual(dialog.preview_path, "")
        self.assertIn("ありません", dialog.preview.text())
        dialog.reject()

    def test_confirmation_cancellations_persist_on_back_and_are_undoable(self):
        self.window.make_decisions([self.paths[0]], "trash")
        self.window.make_decisions([self.paths[1]], "file", self.window.state["destinations"][0]["path"])
        def cancel_and_go_back():
            dialog = QApplication.activeModalWidget()
            try:
                dialog.toggle_candidate(dialog.lists[0].item(0))
                dialog.toggle_candidate(dialog.lists[1].item(0))
            finally:
                dialog.reject()
        QTimer.singleShot(100, cancel_and_go_back)
        self.window.confirm_apply()
        self.assertFalse(self.window.pending())
        self.assertTrue(all(Path(p).exists() for p in self.paths))
        self.window.undo()
        self.assertEqual(set(self.window.pending()), set(self.paths[:2]))

    def test_confirmation_applies_only_remaining_candidates(self):
        self.window.make_decisions([self.paths[0]], "trash")
        self.window.make_decisions([self.paths[1]], "file", self.window.state["destinations"][0]["path"])
        def cancel_one_and_accept():
            dialog = QApplication.activeModalWidget()
            try:
                dialog.toggle_candidate(dialog.lists[0].item(0))
            finally:
                dialog.accept()
        QTimer.singleShot(100, cancel_one_and_accept)
        with patch.object(self.window, "start_apply") as apply:
            self.window.confirm_apply()
        self.assertNotIn(self.paths[0], self.window.state["decisions"])
        self.assertEqual(set(apply.call_args.args[0]), {self.paths[1]})
        self.assertTrue(all(Path(p).exists() for p in self.paths))

    def enter_position(self, value):
        self.window.activateWindow()
        self.window.position_edit.setFocus()
        self.application.processEvents()
        self.window.position_edit.selectAll()
        QTest.keyClicks(self.window.position_edit, str(value))
        QTest.keyClick(self.window.position_edit, Qt.Key.Key_Return)
        self.application.processEvents()

    def test_position_entry_jumps_and_does_not_trigger_destination_shortcuts(self):
        self.enter_position(2)
        self.assertEqual(self.window.current_path(), self.paths[1])
        self.assertEqual(self.window.position_edit.text(), "2")
        self.assertIn("/ 8", self.window.counter_label.text())
        self.assertFalse(self.window.state["decisions"])
        self.assertFalse(self.window.position_edit.hasFocus())
        QTest.keyClick(self.window, Qt.Key.Key_1)
        self.application.processEvents()
        self.assertEqual(self.window.state["decisions"][self.paths[1]]["action"], "file")

    def test_destination_shortcuts_remain_disabled_if_rebuilt_while_editing(self):
        self.window.activateWindow()
        self.window.position_edit.setFocus()
        self.application.processEvents()
        self.window.rebuild_destinations()
        self.window.position_edit.selectAll()
        QTest.keyClicks(self.window.position_edit, "1")
        self.assertFalse(self.window.state["decisions"])
        self.assertEqual(self.window.position_edit.text(), "1")
        QTest.keyClick(self.window.position_edit, Qt.Key.Key_Escape)

    def test_photo_click_leaves_number_edit_and_restores_shortcuts(self):
        self.window.activateWindow()
        self.window.position_edit.setFocus()
        self.application.processEvents()
        self.assertTrue(self.window.position_edit.hasFocus())
        QTest.mouseClick(self.window.canvas, Qt.MouseButton.RightButton,
                         Qt.KeyboardModifier.NoModifier, QPoint(200, 150))
        self.assertFalse(self.window.position_edit.hasFocus())
        self.assertEqual(self.window.current_path(), self.paths[1])
        QTest.keyClick(self.window, Qt.Key.Key_1)
        self.assertEqual(self.window.state["decisions"][self.paths[1]]["action"], "file")

    def test_position_rejects_out_of_range_and_escape_restores(self):
        self.enter_position(999)
        self.assertEqual(self.window.current_path(), self.paths[0])
        self.assertIn("1 から 8", self.window.status_label.text())
        self.assertTrue(self.window.position_edit.hasFocus())
        QTest.keyClick(self.window.position_edit, Qt.Key.Key_Escape)
        self.assertEqual(self.window.position_edit.text(), "1")
        self.assertFalse(self.window.position_edit.hasFocus())
        self.enter_position(0)
        self.assertEqual(self.window.current_path(), self.paths[0])
        self.assertIn("1 から 8", self.window.status_label.text())

    def test_position_uses_filtered_order(self):
        self.window.make_decisions([self.paths[0], self.paths[3], self.paths[6]], "trash")
        self.window.filter_combo.setCurrentIndex(2)
        self.assertEqual(len(self.window.visible), 3)
        self.enter_position(2)
        self.assertEqual(self.window.current_path(), self.paths[3])
        self.assertIn("/ 3", self.window.counter_label.text())

    def test_position_targets_active_comparison_side(self):
        self.window.show_compare()
        self.window.select_side(1)
        self.enter_position(6)
        self.assertEqual((self.window.canvas.path, self.window.compare_canvas.path), (self.paths[0], self.paths[5]))
        self.assertEqual(self.window.position_edit.text(), "6")
        self.window.select_side(0)
        self.assertEqual(self.window.position_edit.text(), "1")
        self.enter_position(4)
        self.assertEqual((self.window.canvas.path, self.window.compare_canvas.path), (self.paths[3], self.paths[5]))

    def test_position_jump_preserves_pinned_left_photo(self):
        self.window.show_compare()
        self.window.toggle_pin()
        self.enter_position(7)
        self.assertEqual(self.window.pinned_path, self.paths[0])
        self.assertEqual((self.window.canvas.path, self.window.compare_canvas.path), (self.paths[0], self.paths[6]))
        self.window.navigate(1)
        self.assertEqual(self.window.canvas.path, self.paths[0])
        self.assertEqual(self.window.compare_canvas.path, self.paths[7])

    def test_position_hidden_when_no_photos_or_in_grid(self):
        self.assertTrue(self.window.position_edit.isVisible())
        self.window.set_view(True)
        self.assertFalse(self.window.position_edit.isVisible())
        empty = self.root / "empty"
        empty.mkdir()
        self.window.show_single()
        self.window.open_folder(str(empty))
        self.wait_for(lambda: not self.window.scanning)
        self.assertFalse(self.window.position_edit.isVisible())
        self.assertEqual(self.window.counter_label.text(), "0枚")

    def test_product_name_and_open_with_friendly_name(self):
        self.assertIn("ピクソート", self.window.windowTitle())
        self.assertNotIn("KOMA", self.window.windowTitle())
        class FakeRegistry:
            HKEY_CURRENT_USER = "current_user"
            REG_SZ, REG_NONE = 1, 0
            entries = []
            class Key:
                def __init__(self, path): self.path = path
                def __enter__(self): return self
                def __exit__(self, *_): pass
            @classmethod
            def CreateKey(cls, root, path): return cls.Key(path)
            @classmethod
            def SetValueEx(cls, key, name, reserved, kind, value):
                cls.entries.append((key.path, name, value))
        register_open_with(self.root / "PixSort.exe", FakeRegistry)
        self.assertTrue(any(name == "FriendlyAppName" and value == "ピクソート" for _, name, value in FakeRegistry.entries))

    def test_refresh_discovers_new_photos_and_folders_keeps_progress_and_selection(self):
        self.window.make_decisions([self.paths[1]], "trash")
        self.window.position = 3
        self.window.refresh_current()
        current = self.window.current_path()
        self.window.checked.add(self.paths[2])
        undo_count = len(self.window.undo_stack)
        added = self.photos / "000-added.jpg"
        shutil.copy2(self.photos / "DEMO_001.jpg", added)
        folder = self.photos / "新しいフォルダ"
        folder.mkdir()
        self.window.refresh_folder()
        self.wait_for(lambda: not self.window.scanning)
        self.assertIn(str(added), self.window.entries)
        self.assertEqual(self.window.current_path(), current)
        self.assertIn(self.paths[1], self.window.pending())
        self.assertIn(self.paths[2], self.window.checked)
        self.assertEqual(len(self.window.undo_stack), undo_count)
        self.assertTrue(any(self.window.folder_list.item(i).data(Qt.ItemDataRole.UserRole) == str(folder)
                            for i in range(self.window.folder_list.count())))
        self.assertIn("更新しました", self.window.status_label.text())
        self.assertTrue(added.exists())

    def test_refresh_reloads_changed_image_and_keeps_candidate_fingerprint(self):
        from core import matches
        path = Path(self.paths[0])
        self.window.make_decisions([str(path)], "trash")
        expected = self.window.state["decisions"][str(path)]["fingerprint"]
        old_pixel = self.window.canvas.image.pixelColor(100, 100).rgba()
        shutil.copyfile(self.photos / "DEMO_005.jpg", path)
        self.window.refresh_folder()
        self.wait_for(lambda: not self.window.scanning and not self.window.canvas.image.isNull())
        self.assertNotEqual(self.window.canvas.image.pixelColor(100, 100).rgba(), old_pixel)
        self.assertEqual(self.window.state["decisions"][str(path)]["fingerprint"], expected)
        self.assertFalse(matches(path, expected))
        self.assertTrue(path.exists())

    def test_refresh_warns_about_missing_candidate_and_keeps_record(self):
        path = Path(self.paths[1])
        contents = path.read_bytes()
        self.window.make_decisions([str(path)], "trash")
        path.unlink()
        self.window.refresh_folder()
        self.wait_for(lambda: not self.window.scanning)
        self.assertNotIn(str(path), self.window.pending())
        self.assertIn(str(path), self.window.state["decisions"])
        self.assertIn("見つからない候補 1件", self.window.status_label.text())
        path.write_bytes(contents)
        self.window.refresh_folder()
        self.wait_for(lambda: not self.window.scanning)
        self.assertIn(str(path), self.window.pending())

    def test_refresh_shows_restored_moved_photo_but_not_completed_copy(self):
        self.window.state["completed"] = {
            self.paths[0]: {"action": "move", "target": "elsewhere"},
            self.paths[1]: {"action": "copy", "target": "elsewhere"}}
        self.window.rebuild_views()
        self.assertNotIn(self.paths[0], self.window.visible)
        self.assertNotIn(self.paths[1], self.window.visible)
        self.window.refresh_folder()
        self.wait_for(lambda: not self.window.scanning)
        self.assertIn(self.paths[0], self.window.visible)
        self.assertNotIn(self.paths[1], self.window.visible)

    def test_refresh_keeps_filter_and_pinned_comparison(self):
        self.window.make_decisions(self.paths[:3], "trash")
        self.window.filter_combo.setCurrentIndex(2)
        self.window.show_compare()
        self.window.toggle_pin()
        self.assertEqual(self.window.canvas.path, self.paths[0])
        self.assertEqual(self.window.compare_canvas.path, self.paths[1])
        shutil.copy2(self.photos / "DEMO_005.jpg", self.photos / "another.jpg")
        self.window.refresh_folder()
        self.wait_for(lambda: not self.window.scanning)
        self.assertEqual(self.window.filter_combo.currentIndex(), 2)
        self.assertEqual(self.window.pinned_path, self.paths[0])
        self.assertEqual((self.window.canvas.path, self.window.compare_canvas.path), tuple(self.paths[:2]))
        self.assertEqual(len(self.window.visible), 3)

    def test_previous_f5_destination_shortcut_is_reassigned_with_notice(self):
        self.window.state["destinations"][0]["key"] = "F5"
        self.window.save()
        self.window.open_folder(str(self.photos))
        self.wait_for(lambda: not self.window.scanning)
        self.assertNotEqual(self.window.state["destinations"][0]["key"], "F5")
        self.assertIn("新しい操作キーとの重複", self.window.status_label.text())
        self.assertIn("集合写真", self.window.status_label.text())

    def test_f5_refreshes_and_failure_preserves_old_list(self):
        added = self.photos / "new.jpg"
        shutil.copy2(self.photos / "DEMO_001.jpg", added)
        self.window.activateWindow()
        self.window.setFocus()
        self.application.processEvents()
        QTest.keyClick(self.window, Qt.Key.Key_F5)
        self.wait_for(lambda: not self.window.scanning)
        self.assertIn(str(added), self.window.entries)
        old_entries = list(self.window.entries)
        with patch("app.scan_current_folder", side_effect=OSError("test scan failure")), \
             patch.object(self.window, "show_error") as show_error:
            self.window.refresh_folder()
            self.wait_for(lambda: not self.window.scanning)
        self.assertEqual(self.window.entries, old_entries)
        show_error.assert_called_once()
        self.assertTrue(self.window.refresh_button.isEnabled())

    def test_click_next_number_destination_and_keyboard_undo(self):
        QTest.mouseClick(self.window.canvas, Qt.MouseButton.RightButton,
                         Qt.KeyboardModifier.NoModifier, QPoint(200, 150))
        self.assertEqual(self.window.current_path(), self.paths[1])
        QTest.mouseClick(self.window.canvas, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, QPoint(200, 150))
        self.assertEqual(self.window.current_path(), self.paths[0])
        QTest.mouseClick(self.window.canvas, Qt.MouseButton.RightButton,
                         Qt.KeyboardModifier.NoModifier, QPoint(200, 150))
        QTest.keyClick(self.window, Qt.Key.Key_2)
        self.application.processEvents()
        self.assertEqual(self.window.current_path(), self.paths[2])
        self.assertEqual(self.window.state["decisions"][self.paths[1]]["destination"],
                         self.window.state["destinations"][1]["path"])
        QTest.keyClick(self.window, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.application.processEvents()
        self.assertNotIn(self.paths[1], self.window.state["decisions"])
        self.assertEqual(self.window.current_path(), self.paths[1])
        self.assertTrue(all(Path(p).exists() for p in self.paths))

    def test_non_contiguous_whole_card_checkbox_batch_delete_and_undo(self):
        self.window.set_view(True)
        self.application.processEvents()
        indexes = [0, 3, 6]
        for index in indexes:
            rect = self.window.grid.visualItemRect(self.window.grid.item(index))
            # Click the center of the photo, not the checkbox itself.
            QTest.mouseClick(self.window.grid.viewport(), Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.NoModifier, rect.center())
        expected = {self.paths[i] for i in indexes}
        self.assertEqual(self.window.checked, expected)
        QTest.mouseClick(self.window.batch_trash_button, Qt.MouseButton.LeftButton)
        self.assertEqual(set(self.window.state["decisions"]), expected)
        self.assertTrue(all(d["action"] == "trash" for d in self.window.state["decisions"].values()))
        self.assertTrue(all(Path(p).exists() for p in self.paths))
        self.window.undo()
        self.assertEqual(self.window.state["decisions"], {})

    def test_checkbox_toggle_all_clear_and_batch_destination(self):
        self.window.set_view(True)
        self.window.check_all()
        self.assertEqual(len(self.window.checked), 8)
        self.window.uncheck_all()
        self.assertEqual(len(self.window.checked), 0)
        for index in (1, 5):
            self.window.grid.item(index).setCheckState(Qt.CheckState.Checked)
        self.window.assign(0)
        self.assertEqual(set(self.window.state["decisions"]), {self.paths[1], self.paths[5]})
        self.assertFalse(self.window.checked)

    def test_shift_click_marks_photo_and_shift_drag_remains_available(self):
        QTest.mouseClick(self.window.canvas, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.ShiftModifier, QPoint(200, 150))
        self.assertEqual(self.window.current_path(), self.paths[1])
        self.assertEqual(self.window.state["decisions"][self.paths[0]]["action"], "trash")
        self.assertTrue(Path(self.paths[0]).exists())
        with patch.object(QDrag, "exec", return_value=Qt.DropAction.IgnoreAction) as drag:
            QTest.mousePress(self.window.canvas, Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.ShiftModifier, QPoint(200, 150))
            # Explicit mouse move with Shift makes this independent of the test platform's keyboard state.
            from PySide6.QtCore import QEvent
            from PySide6.QtGui import QMouseEvent
            move = QMouseEvent(QEvent.Type.MouseMove, QPointF(240, 190), QPointF(240, 190),
                               Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
            QApplication.sendEvent(self.window.canvas, move)
            self.assertEqual(drag.call_count, 1)
        self.assertEqual(set(self.window.state["decisions"]), {self.paths[0]})

    def test_wheel_zoom_keeps_point_under_cursor_and_returns_to_fit(self):
        canvas = self.window.canvas
        old = canvas.image_rect()
        point = QPointF(old.left() + old.width() * .32, old.top() + old.height() * .38)
        event = QWheelEvent(point, QPointF(canvas.mapToGlobal(point.toPoint())), QPoint(), QPoint(0, 120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.ScrollUpdate, False)
        QApplication.sendEvent(canvas, event)
        new = canvas.image_rect()
        self.assertGreater(canvas.zoom_scale, 1.0)
        self.assertAlmostEqual((point.x() - new.left()) / new.width(), .32, places=5)
        self.assertAlmostEqual((point.y() - new.top()) / new.height(), .38, places=5)
        reverse = QWheelEvent(point, QPointF(canvas.mapToGlobal(point.toPoint())), QPoint(), QPoint(0, -120),
                              Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                              Qt.ScrollPhase.ScrollUpdate, False)
        QApplication.sendEvent(canvas, reverse)
        self.assertEqual(canvas.zoom_scale, 1.0)
        self.assertEqual(canvas.offset, QPointF())

    def test_two_up_shift_click_targets_clicked_photo_and_pin_stays_fixed(self):
        self.window.show_compare()
        self.assertEqual(self.window.canvas.path, self.paths[0])
        self.assertEqual(self.window.compare_canvas.path, self.paths[1])
        self.wait_for(lambda: not self.window.compare_canvas.image.isNull())
        QTest.mouseClick(self.window.compare_canvas, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.ShiftModifier, QPoint(100, 100))
        self.assertEqual(set(self.window.state['decisions']), {self.paths[1]})
        self.assertEqual(self.window.state['decisions'][self.paths[1]]['action'], 'trash')
        self.assertTrue(Path(self.paths[1]).exists())
        self.window.undo()
        self.assertEqual(self.window.canvas.path, self.paths[0])
        self.window.toggle_pin()
        self.assertEqual(self.window.pinned_path, self.paths[0])
        self.assertEqual(self.window.compare_canvas.path, self.paths[1])
        QTest.mouseClick(self.window.compare_canvas, Qt.MouseButton.RightButton,
                         Qt.KeyboardModifier.NoModifier, QPoint(100, 100))
        self.assertEqual(self.window.canvas.path, self.paths[0])
        self.assertEqual(self.window.compare_canvas.path, self.paths[2])

    def test_filmstrip_shows_only_three_before_and_after_current(self):
        self.window.position = 4
        self.window.refresh_current()
        self.assertEqual(self.window.film.count(), 7)
        self.assertEqual([self.window.film.item(i).data(Qt.ItemDataRole.UserRole)
                          for i in range(7)], self.paths[1:8])
        self.window.position = 0
        self.window.refresh_current()
        self.assertEqual(self.window.film.count(), 7)
        self.assertEqual([self.window.film.item(i).data(Qt.ItemDataRole.UserRole)
                          for i in range(7)], ['', '', '', *self.paths[:4]])
        self.assertIn('現在', self.window.film.item(3).text())
        self.assertIn('次', self.window.film.item(4).text())
        self.assertEqual(self.window.film.currentRow(), 3)
        self.window.position = 4
        self.window.refresh_current()
        self.assertIn('前', self.window.film.item(2).text())
        self.assertIn('現在', self.window.film.item(3).text())
        self.assertIn('次', self.window.film.item(4).text())
        self.window.film_clicked(self.window.film.item(2))
        self.assertEqual(self.window.current_path(), self.paths[3])

    def test_fullscreen_prefetch_displays_next_preview_immediately(self):
        self.wait_for(lambda: (self.paths[1], "preview") in self.window.images)
        self.window.toggle_photo_fullscreen()
        self.assertTrue(self.window.photo_fullscreen)
        self.window.navigate(1)
        self.assertEqual(self.window.canvas.path, self.paths[1])
        self.assertFalse(self.window.canvas.image.isNull())
        self.assertFalse(self.window.canvas.loading)
        self.assertNotIn((self.paths[1], "full"), self.window.image_pending)
        self.assertNotIn((self.paths[1], "full"), self.window.images)
        # Delayed results for another image must not replace the current photo.
        width = self.window.canvas.image.width()
        stale = QImage(7, 7, QImage.Format.Format_RGB32)
        self.window.image_ready({"key": (self.paths[0], "preview"), "image": stale})
        self.assertEqual(self.window.canvas.image.width(), width)
        self.window.toggle_zoom()
        self.wait_for(lambda: (self.paths[1], "full") in self.window.images)
        self.assertFalse(self.window.canvas.image.isNull())
        self.assertTrue(self.window.canvas.zoomed)

    def test_fullscreen_comparison_prefetches_next_pair(self):
        self.window.show_compare()
        self.window.toggle_photo_fullscreen()
        self.wait_for(lambda: (self.paths[2], "preview") in self.window.images)
        self.window.navigate(1)
        self.assertEqual((self.window.canvas.path, self.window.compare_canvas.path), tuple(self.paths[1:3]))
        self.assertFalse(self.window.canvas.image.isNull())
        self.assertFalse(self.window.compare_canvas.image.isNull())
        self.window.toggle_pin()
        self.wait_for(lambda: (self.paths[3], "preview") in self.window.images)
        self.window.navigate(1)
        self.assertEqual(self.window.canvas.path, self.paths[1])
        self.assertFalse(self.window.compare_canvas.image.isNull())

    def test_middle_click_fullscreen_navigation_toggle_and_escape(self):
        current = self.window.current_path()
        QTest.mouseClick(self.window.canvas, Qt.MouseButton.MiddleButton,
                         Qt.KeyboardModifier.NoModifier, QPoint(200, 150))
        self.application.processEvents()
        self.assertTrue(self.window.photo_fullscreen)
        self.assertTrue(self.window.isFullScreen())
        self.assertFalse(self.window.header_frame.isVisible())
        self.assertFalse(self.window.sidebar_frame.isVisible())
        self.assertFalse(self.window.film.isVisible())
        self.assertEqual(self.window.current_path(), current)
        QTest.mouseClick(self.window.canvas, Qt.MouseButton.RightButton,
                         Qt.KeyboardModifier.NoModifier, QPoint(200, 150))
        self.assertEqual(self.window.current_path(), self.paths[1])
        QTest.mouseClick(self.window.canvas, Qt.MouseButton.MiddleButton,
                         Qt.KeyboardModifier.NoModifier, QPoint(200, 150))
        self.application.processEvents()
        self.assertFalse(self.window.photo_fullscreen)
        self.assertTrue(self.window.header_frame.isVisible())
        self.assertTrue(self.window.film.isVisible())
        QTest.mouseClick(self.window.canvas, Qt.MouseButton.MiddleButton,
                         Qt.KeyboardModifier.NoModifier, QPoint(200, 150))
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.application.processEvents()
        self.assertFalse(self.window.photo_fullscreen)
        self.assertEqual(self.window.current_path(), self.paths[1])
        self.assertEqual(self.window.state['decisions'], {})

    def test_fullscreen_comparison_keeps_both_photos_and_restores_mode(self):
        self.window.show_compare()
        QTest.mouseClick(self.window.compare_canvas, Qt.MouseButton.MiddleButton,
                         Qt.KeyboardModifier.NoModifier, QPoint(100, 100))
        self.application.processEvents()
        self.assertTrue(self.window.photo_fullscreen)
        self.assertEqual((self.window.canvas.path, self.window.compare_canvas.path), tuple(self.paths[:2]))
        self.assertTrue(self.window.compare_canvas.isVisible())
        self.assertFalse(self.window.pane_labels[0].isVisible())
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.application.processEvents()
        self.assertTrue(self.window.compare_mode)
        self.assertTrue(self.window.pane_labels[0].isVisible())
        self.assertTrue(self.window.pin_button.isVisible() or
                        (self.window.tools_button.isVisible() and self.window.pin_action.isEnabled()))

    def test_previous_database_is_copied_once_even_with_wal_open(self):
        old = self.root / 'legacy-state'
        new = self.root / 'new-state'
        store = Store(old)
        old_state = store.load(str(self.photos))
        old_state['decisions']['example'] = {'action':'hold'}
        store.save(str(self.photos), old_state)
        (old / 'settings.json').write_text(json.dumps({'last_root': str(self.photos)}), encoding='utf-8')
        self.assertTrue(migrate_previous_work(new, old))
        restored = Store(new)
        self.assertEqual(restored.load(str(self.photos))['decisions'], {'example': {'action':'hold'}})
        self.assertEqual(json.loads((new / 'settings.json').read_text(encoding='utf-8'))['last_root'], str(self.photos))
        old_state['decisions']['later'] = {'action':'trash'}
        store.save(str(self.photos), old_state)
        self.assertFalse(migrate_previous_work(new, old))
        self.assertNotIn('later', restored.load(str(self.photos))['decisions'])
        restored.close()
        store.close()

    def test_custom_key_capture_conflicts_persistence_and_dispatch(self):
        dialog = KeyCaptureDialog(self.window, 0)
        dialog.setModal(True)
        dialog.show()
        self.application.processEvents()
        QTest.keyClick(dialog, Qt.Key.Key_2)
        self.assertIn('使われています', dialog.message.text())
        self.assertIsNone(dialog.value)
        QTest.keyClick(dialog, Qt.Key.Key_X)
        self.assertIn('削除候補', dialog.message.text())
        QTest.keyClick(dialog, Qt.Key.Key_A)
        self.assertEqual(dialog.value, 'A')
        self.assertEqual(self.window.set_destination_key(0, dialog.value), '')
        self.assertIn('使われています', self.window.set_destination_key(1, 'A'))
        self.assertIn('取り消し', self.window.set_destination_key(1, 'Ctrl+Z'))
        self.window.activateWindow()
        self.window.setFocus()
        self.application.processEvents()
        QTest.keyClick(self.window, Qt.Key.Key_1)
        self.application.processEvents()
        self.assertFalse(self.window.state['decisions'])
        QTest.keyClick(self.window, Qt.Key.Key_A)
        self.application.processEvents()
        self.assertEqual(self.window.state['decisions'][self.paths[0]]['destination'],
                         self.window.state['destinations'][0]['path'])
        self.window.open_folder(str(self.photos))
        self.wait_for(lambda: not self.window.scanning)
        self.assertEqual(self.window.state['destinations'][0]['key'], 'A')

    def test_drop_zone_marks_only_dragged_photo_and_advances(self):
        mime = QMimeData()
        mime.setData("application/x-koma-photo", self.paths[0].encode("utf-8"))
        enter = QDragEnterEvent(QPoint(30, 30), Qt.DropAction.MoveAction, mime,
                                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
        QApplication.sendEvent(self.window.trash_zone, enter)
        self.assertTrue(enter.isAccepted())
        drop = QDropEvent(QPointF(30, 30), Qt.DropAction.MoveAction, mime,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
        QApplication.sendEvent(self.window.trash_zone, drop)
        self.assertTrue(drop.isAccepted())
        self.assertEqual(self.window.state["decisions"][self.paths[0]]["action"], "trash")
        self.assertEqual(self.window.current_path(), self.paths[1])
        self.assertTrue(Path(self.paths[0]).exists())

    def test_restart_and_unreviewed_filter_keep_progress(self):
        self.window.mark_current("hold")
        self.window.assign(0)
        cursor = self.window.current_path()
        self.window.open_folder(str(self.photos))
        self.wait_for(lambda: not self.window.scanning)
        self.assertEqual(self.window.current_path(), cursor)
        self.assertEqual(len(self.window.state["decisions"]), 2)
        self.window.filter_combo.setCurrentIndex(1)
        self.assertEqual(len(self.window.visible), 6)

    def test_file_execution_from_ui_moves_and_verifies_only_queued_photo(self):
        self.window.assign(0)
        source = Path(self.paths[0])
        original = source.read_bytes()
        self.window.start_apply(self.window.pending())
        self.wait_for(lambda: not self.window.applying)
        self.assertFalse(source.exists())
        self.assertTrue(Path(self.paths[1]).exists())
        target = self.photos / "整理後/集合写真" / source.name
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(self.window.pending())
        self.assertEqual(len(self.window.visible), 7)

    def test_file_execution_from_ui_trashes_a_disposable_photo_in_worker(self):
        source = Path(self.paths[0])
        self.window.mark_current('trash')
        self.window.start_apply(self.window.pending())
        self.wait_for(lambda: not self.window.applying)
        self.assertFalse(source.exists())
        operation = self.window.store.history()[0]
        self.assertEqual(operation[2], 'done')
        if sys.platform == 'win32':
            self.assertTrue(Path(operation[4]).exists())


if __name__ == "__main__":
    unittest.main()
