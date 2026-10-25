from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import hashlib
import json
import logging
import os
import signal
import shutil
import subprocess
import re
import sys
import threading
import time
import uuid
import zipfile

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import core
import publishing
from publishing_routes import router as publishing_router

worker = None
preparation_lock = threading.Lock()
preview_lock = threading.Lock()
clip_lock = threading.Lock()
layer_lock = threading.Lock()
publisher = publishing.Publisher()


class OAuthLogFilter(logging.Filter):
    def filter(self, record):
        if record.args and isinstance(record.args, tuple) and len(record.args) >= 3:
            values = list(record.args)
            if str(values[2]).startswith("/api/publishing/oauth/callback"):
                values[2] = "/api/publishing/oauth/callback"
                record.args = tuple(values)
        return True


@asynccontextmanager
async def lifespan(app):
    global worker
    core.initialize()
    publishing.initialize()
    logging.getLogger("uvicorn.access").addFilter(OAuthLogFilter())
    instance_guard = core.acquire_instance_lock()
    logging.basicConfig(filename=core.ROOT / "logs/server.log", encoding="utf-8", level=logging.INFO)
    # Let a surviving worker finish stopping before recovering its jobs.
    previous_worker = core.get_meta("worker_pid")
    if core.process_alive(previous_worker):
        core.set_meta("worker_stop", True)
        for _ in range(40):
            if not core.process_alive(previous_worker):
                break
            await asyncio.sleep(.25)
        if core.process_alive(previous_worker):
            instance_guard.close()
            raise RuntimeError("Предыдущая сборка ещё останавливается. Повторите запуск через несколько секунд.")
    with core.database() as db:
        interrupted_batches = [row[0] for row in db.execute("SELECT DISTINCT batch_id FROM jobs WHERE status IN ('rendering','queued')")]
        db.execute("UPDATE jobs SET status='interrupted',progress=0,error='Сборка прервана. Нажмите «Повторить» или «Возобновить».',updated=? WHERE status IN ('rendering','queued')", (core.now(),))
    for batch_id in interrupted_batches:
        core.write_manifest(batch_id)
    core.set_meta("worker_stop", False)
    core.set_meta("hardware", await run_in_threadpool(core.detect_hardware))
    worker = subprocess.Popen([sys.executable, str(core.ROOT / "worker.py"), str(os.getpid())], cwd=core.ROOT, creationflags=core.CREATE_FLAGS)
    core.set_meta("worker_pid", worker.pid)
    publisher.start()
    try:
        yield
    finally:
        await run_in_threadpool(publisher.close)
        core.set_meta("worker_stop", True)
        for _ in range(48):
            if worker.poll() is not None:
                break
            await asyncio.sleep(.25)
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=5)
        instance_guard.close()


app = FastAPI(title="TikTok Store Studio", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=core.ROOT / "static"), name="static")
app.include_router(publishing_router)


@app.middleware("http")
async def local_only(request: Request, call_next):
    # Restrict the local service to its own origin, including file imports.
    host = request.headers.get("host", "").split(":")[0].lower()
    # Allow any origin on loopback/preview hosts (e.g. sandboxed ports) while keeping the panel local-only.
    if not re.fullmatch(r"(127(\.0){2}\.1|localhost|\[?::1\]?|0\.0\.0\.0|\d{1,3}(\.\d{1,3}){3})", host):
        return JSONResponse({"detail": "Недопустимый адрес панели."}, status_code=403)
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin not in (f"http://{request.headers.get('host')}", f"https://{request.headers.get('host')}"):
        return JSONResponse({"detail": "Откройте действие из панели Studio."}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.exception_handler(ValueError)
async def bad_request(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(Exception)
async def unexpected_error(request, exc):
    logging.exception("Request failed: %s", request.url.path, exc_info=exc)
    return JSONResponse({"detail": "Не удалось выполнить действие. Подробности сохранены в logs/server.log."}, status_code=500)


@app.get("/")
def index():
    return FileResponse(core.ROOT / "templates/index.html")


@app.get("/api/state")
def state():
    config = core.settings()
    directory = Path(config["output_dir"])
    while not directory.exists() and directory.parent != directory:
        directory = directory.parent
    return {"settings": config, "assets": core.assets(), "batches": core.batches(),
            "presets": core.get_meta("presets", {}), "paused": core.get_meta("paused", False),
            "system": {"ffmpeg": bool(core.executable("ffmpeg")), "ffprobe": bool(core.executable("ffprobe")),
                       "hardware": core.get_meta("hardware", {}),
                       "free_gb": round(shutil.disk_usage(directory).free / 1024**3, 1),
                       "worker": worker is not None and worker.poll() is None}}


@app.post("/api/settings")
def save_settings(payload: dict):
    value = core.clean_settings(payload)
    core.set_meta("settings", value)
    return value


@app.post("/api/presets")
def save_preset(payload: dict):
    name = str(payload.get("name", "")).strip()[:60]
    if not name:
        raise ValueError("Введите название пресета.")
    config = core.clean_settings(payload.get("settings", {}))
    # Materials, output folder and hardware settings stay local to this workspace.
    preset = {key: value for key, value in config.items() if key not in ("texts", "hashtags", "description", "output_dir", "encoder_mode", "parallel_jobs")}
    presets = core.get_meta("presets", {})
    presets[name] = preset
    core.set_meta("presets", presets)
    return {"ok": True}


@app.post("/api/assets/upload")
def upload(kind: str = Form(...), files: list[UploadFile] = File(...)):
    if kind not in core.KINDS:
        raise ValueError("Неизвестный тип материала.")
    imported, errors = [], []
    for file in files:
        filename = Path(file.filename or "file").name
        suffix = Path(filename).suffix.lower()
        if suffix not in core.EXTENSIONS[kind]:
            errors.append(filename + ": неподдерживаемое расширение.")
            continue
        target = core.ROOT / "assets" / core.KINDS[kind] / (uuid.uuid4().hex + suffix)
        try:
            with target.open("wb") as stream:
                shutil.copyfileobj(file.file, stream, 1024 * 1024)
            imported.append(core.add_asset(target, kind, filename, managed=True))
        except Exception as exc:
            target.unlink(missing_ok=True)
            errors.append(filename + ": " + str(exc))
        finally:
            file.file.close()
    return {"imported": imported, "errors": errors}


@app.post("/api/assets/path")
def import_path(payload: dict):
    return {"id": core.add_asset(str(payload.get("path", "")), str(payload.get("kind", "video")))}


@app.delete("/api/assets/{asset_id}")
def remove_asset(asset_id: str):
    with core.database() as db:
        db.execute("DELETE FROM assets WHERE id=?", (asset_id,))
    return {"ok": True}


@app.get("/api/assets/{asset_id}/media")
def asset_media(asset_id: str):
    with core.database() as db:
        asset = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if not asset or asset["kind"] == "font" or not Path(asset["path"]).is_file():
        raise HTTPException(404, "Материал не найден.")
    return FileResponse(asset["path"])


@app.post("/api/preview/layer")
def preview_layer(payload: dict):
    config = core.clean_settings(payload.get("settings", {}))
    text = str(payload.get("text", "")).strip() or "Твой текст\nв твоём стиле"
    if len(text) > 5000:
        raise ValueError("Текст предпросмотра слишком длинный.")
    config["font_path"] = core.font_path(config["font"])
    style_keys = core.typography.STYLE_KEYS
    identity = {key: config[key] for key in style_keys}
    identity.update(text=text, font_mtime=Path(config["font_path"]).stat().st_mtime_ns, version=3)
    key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    target = core.ROOT / "output/previews" / ("layer_" + key + ".png")
    layout_file = target.with_suffix(".json")
    with layer_lock:
        if target.is_file() and layout_file.is_file():
            layout = json.loads(layout_file.read_text(encoding="utf-8"))
        else:
            layout = core.make_overlay(text, config, target)
            layout_file.write_text(json.dumps(layout), encoding="utf-8")
    return {"url": "/api/preview-layers/" + key, "layout": layout, "sample": not bool(str(payload.get("text", "")).strip())}


@app.get("/api/preview-layers/{key}")
def layer_file(key: str):
    import re
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise HTTPException(404)
    target = core.ROOT / "output/previews" / ("layer_" + key + ".png")
    if not target.is_file():
        raise HTTPException(404)
    return FileResponse(target, media_type="image/png")


@app.post("/api/preview/progress")
def preview_progress(payload: dict):
    config = core.clean_settings(payload.get("settings", {}))
    if not config["progress_enabled"]:
        return {"enabled": False}
    config["font_path"] = core.font_path(config["font"])
    identity = {key: config[key] for key in core.progress_overlay.STYLE_KEYS}
    identity.update(font_mtime=Path(config["font_path"]).stat().st_mtime_ns, version=1)
    key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    prefix = core.ROOT / "output/previews" / ("meter_"+key)
    metadata = prefix.with_suffix(".json")
    with layer_lock:
        if metadata.is_file() and prefix.with_name(prefix.name+"_base.png").is_file() and prefix.with_name(prefix.name+"_fill.png").is_file():
            geometry = json.loads(metadata.read_text(encoding="utf-8"))
        else:
            assets = core.progress_overlay.save(config, prefix)
            geometry = assets["geometry"]
            metadata.write_text(json.dumps(geometry), encoding="utf-8")
    return {"enabled": True, "geometry": geometry, "base_url": f"/api/preview-progress/{key}/base", "fill_url": f"/api/preview-progress/{key}/fill"}


@app.get("/api/preview-progress/{key}/{kind}")
def preview_progress_file(key: str, kind: str):
    import re
    if not re.fullmatch(r"[a-f0-9]{64}", key) or kind not in ("base", "fill"):
        raise HTTPException(404)
    path = core.ROOT / "output/previews" / f"meter_{key}_{kind}.png"
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png")


@app.post("/api/preview/clip")
def preview_clip(payload: dict):
    core.require_tools()
    with core.database() as db:
        row = db.execute("SELECT * FROM assets WHERE id=? AND kind='video'", (str(payload.get("asset_id", "")),)).fetchone()
    if not row or not Path(row["path"]).is_file():
        raise ValueError("Выберите доступное фоновое видео.")
    info = core.probe(row["path"])
    try:
        start = float(payload.get("start", 0))
    except (ValueError, TypeError):
        raise ValueError("Укажите время начала предпросмотра в секундах.")
    import math
    if not math.isfinite(start):
        raise ValueError("Некорректное время начала.")
    start = round(max(0, min(start, info["duration"] - .05)), 2)
    length = min(8, info["duration"] - start)
    identity = {"id": row["id"], "mtime": Path(row["path"]).stat().st_mtime_ns, "start": start, "length": length, "version": 2}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    target = core.ROOT / "output/previews" / ("clip_" + key + ".mp4")
    poster = target.with_suffix(".jpg")
    with clip_lock:
        if not target.is_file():
            temporary = target.with_suffix(".part.mp4")
            try:
                filters = core.preview_color_filter(info) + "scale=trunc(iw*sar/2)*2:ih,setsar=1,scale=960:960:force_original_aspect_ratio=decrease:force_divisible_by=2,fps=30"
                core.run([core.executable("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start), "-i", row["path"],
                          "-t", str(length), "-map", "0:v:0", "-an", "-vf", filters,
                          "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p",
                          "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-movflags", "+faststart", str(temporary)])
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        if not poster.is_file():
            core.run([core.executable("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-i", str(target), "-frames:v", "1", str(poster)])
    return {"url": "/api/preview-clips/" + key, "poster": "/api/preview-clips/" + key + "/poster",
            "start": start, "duration": length, "source_duration": info["duration"]}


@app.get("/api/preview-clips/{key}")
@app.get("/api/preview-clips/{key}/poster")
def clip_file(key: str, request: Request):
    import re
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise HTTPException(404)
    is_poster = request.url.path.endswith("/poster")
    target = core.ROOT / "output/previews" / ("clip_" + key + (".jpg" if is_poster else ".mp4"))
    if not target.is_file():
        raise HTTPException(404)
    return FileResponse(target, media_type="image/jpeg" if is_poster else "video/mp4")


@app.post("/api/preview")
def preview(payload: dict):
    if not preview_lock.acquire(blocking=False):
        raise ValueError("Предпросмотр уже обновляется. Дождитесь результата.")
    try:
        core.require_tools()
        config = core.clean_settings(payload.get("settings", {}))
        videos = core.select_assets(payload.get("video_ids"), "video")
        if not videos or not config["texts"]:
            raise ValueError("Для предпросмотра выберите видео и добавьте хотя бы один текст.")
        config["font_path"] = core.font_path(config["font"])
        spec = {"background": videos[0], "start": min(1, videos[0]["info"]["duration"] / 4), "settings": config}
        key = hashlib.sha256(json.dumps({"spec": spec, "text": config["texts"][0]}, sort_keys=True).encode()).hexdigest()
        target = core.ROOT / "output/previews" / ("live_" + key + ".jpg")
        overlay = core.ROOT / "output/temp" / ("live_" + key + ".png")
        if not target.exists():
            core.make_overlay(config["texts"][0], config, overlay)
            try:
                core.render_preview(spec, overlay, target)
            finally:
                overlay.unlink(missing_ok=True)
        return {"url": "/api/previews/live_" + key}
    finally:
        preview_lock.release()


@app.get("/api/previews/{key}")
def preview_file(key: str):
    if not all(char in "0123456789abcdef_live" for char in key) or len(key) > 80:
        raise HTTPException(404)
    target = core.ROOT / "output/previews" / (key + ".jpg")
    if not target.is_file():
        raise HTTPException(404)
    return FileResponse(target, media_type="image/jpeg")


@app.post("/api/batches")
def prepare(payload: dict):
    if not preparation_lock.acquire(blocking=False):
        raise ValueError("Другая партия ещё подготавливается.")
    try:
        return {"id": core.prepare_batch(payload)}
    finally:
        preparation_lock.release()


@app.post("/api/batches/{batch_id}/start")
def start(batch_id: str):
    if worker is None or worker.poll() is not None:
        raise ValueError("Процесс сборки остановлен. Перезапустите Studio.")
    with core.database() as db:
        if not db.execute("SELECT id FROM batches WHERE id=?", (batch_id,)).fetchone():
            raise HTTPException(404)
        db.execute("UPDATE jobs SET status='queued',cancel_requested=0,error='',progress=0,updated=? WHERE batch_id=? AND status IN ('ready','interrupted','error')", (core.now(), batch_id))
    core.set_meta("paused", False)
    return {"ok": True}


@app.post("/api/queue/{action}")
def queue(action: str):
    if action not in ("pause", "resume"):
        raise HTTPException(404)
    core.set_meta("paused", action == "pause")
    return {"ok": True}


def get_job(job_id):
    with core.database() as db:
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        raise HTTPException(404, "Задание не найдено.")
    return core.job_dict(job)


@app.post("/api/jobs/{job_id}/{action}")
def job_action(job_id: str, action: str, payload: dict = {}):
    if action not in ("cancel", "retry", "edit"):
        raise HTTPException(404)
    with preparation_lock:
        with core.database() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise HTTPException(404)
            job = core.job_dict(row)
            if action == "cancel":
                if job["status"] == "rendering":
                    db.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
                elif job["status"] in ("ready", "queued", "interrupted", "error"):
                    db.execute("UPDATE jobs SET status='cancelled',cancel_requested=1,progress=0,updated=? WHERE id=?", (core.now(), job_id))
            elif action == "retry":
                if job["status"] not in ("error", "cancelled", "interrupted"):
                    raise ValueError("Сейчас это задание нельзя повторить.")
                db.execute("UPDATE jobs SET status='queued',cancel_requested=0,progress=0,error='',updated=? WHERE id=?", (core.now(), job_id))
            elif action == "edit":
                if job["status"] != "ready":
                    raise ValueError("Редактировать можно только подготовленное задание до запуска.")
                spec = job["spec"]
                if payload.get("regenerate"):
                    import random
                    config = spec["settings"]
                    others = [json.loads(item["spec"]) for item in db.execute("SELECT spec FROM jobs WHERE batch_id=? AND id!=? AND status!='cancelled'", (job["batch_id"], job_id))]
                    candidates = []
                    selection = spec.get("selection", {})
                    videos = core.select_assets(selection.get("video_ids", [spec["background"]["id"]]), "video")
                    for video in videos:
                        occupied = [(item["start"], item["start"] + item["duration"]) for item in others if item["background"]["id"] == video["id"]]
                        if config["allow_repeats"]:
                            occupied = []
                        candidates += [(video, start, end) for start, end in core.available_gaps(video["info"]["duration"], occupied) if end-start > spec["duration"] + .05]
                    if not candidates:
                        raise ValueError("Не удалось подобрать другой фрагмент без пересечения с остальными роликами.")
                    background, start, end = random.choice(candidates)
                    spec["background"] = background
                    spec["start"] = round(random.uniform(start, end-spec["duration"]-.01), 4)
                    available_texts = [text for text in config["texts"] if text not in [item["text"] for item in others]]
                    spec["text"] = random.choice(available_texts or config["texts"])
                    tracks = core.select_assets(selection.get("music_ids", [spec["music"]["id"]]), "music")
                    tracks = [track for track in tracks if config["loop_music"] or track["info"]["duration"] >= spec["duration"]]
                    if not tracks:
                        raise ValueError("Выбранная музыка недоступна или слишком короткая.")
                    spec["music"] = random.choice(tracks)
                    spec["music_start"] = round(random.uniform(0, max(0, spec["music"]["info"]["duration"] - spec["duration"])), 3)
                    spec["caption"] = (config["description"] + "\n\n" + " ".join("#" + x for x in random.sample(config["hashtags"], 5))).strip()
                if "text" in payload:
                    text = str(payload["text"]).strip()
                    if not text or len(text) > 5000:
                        raise ValueError("Введите текст длиной от 1 до 5000 символов.")
                    spec["text"] = text
                temp_overlay = Path(spec["overlay"]).with_suffix(".edit.png")
                temp_preview = Path(job["preview"]).with_suffix(".edit.jpg")
                try:
                    spec["text_layout"] = core.make_overlay(spec["text"], spec["settings"], temp_overlay)
                    core.render_preview(spec, temp_overlay, temp_preview)
                    os.replace(temp_overlay, spec["overlay"])
                    os.replace(temp_preview, job["preview"])
                    db.execute("UPDATE jobs SET spec=?,updated=? WHERE id=?", (json.dumps(spec, ensure_ascii=False), core.now(), job_id))
                finally:
                    temp_overlay.unlink(missing_ok=True)
                    temp_preview.unlink(missing_ok=True)
        if action == "retry":
            core.set_meta("paused", False)
        core.write_manifest(job["batch_id"])
    return {"ok": True}


@app.get("/api/jobs/{job_id}/video")
def video(job_id: str, download: bool = False):
    job = get_job(job_id)
    if not job["available"]:
        raise HTTPException(404, "Готовый файл недоступен.")
    return FileResponse(job["output"], media_type="video/mp4", filename=Path(job["output"]).name if download else None)


@app.get("/api/jobs/{job_id}/caption")
def caption(job_id: str):
    job = get_job(job_id)
    from fastapi.responses import Response
    return Response(job["spec"]["caption"], media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{job["ordinal"]:02d}_description.txt"'})


@app.get("/api/jobs/{job_id}/playback")
def playback(job_id: str):
    job = get_job(job_id)
    target = core.ROOT / "output/previews" / (job_id + "_watch.mp4")
    if not job["available"] or not target.is_file():
        raise HTTPException(404, "Копия для просмотра пока недоступна. Скачайте оригинал HDR.")
    return FileResponse(target, media_type="video/mp4")


@app.get("/api/batches/{batch_id}/zip")
def batch_zip(batch_id: str):
    with core.database() as db:
        batch = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        jobs = db.execute("SELECT * FROM jobs WHERE batch_id=? AND status='done' ORDER BY ordinal", (batch_id,)).fetchall()
    if not batch:
        raise HTTPException(404)
    if not jobs:
        raise ValueError("В партии пока нет готовых видео.")
    archive = core.ROOT / "output/temp" / ("export_" + uuid.uuid4().hex + ".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
        for job in jobs:
            path = Path(job["output"])
            if path.is_file():
                bundle.write(path, path.name)
                bundle.writestr(path.with_suffix(".txt").name, json.loads(job["spec"])["caption"])
        bundle.writestr("manifest.json", json.dumps({"name": batch["name"], "jobs": [core.job_dict(row) for row in jobs]}, ensure_ascii=False, indent=2))
    from starlette.background import BackgroundTask
    return FileResponse(archive, filename="studio_" + batch_id[:8] + ".zip", background=BackgroundTask(archive.unlink, missing_ok=True))


@app.post("/api/folder")
def open_folder(payload: dict):
    batch_id = payload.get("batch_id")
    if batch_id:
        with core.database() as db:
            batch = db.execute("SELECT folder FROM batches WHERE id=?", (str(batch_id),)).fetchone()
        if not batch:
            raise HTTPException(404)
        folder = Path(batch[0])
    else:
        folder = Path(core.settings()["output_dir"])
    folder.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(folder))
    return {"path": str(folder)}


@app.post("/api/cleanup")
def cleanup():
    if not preparation_lock.acquire(blocking=False):
        raise ValueError("Дождитесь подготовки партии.")
    try:
        with core.database() as db:
            protected = set()
            for row in db.execute("SELECT spec FROM jobs WHERE status IN ('ready','queued','rendering','interrupted','error','cancelled')"):
                spec = json.loads(row[0])
                protected.add(spec["overlay"])
                protected.update(spec.get("progress_assets", {}).get(key, "") for key in ("base", "fill"))
        removed = 0
        for path in (core.ROOT / "output/temp").iterdir():
            if path.is_file() and str(path) not in protected and path.stat().st_mtime < time.time() - 86400:
                path.unlink(missing_ok=True)
                removed += 1
        for path in (core.ROOT / "output/previews").iterdir():
            if path.is_file() and path.name.startswith(("live_", "layer_", "clip_", "meter_")) and path.stat().st_mtime < time.time() - 86400:
                path.unlink(missing_ok=True)
                removed += 1
        return {"removed": removed}
    finally:
        preparation_lock.release()


@app.post("/api/shutdown")
async def shutdown():
    async def stop_later():
        await asyncio.sleep(.5)
        signal.raise_signal(signal.SIGINT)
    asyncio.create_task(stop_later())
    return {"ok": True}
