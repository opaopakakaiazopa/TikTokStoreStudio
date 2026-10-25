"""Check text geometry and actual PNGs in an isolated workspace."""
from pathlib import Path
from unittest.mock import patch
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
import core

PROJECT = core.ROOT


class EditorChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="studio-editor-")
        self.folder = Path(self.temp.name)
        self.config = {**core.DEFAULTS, "font_size": 48, "min_font_size": 48,
                       "font_path": core.font_path("arial"), "stroke": 0}
        self.index = 0

    def tearDown(self):
        self.temp.cleanup()

    def render(self, text="ДЛИННАЯ СТРОКА\nОК", **settings):
        path = self.folder / f"{self.index}.png"
        self.index += 1
        layout = core.make_overlay(text, {**self.config, **settings}, path)
        with Image.open(path) as source:
            image = source.copy()
        return layout, image

    def test_horizontal_position_is_not_clamped_to_old_safe_area(self):
        middle, _ = self.render("НОВЫЙ ТЕКСТ", text_x=50)
        shifted, image = self.render("НОВЫЙ ТЕКСТ", text_x=70)
        self.assertAlmostEqual(shifted["bbox"][0]-middle["bbox"][0], 216, delta=1)
        edge, image = self.render("НОВЫЙ ТЕКСТ", text_x=100)
        self.assertGreater(edge["bbox"][2], 1080 * .95)
        self.assertEqual(edge["bbox"][2], 1080)
        self.assertEqual(image.getchannel("A").getbbox(), edge["bbox"])

    def test_tracking_changes_glyph_positions_and_preserves_text(self):
        normal, _ = self.render("ПРИВЕТ", letter_spacing=0)
        spaced, _ = self.render("ПРИВЕТ", letter_spacing=10)
        self.assertGreaterEqual((spaced["bbox"][2]-spaced["bbox"][0])-(normal["bbox"][2]-normal["bbox"][0]), 48)
        self.assertEqual(spaced["lines"], 1)

    def test_line_spacing_and_manual_break_spacing_are_distinct(self):
        compact, _ = self.render(line_spacing=.8)
        loose, _ = self.render(line_spacing=1.8)
        self.assertGreater(loose["bbox"][3]-loose["bbox"][1], compact["bbox"][3]-compact["bbox"][1]+40)
        plain, _ = self.render(paragraph_spacing=0)
        paragraph, _ = self.render(paragraph_spacing=30)
        self.assertAlmostEqual(paragraph["line_boxes"][1][1]-plain["line_boxes"][1][1], 30, delta=1)
        auto = "ДЛИННЫЙ ТЕКСТ БЕЗ РУЧНЫХ ПЕРЕНОСОВ СТРОКИ"
        a, _ = self.render(auto, text_width=30, paragraph_spacing=0)
        b, _ = self.render(auto, text_width=30, paragraph_spacing=80)
        self.assertGreater(a["lines"], 1)
        self.assertEqual(a["bbox"], b["bbox"])

    def test_alignment_matches_actual_line_boxes(self):
        left, _ = self.render(text_align="left")
        center, _ = self.render(text_align="center")
        right, _ = self.render(text_align="right")
        self.assertEqual(left["line_boxes"][0][0], left["line_boxes"][1][0])
        self.assertGreater(center["line_boxes"][1][0], center["line_boxes"][0][0])
        self.assertEqual(right["line_boxes"][0][2], right["line_boxes"][1][2])

    def test_rotation_and_opacity_are_in_the_actual_png(self):
        flat, _ = self.render("ПОВОРОТ")
        rotated, _ = self.render("ПОВОРОТ", text_rotation=90)
        self.assertAlmostEqual(flat["bbox"][2]-flat["bbox"][0], rotated["bbox"][3]-rotated["bbox"][1], delta=1)
        half, image = self.render(text_opacity=50)
        self.assertEqual(image.getchannel("A").getextrema()[1], 128)
        transparent, image = self.render(text_opacity=0)
        self.assertIsNone(transparent["bbox"])
        self.assertEqual(image.getchannel("A").getextrema(), (0, 0))

    def test_frame_constraint_and_free_positioning(self):
        constrained, _ = self.render("У КРАЯ", text_x=0)
        free, _ = self.render("У КРАЯ", text_x=0, keep_text_in_frame=False)
        self.assertEqual(constrained["bbox"][0], 0)
        self.assertGreater(constrained["bbox"][2], free["bbox"][2])
        self.assertAlmostEqual(free["position"]["x"], 0, delta=.1)
        lower, _ = self.render(text_y=100, text_rotation=35)
        self.assertLessEqual(lower["bbox"][3], 1920)
        self.assertGreater(lower["limits"]["y"][0], 0)

    def test_resolution_scales_font_tracking_and_pixel_dimensions(self):
        hd, _ = self.render(letter_spacing=6, paragraph_spacing=20)
        smaller, _ = self.render(resolution="720", letter_spacing=6, paragraph_spacing=20)
        self.assertEqual((smaller["width"], smaller["height"]), (720, 1280))
        self.assertAlmostEqual((hd["bbox"][2]-hd["bbox"][0])/1.5, smaller["bbox"][2]-smaller["bbox"][0], delta=4)
        self.assertEqual(smaller["font_size"], 32)

    def test_unicode_clusters_are_not_split_by_tracking(self):
        from typography import clusters
        self.assertEqual(len(clusters("е\u0301")), 1)
        self.assertEqual(len(clusters("👩\u200d💻")), 1)
        self.assertEqual(len(clusters("🇧🇾")), 1)
        layout, _ = self.render("Привет, мир!", letter_spacing=2.5)
        self.assertIsNotNone(layout["bbox"])

    def test_preview_cache_changes_for_each_style_and_matches_export_overlay(self):
        (self.folder / "static").mkdir()
        with patch.object(core, "ROOT", self.folder), patch.object(core, "DB", self.folder / "data/studio.db"):
            core.initialize()
            from fastapi.testclient import TestClient
            from app import app
            client = TestClient(app)
            base = {**self.config, "text_width": 90}
            text = "ДЛИННАЯ СТРОКА\nОК"
            first = client.post("/api/preview/layer", json={"settings": base, "text": text}).json()
            variants = {"line_spacing": 1.7, "letter_spacing": 5, "paragraph_spacing": 25,
                        "text_align": "right", "text_opacity": 60, "text_rotation": 20, "keep_text_in_frame": False}
            urls = {first["url"]}
            for key, value in variants.items():
                response = client.post("/api/preview/layer", json={"settings": {**base, key:value}, "text": text})
                self.assertEqual(response.status_code, 200)
                result = response.json()
                self.assertNotIn(result["url"], urls)
                urls.add(result["url"])
                reference = self.folder / "export-overlay.png"
                core.make_overlay(text, {**base, key:value}, reference)
                self.assertEqual(client.get(result["url"]).content, reference.read_bytes())
            again = client.post("/api/preview/layer", json={"settings": base, "text": text}).json()
            self.assertEqual(first["url"], again["url"])
            self.assertEqual(client.post("/api/settings", json={"line_spacing": 99}).status_code, 400)
            self.assertEqual(client.post("/api/settings", json={"text_align": "unknown"}).status_code, 400)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(EditorChecks)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {"checks":result.testsRun,"passed":result.wasSuccessful(),"failures":len(result.failures),"errors":len(result.errors),
              "scope":"actual PNG geometry, tracking, line spacing, alignment, opacity/rotation, frame bounds, preview/export parity"}
    (PROJECT / "docs/editor-check-results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)
