import os, sys, tempfile, unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw, ImageEnhance, ImageChops
from similar import find_similar, signature_for, similarity


class SimilarityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("KOMA_TEST_DIR"))
        self.root = Path(self.tmp.name)
        self.base = Image.new("RGB", (480, 320), (40, 90, 140))
        draw=ImageDraw.Draw(self.base)
        draw.rectangle((50, 120, 240, 310), fill=(220, 160, 40))
        draw.ellipse((270, 55, 400, 200), fill=(240, 230, 120))
        draw.line((0, 280, 480, 220), fill=(20, 30, 40), width=12)

    def tearDown(self): self.tmp.cleanup()

    def save(self, name, image, second=None, orientation=None):
        p=self.root/name; exif=Image.Exif()
        if second is not None: exif[36867]=f"2026:10:30 14:00:{second:02d}"
        if orientation is not None: exif[274]=orientation
        image.save(p, quality=94, exif=exif); return str(p)

    def test_same_scene_survives_brightness_and_small_camera_shift(self):
        a=self.save("a.jpg",self.base,0)
        b=self.save("b.jpg",ImageEnhance.Brightness(self.base).enhance(1.35),1)
        c=self.save("c.jpg",ImageChops.offset(self.base, 5, 2),2)
        before=[Path(p).read_bytes() for p in (a,b,c)]
        result=find_similar([a,b,c], time_window=3, threshold=.83)
        self.assertEqual(result["groups"],[[a,b,c]])
        self.assertEqual(before,[Path(p).read_bytes() for p in (a,b,c)])
        self.assertGreater(similarity(signature_for(a),signature_for(b)), .90)

    def test_different_composition_does_not_join_even_at_same_time(self):
        a=self.save("a.jpg",self.base,0)
        other=Image.new("RGB",self.base.size,(15,15,15))
        d=ImageDraw.Draw(other);d.polygon(((0,0),(480,0),(240,250)),fill=(230,230,230))
        b=self.save("b.jpg",other,1)
        self.assertEqual(find_similar([a,b],3,.83)["groups"],[])

    def test_time_and_composition_modes_are_distinct_and_missing_time_is_reported(self):
        a=self.save("a.jpg",self.base,0);b=self.save("b.jpg",self.base,20)
        c=self.save("c.jpg",self.base)
        time=find_similar([a,b,c],3,.83)
        self.assertEqual(time["groups"],[]);self.assertEqual(time["missing_time"],1)
        visual=find_similar([a,b,c],None,.83)
        self.assertEqual(visual["groups"],[[a,b,c]])

    def test_near_times_do_not_chain_across_anchor_window(self):
        paths=[self.save(f"{s}.jpg",self.base,s) for s in (0,2,4)]
        result=find_similar(paths,3,.83)
        self.assertEqual(result["groups"],[[paths[0],paths[1]]])

    def test_unreadable_and_cancellation(self):
        a=self.save("a.jpg",self.base,0);bad=self.root/"bad.jpg";bad.write_bytes(b"not an image")
        result=find_similar([a,str(bad)],None,.83)
        self.assertEqual(result["unreadable"],1)
        self.assertTrue(find_similar([a],None,.83,cancelled=lambda:True)["cancelled"])

    def test_nested_camera_exif_and_subseconds_are_read(self):
        path=self.root/"nested.jpg"
        exif=Image.Exif(); exif[34665]={36867:"2026:10:30 14:00:01",37521:"250"}
        self.base.save(path,exif=exif)
        timestamp=signature_for(str(path)).timestamp
        self.assertIsNotNone(timestamp)
        self.assertAlmostEqual(timestamp % 1, .25, places=4)

    def test_portrait_and_landscape_do_not_become_stretched_matches(self):
        a=self.save("a.jpg",self.base,0)
        b=self.save("b.jpg",self.base.transpose(Image.Transpose.ROTATE_90),1)
        self.assertEqual(similarity(signature_for(a),signature_for(b)),0)
