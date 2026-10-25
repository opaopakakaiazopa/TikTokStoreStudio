"""Verify the 80% timing in actual FFmpeg frames and CPU/NVENC HDR exports."""
from array import array
from pathlib import Path
from unittest.mock import patch
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from html.parser import HTMLParser
from urllib.parse import urlsplit

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core
import progress_overlay as meter

PROJECT = core.ROOT
FFMPEG = core.executable("ffmpeg")
FFPROBE = core.executable("ffprobe")


class ProgressChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="studio-progress-")
        cls.folder = Path(cls.temp.name)
        cls.patches = [patch.object(core,"ROOT",cls.folder),patch.object(core,"DB",cls.folder/"data/studio.db"),
                       patch.object(core,"executable",lambda name:FFMPEG if name=="ffmpeg" else FFPROBE)]
        for item in cls.patches:item.start()
        core.initialize()
        (cls.folder/"static").mkdir()
        cls.config = {**core.DEFAULTS,"resolution":"720","font_path":core.font_path("arial"),"progress_enabled":True,
                      "progress_width":60,"progress_height":20,"progress_color":"#ffffff","progress_track_color":"#000000"}
        cls.source=cls.folder/"source.mp4"
        core.run([FFMPEG,"-v","error","-y","-f","lavfi","-i","color=black:s=320x180:r=30:d=3",
                  "-f","lavfi","-i","sine=d=3","-c:v","libx264","-preset","ultrafast","-c:a","aac","-shortest",str(cls.source)])

    @classmethod
    def tearDownClass(cls):
        for item in reversed(cls.patches):item.stop()
        cls.temp.cleanup()

    def spec(self,name):
        overlay=self.folder/"output/temp"/(name+".png")
        core.make_overlay("ТЕСТ HDR",self.config,overlay)
        spec={"settings":dict(self.config),"duration":3.0,"start":0,"music_start":0,"overlay":str(overlay),
              "background":{"path":str(self.source),"info":core.probe(self.source)},"music":{"path":str(self.source)}}
        core.ensure_progress_assets(spec)
        return spec

    def test_curve_is_fast_then_slow_and_finishes_at_exactly_80_percent(self):
        self.assertEqual(meter.fraction(0),0)
        self.assertGreater(meter.fraction(.1),.1/.8)
        self.assertGreater(meter.fraction(.1)-meter.fraction(0),meter.fraction(.7)-meter.fraction(.6))
        self.assertLess(meter.fill_width(500,.799999),500)
        self.assertEqual(meter.fill_width(500,.8),500)
        self.assertEqual(meter.fill_width(500,1),500)

    def test_actual_ffmpeg_alpha_frames_are_empty_then_full_at_80_percent(self):
        spec=self.spec("alpha")
        assets=spec["progress_assets"];g=assets["geometry"]
        raw=subprocess.run([FFMPEG,"-v","error","-loop","1","-framerate","30","-i",assets["fill"],
            "-vf",meter.alpha_filter(g["width"],3,self.config["progress_easing"]),"-frames:v","91","-pix_fmt","rgba","-f","rawvideo","pipe:1"],capture_output=True,check=True,creationflags=core.CREATE_FLAGS).stdout
        size=g["width"]*g["bar_height"]*4
        def row(index):
            start=index*size+(g["bar_height"]//2)*g["width"]*4
            return raw[start+3:start+g["width"]*4:4]
        self.assertEqual(max(row(0)),0)
        for index in (9,36,71):
            actual=sum(value>127 for value in row(index))
            expected=meter.fill_width(g["width"],index/90,self.config["progress_easing"])
            self.assertAlmostEqual(actual,expected,delta=2)
        self.assertEqual(row(71)[-1],0)
        self.assertGreater(row(72)[-1],200)
        self.assertEqual(row(72),row(90))

    def encode(self,backend):
        import worker
        tag=backend+"-"+uuid.uuid4().hex[:6]
        spec=self.spec(tag)
        job_id="progress-"+tag
        with core.database() as db:
            db.execute("INSERT INTO jobs(id,status,created,updated) VALUES (?,?,?,?)",(job_id,"rendering",core.now(),core.now()))
        output=self.folder/(tag+".mp4")
        result=worker.encode({"id":job_id},spec,Path(spec["overlay"]),output,backend,self.folder/"logs"/(tag+".log"))
        worker.validate_result(result,3)
        metadata=json.loads(core.run([FFPROBE,"-v","error","-select_streams","v:0","-read_intervals","%+#1","-show_frames","-of","json",str(output)]))
        self.assertTrue(any("Mastering display" in side.get("side_data_type","") for frame in metadata["frames"] for side in frame.get("side_data_list",[])))
        if not self.config["progress_enabled"]:
            return
        g=spec["progress_assets"]["geometry"]
        def luma(time):
            raw=subprocess.run([FFMPEG,"-v","error","-ss",str(time),"-i",str(output),"-frames:v","1","-pix_fmt","yuv420p10le","-f","rawvideo","pipe:1"],capture_output=True,check=True,creationflags=core.CREATE_FLAGS).stdout
            pixels=array('H');pixels.frombytes(raw[:720*1280*2])
            offset=(g["y"]+g["bar_height"]//2)*720+g["x"]
            return pixels[offset:offset+g["width"]]
        empty,partial,full,tail=luma(0),luma(1.2),luma(2.4),luma(2.8)
        self.assertLess(max(empty),120)
        self.assertGreater(min(full[3:-3]),450)
        measured=sum(value>300 for value in partial)
        self.assertAlmostEqual(measured/g["width"],meter.fraction(.4),delta=.025)
        self.assertLess(max(abs(a-b) for a,b in zip(full[3:-3],tail[3:-3])),20)

    def test_cpu_export_is_hdr_and_contains_the_animated_bar(self):
        self.encode("cpu")

    def test_nvidia_export_keeps_hdr_metadata_with_extra_inputs(self):
        if not core.detect_hardware()["nvenc"]:self.skipTest("NVIDIA Main10 unavailable")
        self.encode("nvidia")

    def test_nvidia_without_progress_keeps_hdr_metadata(self):
        if not core.detect_hardware()["nvenc"]:self.skipTest("NVIDIA Main10 unavailable")
        self.config={**self.config,"progress_enabled":False}
        self.encode("nvidia")

    def test_long_custom_label_is_fitted_and_configuration_is_validated(self):
        config={**self.config,"progress_text":"В КОНЦЕ ШКАЛЫ — ПРОМО НА НОВУЮ КОЛЛЕКЦИЮ"}
        base,fill,g=meter.artwork(config)
        self.assertLess(base.height,1280)
        self.assertEqual(fill.height,g["bar_height"])
        with self.assertRaises(ValueError):core.clean_settings({"progress_easing":1})
        with self.assertRaises(ValueError):core.clean_settings({"progress_text":"x"*201})

    def test_preview_api_caches_style_and_keeps_finish_fixed(self):
        from fastapi.testclient import TestClient
        from app import app
        client=TestClient(app)
        payload={"settings":{**self.config,"progress_text":"В КОНЦЕ ШКАЛЫ — ПРОМО","progress_finish":25}}
        response=client.post("/api/preview/progress",json=payload)
        self.assertEqual(response.status_code,200)
        data=response.json()
        self.assertEqual(data["geometry"]["finish_fraction"],.8)
        self.assertEqual(client.get(data["fill_url"]).headers["content-type"],"image/png")
        again=client.post("/api/preview/progress",json=payload).json()
        self.assertEqual(data["base_url"],again["base_url"])
        changed=client.post("/api/preview/progress",json={"settings":{**self.config,"progress_text":"В КОНЦЕ ИМБА"}}).json()
        self.assertNotEqual(data["base_url"],changed["base_url"])
        disabled=client.post("/api/preview/progress",json={"settings":{"progress_enabled":False}}).json()
        self.assertFalse(disabled["enabled"])

    def test_html_loads_each_feature_script_separately(self):
        class Scripts(HTMLParser):
            def __init__(self):super().__init__();self.paths=[]
            def handle_starttag(self,tag,attrs):
                source=dict(attrs).get("src","")
                if tag=="script" and source:self.paths.append(urlsplit(source).path)
        page=Scripts()
        page.feed((PROJECT/"templates/index.html").read_text(encoding="utf-8"))
        self.assertEqual(page.paths,["/static/app.js","/static/preview.js","/static/progress.js","/static/publishing.js"])


if __name__=="__main__":
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ProgressChecks))
    report={"checks":result.testsRun,"passed":result.wasSuccessful(),"failures":len(result.failures),"errors":len(result.errors),"skipped":len(result.skipped),
            "scope":"80% easing, actual FFmpeg alpha frames, CPU/NVIDIA HDR10 exports and mastering metadata"}
    (PROJECT/"docs/progress-check-results.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)
