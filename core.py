from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import math
import os
import random
import re
import shutil
import sqlite3
import subprocess
import textwrap
import threading
import time
import uuid

from PIL import Image, ImageDraw, ImageFont
import typography
import progress_overlay

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "studio.db"
KINDS = {"video": "backgrounds", "music": "music", "font": "fonts"}
EXTENSIONS = {
    "video": {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mts"},
    "music": {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".opus"},
    "font": {".ttf", ".otf"},
}
DEFAULTS = {
    "count": 5, "duration_min": 13, "duration_max": 15, "resolution": "1080",
    "crop_mode": "cover", "focus_x": 50, "focus_y": 50,
    "font": "arial", "font_size": 64, "min_font_size": 32, "text_width": 76,
    "text_x": 50, "text_y": 50, "stroke": 4, "text_color": "#ffffff", "stroke_color": "#000000",
    "line_spacing": 1.12, "letter_spacing": 0, "paragraph_spacing": 0, "text_align": "center",
    "text_opacity": 100, "text_rotation": 0, "keep_text_in_frame": True,
    "progress_enabled": False, "progress_text": "В КОНЦЕ — ПРОМО", "progress_width": 80, "progress_y": 6,
    "progress_height": 18, "progress_font_size": 42, "progress_gap": 16, "progress_easing": 2.2,
    "progress_color": "#bce5c9", "progress_track_color": "#202630", "progress_text_color": "#ffffff",
    "export_profile": "hdr10",
    "encoder_mode": "auto", "parallel_jobs": 2,
    "music_volume": 60, "original_volume": 0, "fade": 0.35,
    "loop_music": False, "allow_repeats": False,
    "description": "шоп в тг @store", "texts": [], "hashtags": [],
    "output_dir": str(ROOT / "output" / "videos"),
}
CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def process_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def acquire_instance_lock():
    handle = (ROOT / "data/studio.lock").open("a+b")
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("Studio уже запущена. Откройте http://127.0.0.1:8765")
    return handle


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def database():
    connection = sqlite3.connect(DB, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=20000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize():
    for folder in ("data", "logs", "output/previews", "output/temp", "output/videos", *["assets/" + x for x in KINDS.values()]):
        (ROOT / folder).mkdir(parents=True, exist_ok=True)
    with database() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS assets (
                id TEXT PRIMARY KEY, kind TEXT, name TEXT, path TEXT, info TEXT,
                created TEXT, managed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY, name TEXT, settings TEXT, warnings TEXT, folder TEXT, created TEXT
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, batch_id TEXT, ordinal INTEGER, spec TEXT,
                status TEXT, progress REAL DEFAULT 0, error TEXT DEFAULT '',
                cancel_requested INTEGER DEFAULT 0, output TEXT DEFAULT '',
                preview TEXT DEFAULT '', created TEXT, updated TEXT
            );
            CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created);
        """)


def get_meta(key, default=None):
    with database() as db:
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_meta(key, value):
    with database() as db:
        db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, ensure_ascii=False)))


def settings():
    return {**DEFAULTS, **get_meta("settings", {}), "export_profile": "hdr10"}


def clean_settings(data):
    result = {**settings(), **{key: value for key, value in data.items() if key in DEFAULTS}}
    ranges = {
        "count": (1, 100, int), "duration_min": (1, 120, float), "duration_max": (1, 120, float),
        "focus_x": (0, 100, float), "focus_y": (0, 100, float),
        "font_size": (12, 240, int), "min_font_size": (10, 240, int),
        "text_width": (5, 100, float), "text_x": (0, 100, float), "text_y": (0, 100, float), "stroke": (0, 20, int),
        "line_spacing": (.5, 3, float), "letter_spacing": (-6, 40, float), "paragraph_spacing": (0, 120, float),
        "text_opacity": (0, 100, float), "text_rotation": (-180, 180, float),
        "progress_width": (20, 94, float), "progress_y": (0, 40, float), "progress_height": (6, 50, int),
        "progress_font_size": (18, 100, int), "progress_gap": (4, 80, int), "progress_easing": (1.2, 5, float),
        "music_volume": (0, 150, float), "original_volume": (0, 150, float), "fade": (0, 3, float),
        "parallel_jobs": (1, 3, int),
    }
    for key, (low, high, cast) in ranges.items():
        try:
            value = cast(result[key])
        except (ValueError, TypeError, OverflowError):
            raise ValueError(f"Некорректное значение настройки: {key}.")
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"Настройка {key} должна быть от {low} до {high}.")
        result[key] = value
    if result["duration_min"] > result["duration_max"]:
        raise ValueError("Минимальная длительность больше максимальной.")
    if result["min_font_size"] > result["font_size"]:
        raise ValueError("Минимальный шрифт больше выбранного размера.")
    if result["resolution"] not in ("1080", "720") or result["crop_mode"] not in ("cover", "contain"):
        raise ValueError("Неизвестный формат кадра.")
    if result["encoder_mode"] not in ("auto", "nvidia", "cpu"):
        raise ValueError("Выберите кодирование: Авто, NVIDIA или CPU.")
    if result["text_align"] not in ("left", "center", "right"):
        raise ValueError("Выберите выравнивание текста: слева, по центру или справа.")
    for key in ("text_color", "stroke_color", "progress_color", "progress_track_color", "progress_text_color"):
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(result[key])):
            raise ValueError("Укажите цвет в формате #RRGGBB.")
    if not isinstance(result["texts"], list) or not isinstance(result["hashtags"], list):
        raise ValueError("Тексты и теги должны быть списками.")
    result["texts"] = [str(x).strip() for x in result["texts"] if str(x).strip()]
    if len(result["texts"]) > 2000 or any(len(x) > 5000 for x in result["texts"]):
        raise ValueError("Слишком много текстов или слишком длинная запись.")
    tags = []
    for item in result["hashtags"]:
        for tag in re.split(r"[\s,;]+", str(item)):
            tag = tag.strip().lstrip("#").casefold()
            if tag and tag not in tags:
                if not re.fullmatch(r"[\w]+", tag):
                    raise ValueError(f"В хештеге «{tag}» есть недопустимые символы.")
                tags.append(tag)
    result["hashtags"] = tags
    result["description"] = str(result["description"]).strip()[:4000]
    result["output_dir"] = str(Path(str(result["output_dir"])).expanduser().resolve())
    result["progress_text"] = str(result["progress_text"]).strip()
    if len(result["progress_text"]) > 200:
        raise ValueError("Подпись под шкалой должна быть не длиннее 200 символов.")
    for key in ("loop_music", "allow_repeats", "keep_text_in_frame", "progress_enabled"):
        result[key] = bool(result[key])
    result["export_profile"] = "hdr10"
    font_path(result["font"])
    return result


def executable(name):
    local = ROOT / "tools" / "bin" / (name + (".exe" if os.name == "nt" else ""))
    return str(local) if local.exists() else shutil.which(name)


def require_tools():
    if not all(executable(name) for name in ("ffmpeg", "ffprobe")):
        raise ValueError("FFmpeg не установлен. Закройте программу и запустите «Установить.bat».")


def run(command, timeout=90):
    process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=CREATE_FLAGS)
    if process.returncode:
        raise ValueError("Не удалось прочитать или обработать файл. " + process.stderr[-1800:])
    return process.stdout


def probe(path):
    require_tools()
    data = json.loads(run([executable("ffprobe"), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]))
    streams = data.get("streams", [])
    video = next((x for x in streams if x.get("codec_type") == "video" and not x.get("disposition", {}).get("attached_pic")), None)
    audio = next((x for x in streams if x.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or (video or audio or {}).get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Не удалось определить длительность файла.")
    return {
        "duration": duration, "width": (video or {}).get("width"), "height": (video or {}).get("height"),
        "video": bool(video), "audio": bool(audio), "size": Path(path).stat().st_size,
        "hdr": (video or {}).get("color_transfer") in ("smpte2084", "arib-std-b67"),
        "color_primaries": (video or {}).get("color_primaries"),
        "color_transfer": (video or {}).get("color_transfer"),
        "color_space": (video or {}).get("color_space"),
        "color_range": (video or {}).get("color_range"),
        "pix_fmt": (video or {}).get("pix_fmt"), "codec": (video or {}).get("codec_name"),
        "profile": (video or {}).get("profile"),
    }


def add_asset(path, kind, name=None, managed=False):
    path = Path(path).resolve()
    if kind not in KINDS or path.suffix.lower() not in EXTENSIONS[kind]:
        raise ValueError("Неподдерживаемый тип файла.")
    if not path.is_file():
        raise ValueError("Файл по этому пути не найден.")
    if kind == "font":
        ImageFont.truetype(str(path), 32)
        info = {"size": path.stat().st_size}
    else:
        info = probe(path)
        if not info[kind if kind == "video" else "audio"]:
            raise ValueError("В файле нет нужной видео- или звуковой дорожки.")
    with database() as db:
        existing = db.execute("SELECT id FROM assets WHERE path=? AND kind=?", (str(path), kind)).fetchone()
        if existing:
            db.execute("UPDATE assets SET info=? WHERE id=?", (json.dumps(info), existing[0]))
            return existing[0]
        asset_id = uuid.uuid4().hex
        db.execute("INSERT INTO assets VALUES (?,?,?,?,?,?,?)", (asset_id, kind, name or path.name, str(path), json.dumps(info), now(), int(managed)))
    return asset_id


def assets():
    with database() as db:
        rows = db.execute("SELECT * FROM assets ORDER BY created DESC").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["info"] = json.loads(item["info"])
        item["exists"] = Path(item["path"]).is_file()
        result.append(item)
    return result


def font_path(value):
    builtins = {
        "arial": Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/arialbd.ttf",
        "segoe": Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/segoeuib.ttf",
        "impact": Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/impact.ttf",
    }
    if value in builtins:
        path = builtins[value]
    else:
        with database() as db:
            item = db.execute("SELECT path FROM assets WHERE id=? AND kind='font'", (str(value),)).fetchone()
        if not item:
            raise ValueError("Выбранный шрифт не найден.")
        path = Path(item[0])
    if not path.is_file():
        raise ValueError("Файл шрифта недоступен. Выберите другой шрифт.")
    return str(path)


def make_overlay(text, config, target):
    config = {**DEFAULTS, **config}
    if not config.get("font_path"):
        config["font_path"] = font_path(config["font"])
    return typography.render_overlay(text, config, target)


def color_inputs(info):
    """Use measured color tags; untagged ordinary video is treated as BT.709."""
    hdr = info.get("hdr", False)
    def known(key, fallback):
        return info.get(key) if info.get(key) not in (None, "unknown", "unspecified", "reserved") else fallback
    primaries = known("color_primaries", "bt2020" if hdr else "bt709")
    transfer = known("color_transfer", "smpte2084" if hdr else "bt709")
    matrix = known("color_space", "gbr" if str(info.get("pix_fmt", "")).startswith(("rgb", "bgr", "gbr")) else "bt2020nc" if hdr else "bt709")
    input_range = "full" if info.get("color_range") == "pc" or matrix == "gbr" else "limited"
    return f"pin={primaries}:tin={transfer}:min={matrix}:rin={input_range}"


def preview_color_filter(info):
    if info.get("hdr"):
        return f"zscale={color_inputs(info)}:t=linear:npl=203,format=gbrpf32le,tonemap=hable:desat=0:peak=5,zscale=p=bt709:t=bt709:m=bt709:r=limited,format=yuv420p,"
    return ""


def hdr_video_graph(spec, overlay_input=2, progress_inputs=None):
    """Keep the background at >=10 bits; map sRGB text white to 203 cd/m².

    The overlay is blended in 10-bit PQ, never through an 8-bit SDR background.
    Both source PQ and output PQ use the same absolute luminance normalization.
    """
    info = spec["background"]["info"]
    base = (f"zscale={color_inputs(info)}:p=bt2020:t=linear:m=gbr:r=full:npl=203,format=gbrpf32le,"
            "zscale=pin=bt2020:tin=linear:min=gbr:rin=full:p=bt2020:t=smpte2084:m=bt2020nc:r=limited:npl=203,format=yuv444p10le,")
    text = ("format=gbrap16le,zscale=pin=bt709:tin=iec61966-2-1:min=gbr:rin=full:p=bt2020:t=linear:m=gbr:r=full,format=gbrapf32le,"
            "zscale=pin=bt2020:tin=linear:min=gbr:rin=full:p=bt2020:t=smpte2084:m=bt2020nc:r=limited:npl=203,format=yuva444p10le")
    graph = (f"[0:v:0]{base}{shape_filter(spec)}[base];"
            f"[{overlay_input}:v:0]{text}[text];"
            "[base][text]overlay=0:0:shortest=1:format=yuv444p10:alpha=straight[main]")
    if progress_inputs:
        base_input, fill_input = progress_inputs
        geometry = spec["progress_assets"]["geometry"]
        animated = progress_overlay.alpha_filter(geometry["width"], spec["duration"], spec["settings"]["progress_easing"])
        graph += (f";[{base_input}:v]format=rgba[hookbase];[{fill_input}:v]{animated}[hookfill];"
                  "[hookbase][hookfill]overlay=0:0:shortest=1:format=auto[hooksrgb];"
                  f"[hooksrgb]{text}[hookpq];[main][hookpq]overlay={geometry['x']}:{geometry['y']}:shortest=1:format=yuv444p10:alpha=straight,format=yuv420p10le[v]")
    else:
        graph += ";[main]format=yuv420p10le[v]"
    return graph


def hdr_encoder_options(info, backend="cpu"):
    if backend == "nvidia":
        return ["-c:v", "hevc_nvenc", "-preset", "p4", "-tune", "hq", "-rc", "vbr",
                "-cq", "20", "-b:v", "0", "-profile:v", "main10", "-pix_fmt", "p010le",
                "-bf", "0", "-extra_sei", "1", "-tag:v", "hvc1",
                "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc", "-color_range", "tv"]
    # The transfer describes absolute luminance, not a cosmetic HDR label.
    # Mastering profile spans the PQ container; unknown content light levels stay unknown.
    maximum = 10000 if info.get("hdr") else 203
    master = f"G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L({maximum * 10000},1)"
    params = ("pools=4:frame-threads=2:hdr10=1:hdr10-opt=1:repeat-headers=1:"
              "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:range=limited:chromaloc=2:"
              f"master-display={master}:max-cll=0,0")
    return ["-c:v", "libx265", "-preset", "fast", "-crf", "20", "-profile:v", "main10",
            "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1", "-x265-params", params,
            "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc", "-color_range", "tv"]


def detect_hardware():
    """Check the installed driver and actual Main10 encoder, once per app start."""
    result = {"name": "", "memory_mb": None, "driver": "", "nvenc": False, "reason": ""}
    try:
        line = run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"], timeout=10).splitlines()[0]
        name, memory, driver = [item.strip() for item in line.split(",")]
        result.update(name=name, memory_mb=int(memory), driver=driver)
    except (OSError, ValueError, subprocess.TimeoutExpired, IndexError):
        pass
    try:
        require_tools()
        run([executable("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             "color=s=320x180:r=30", "-frames:v", "3", "-vf", "format=p010le",
             *hdr_encoder_options({}, "nvidia"), "-f", "null", "-"], timeout=20)
        result["nvenc"] = True
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        result["reason"] = "NVENC HEVC 10 бит недоступен. Сборка будет на CPU."
        result["detail"] = str(exc)[-1500:]
    return result


_hdr_reference_lock = threading.Lock()


def hdr_metadata_reference(spec):
    """NVENC forwards frame HDR side data. Cache one x265 frame to supply it.

    The reference supplies frame properties only; blend opacity 0 keeps every
    content pixel from the actual video, including the overlaid text.
    """
    width = int(spec["settings"]["resolution"])
    height = width * 16 // 9
    info = spec["background"]["info"]
    maximum = 10000 if info.get("hdr") else 203
    folder = ROOT / "data/encoder_cache"
    target = folder / f"hdr10_v1_{width}x{height}_{maximum}.mp4"
    with _hdr_reference_lock:
        if target.is_file():
            return target
        folder.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.stem + "." + uuid.uuid4().hex + ".mp4")
        try:
            run([executable("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                 f"color=s={width}x{height}:r=30", "-frames:v", "1", "-vf", "format=yuv420p10le",
                 *hdr_encoder_options(info), str(temporary)], timeout=90)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return target


def nvenc_video_graph(spec, progress_inputs=None, metadata_input=3):
    graph = hdr_video_graph(spec, progress_inputs=progress_inputs).removesuffix("[v]") + "[content]"
    # FFmpeg also initializes encoder stream side data from the graph's first
    # video source. Declare the HDR reference first, then preserve its frame
    # properties through blend while taking all actual pixels from content.
    return (f"[{metadata_input}:v]setpts=PTS-STARTPTS[metadata];" + graph + ";"
            "[metadata][content]blend=all_mode=normal:all_opacity=0:shortest=1,"
            "format=p010le,setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc:range=limited[v]")


def render_watch_copy(source, target, should_stop=None):
    import tempfile
    target = Path(target)
    temporary = target.with_suffix(".part.mp4")
    command = [executable("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-threads", "2", "-filter_threads", "2", "-i", str(source),
         "-map", "0:v:0", "-map", "0:a:0?", "-vf", preview_color_filter({"hdr": True, "color_primaries": "bt2020", "color_transfer": "smpte2084", "color_space": "bt2020nc"}) + "scale=360:640",
         "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-movflags", "+faststart", str(temporary)]
    process = None
    try:
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=errors, creationflags=CREATE_FLAGS)
            deadline = time.monotonic() + 300
            while process.poll() is None:
                if (should_stop and should_stop()) or time.monotonic() > deadline:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=5)
                    raise InterruptedError("Просмотр остановлен.")
                time.sleep(.2)
            if process.returncode:
                errors.seek(0)
                raise ValueError(errors.read().decode("utf-8", errors="replace")[-1500:])
        os.replace(temporary, target)
    finally:
        if process and process.poll() is None:
            process.kill(); process.wait(timeout=5)
        temporary.unlink(missing_ok=True)


def shape_filter(spec):
    config = spec["settings"]
    width, height = int(config["resolution"]), int(config["resolution"]) * 16 // 9
    if config["crop_mode"] == "cover":
        shape = f"scale={width}:{height}:force_original_aspect_ratio=increase:force_divisible_by=2,crop={width}:{height}:(iw-ow)*{config['focus_x']/100}:(ih-oh)*{config['focus_y']/100}"
    else:
        shape = f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
    # Normalize display aspect before cropping, including anamorphic sources.
    return "scale=trunc(iw*sar/2)*2:ih,setsar=1," + shape + ",setsar=1,fps=30"


def video_filter(spec):
    # Only the viewing copy is tone mapped to SDR.
    return preview_color_filter(spec["background"]["info"]) + shape_filter(spec)


def render_preview(spec, overlay, target):
    require_tools()
    run([executable("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-ss", str(spec["start"]),
         "-i", spec["background"]["path"], "-i", str(overlay), "-filter_complex",
         f"[0:v:0]{video_filter(spec)}[base];[base][1:v:0]overlay=0:0,scale=360:640[v]",
         "-map", "[v]", "-frames:v", "1", "-q:v", "3", str(target)])
    if spec["settings"].get("progress_enabled"):
        spec.setdefault("overlay", str(overlay))
        assets = ensure_progress_assets(spec)
        geometry = assets["geometry"]
        with Image.open(target) as source:
            image = source.convert("RGBA")
        progress = progress_overlay.frame(assets)
        size = (max(1,round(progress.width*360/geometry["frame_width"])),max(1,round(progress.height*640/geometry["frame_height"])))
        image.alpha_composite(progress.resize(size, Image.Resampling.LANCZOS),
                              (round(geometry["x"]*360/geometry["frame_width"]),round(geometry["y"]*640/geometry["frame_height"])))
        image.convert("RGB").save(target, quality=92)


def ensure_progress_assets(spec):
    config = {**DEFAULTS, **spec["settings"]}
    if not config["progress_enabled"]:
        return None
    assets = spec.get("progress_assets")
    if assets and Path(assets["base"]).is_file() and Path(assets["fill"]).is_file():
        return assets
    if not config.get("font_path"):
        config["font_path"] = font_path(config["font"])
    prefix = Path(spec["overlay"]).with_suffix("").with_name(Path(spec["overlay"]).stem+"_progress")
    spec["progress_assets"] = progress_overlay.save(config, prefix)
    return spec["progress_assets"]


def available_gaps(duration, occupied):
    cursor = 0
    gaps = []
    for start, end in sorted(occupied):
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        gaps.append((cursor, duration))
    return gaps


def select_assets(ids, kind):
    available = [a for a in assets() if a["kind"] == kind and a["exists"]]
    if ids is not None:
        available = [a for a in available if a["id"] in ids]
    for item in available:
        # Refresh metadata: a path can point to a file replaced since import.
        item["info"] = probe(item["path"])
        if not item["info"][kind if kind == "video" else "audio"]:
            raise ValueError("Материал больше не содержит нужной дорожки: " + item["name"])
    return available


def prepare_batch(payload):
    require_tools()
    config = clean_settings(payload.get("settings", {}))
    videos = select_assets(payload.get("video_ids"), "video")
    music = select_assets(payload.get("music_ids"), "music")
    if not videos:
        raise ValueError("Добавьте и выберите хотя бы одно фоновое видео.")
    if not music:
        raise ValueError("Добавьте и выберите хотя бы один музыкальный файл.")
    if not config["texts"]:
        raise ValueError("Добавьте рекламные тексты.")
    if len(config["hashtags"]) < 5:
        raise ValueError("Нужно не менее пяти разных хештегов.")
    output_root = Path(config["output_dir"])
    output_root.mkdir(parents=True, exist_ok=True)
    estimated = config["count"] * config["duration_max"] * (900_000 if config["resolution"] == "1080" else 500_000)
    if shutil.disk_usage(output_root).free < estimated + 200_000_000:
        raise ValueError("Недостаточно свободного места для партии. Освободите диск или уменьшите число роликов.")
    config["font_path"] = font_path(config["font"])
    batch_id = uuid.uuid4().hex
    name = str(payload.get("name", "")).strip()[:120] or "Партия " + datetime.now().strftime("%d.%m · %H:%M")
    folder = output_root / (datetime.now().strftime("%Y-%m-%d_%H-%M-%S_") + batch_id[:6])
    folder.mkdir()
    warnings = []
    if len(config["texts"]) < config["count"]:
        warnings.append("Текстов меньше, чем роликов: некоторые надписи повторяются.")
    historical = {item["id"]: [] for item in videos}
    with database() as db:
        previous = db.execute("SELECT spec FROM jobs WHERE status NOT IN ('cancelled','error')").fetchall()
    for row in previous:
        old = json.loads(row[0])
        asset_id = old["background"]["id"]
        if asset_id in historical:
            historical[asset_id].append((old["start"], old["start"] + old["duration"]))
    current = {item["id"]: [] for item in videos}
    texts, prepared = [], []
    try:
        for index in range(config["count"]):
            duration = round(random.uniform(config["duration_min"], config["duration_max"]), 3)
            candidates, history_reused = [], False
            for respect_history in (True, False):
                for video in videos:
                    occupied = current[video["id"]] + (historical[video["id"]] if respect_history else [])
                    gaps = available_gaps(video["info"]["duration"], occupied)
                    candidates.extend((video, start, end) for start, end in gaps if end - start >= duration + .05)
                if candidates:
                    history_reused = not respect_history
                    break
            if not candidates and config["allow_repeats"]:
                candidates = [(v, 0, v["info"]["duration"]) for v in videos if v["info"]["duration"] >= duration + .05]
                warnings.append("Разрешён повтор отрезков внутри партии: свободных фрагментов недостаточно.")
            if not candidates:
                raise ValueError("Не хватает непересекающихся фрагментов для партии. Уменьшите число/длительность роликов, добавьте исходник или разрешите повтор отрезков.")
            if history_reused:
                warnings.append("Некоторые фрагменты уже использовались в ранее подготовленных партиях.")
            background, start_min, start_max = random.choice(candidates)
            start = round(random.uniform(start_min, start_max - duration - .01), 4)
            current[background["id"]].append((start, start + duration))
            if not texts:
                texts = random.sample(config["texts"], len(config["texts"]))
            text = texts.pop()
            suitable = [track for track in music if config["loop_music"] or track["info"]["duration"] >= duration]
            if not suitable:
                raise ValueError("Музыка короче ролика. Выберите более длинную дорожку или включите повтор короткой музыки.")
            track = random.choice(suitable)
            music_start = round(random.uniform(0, max(0, track["info"]["duration"] - duration)), 3)
            tags = ["#" + x for x in random.sample(config["hashtags"], 5)]
            caption = (config["description"] + "\n\n" + " ".join(tags)).strip()
            job_id = uuid.uuid4().hex
            overlay = ROOT / "output/temp" / (job_id + ".png")
            preview = ROOT / "output/previews" / (job_id + ".jpg")
            text_layout = make_overlay(text, config, overlay)
            spec = {"background": background, "start": start, "duration": duration, "text": text,
                    "music": track, "music_start": music_start, "caption": caption,
                    "settings": config, "overlay": str(overlay), "text_layout": text_layout,
                    "selection": {"video_ids": [v["id"] for v in videos], "music_ids": [m["id"] for m in music]}}
            render_preview(spec, overlay, preview)
            output = folder / f"{index + 1:02d}_video.mp4"
            prepared.append({"id": job_id, "ordinal": index + 1, "spec": spec, "preview": str(preview), "output": str(output)})
        with database() as db:
            db.execute("INSERT INTO batches VALUES (?,?,?,?,?,?)", (batch_id, name, json.dumps(config, ensure_ascii=False), json.dumps(list(dict.fromkeys(warnings)), ensure_ascii=False), str(folder), now()))
            for item in prepared:
                db.execute("INSERT INTO jobs (id,batch_id,ordinal,spec,status,output,preview,created,updated) VALUES (?,?,?,?,?,?,?,?,?)",
                           (item["id"], batch_id, item["ordinal"], json.dumps(item["spec"], ensure_ascii=False), "ready", item["output"], item["preview"], now(), now()))
        write_manifest(batch_id)
    except Exception:
        # Only remove this newly allocated party, never any user material.
        for item in prepared:
            Path(item["preview"]).unlink(missing_ok=True)
            Path(item["spec"]["overlay"]).unlink(missing_ok=True)
        try:
            folder.rmdir()
        except OSError:
            pass
        raise
    return batch_id


def job_dict(row):
    value = dict(row)
    value["spec"] = json.loads(value["spec"])
    value["available"] = value["status"] == "done" and Path(value["output"]).is_file()
    value["hdr"] = value["spec"]["settings"].get("export_profile") == "hdr10"
    value["playback_available"] = (ROOT / "output/previews" / (value["id"] + "_watch.mp4")).is_file()
    return value


def batches():
    with database() as db:
        rows = db.execute("SELECT * FROM batches ORDER BY created DESC").fetchall()
        jobs = db.execute("SELECT * FROM jobs ORDER BY ordinal").fetchall()
    grouped = {}
    for row in jobs:
        grouped.setdefault(row["batch_id"], []).append(job_dict(row))
    result = []
    for row in rows:
        item = dict(row)
        item["settings"] = json.loads(item["settings"])
        item["warnings"] = json.loads(item["warnings"])
        item["jobs"] = grouped.get(item["id"], [])
        result.append(item)
    return result


def write_manifest(batch_id):
    with database() as db:
        # Serialize snapshot + file replacement across app and parallel workers.
        db.execute("BEGIN IMMEDIATE")
        batch = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        rows = db.execute("SELECT * FROM jobs WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall()
        if not batch:
            return
        manifest = {"name": batch["name"], "created": batch["created"], "jobs": []}
        for row in rows:
            value = job_dict(row)
            manifest["jobs"].append({"number": value["ordinal"], "status": value["status"], "file": Path(value["output"]).name, "error": value["error"], **value["spec"]})
            if value["status"] == "done":
                Path(value["output"]).with_suffix(".txt").write_text(value["spec"]["caption"], encoding="utf-8")
        target = Path(batch["folder"]) / "manifest.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
