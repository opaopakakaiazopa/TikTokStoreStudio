from pathlib import Path
import html
import shutil
import uuid

from fastapi import APIRouter, File, Request, UploadFile, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import core
import publishing as pub

router = APIRouter(prefix="/api/publishing")


@router.get("/state")
def state():
    return pub.public_state()


@router.post("/config")
def config(payload: dict):
    return pub.save_configuration(payload)


@router.post("/accounts")
def save_account(payload: dict):
    return pub.save_account(payload)


@router.post("/accounts/{account_id}/connect")
def connect(account_id: str):
    url, cookie = pub.oauth_start(account_id)
    response = JSONResponse({"url": url})
    response.set_cookie("studio_tiktok_oauth", cookie, max_age=600, httponly=True, samesite="lax", path="/api/publishing/oauth/callback")
    return response


@router.get("/oauth/callback")
def callback(request: Request):
    try:
        values = request.query_params
        if values.get("error"):
            raise ValueError("TikTok не предоставил доступ. Повторите подключение из Studio.")
        if not values.get("code") or not values.get("state"):
            raise ValueError("Неполный ответ авторизации TikTok.")
        pub.oauth_finish(values["state"], values["code"], request.cookies.get("studio_tiktok_oauth", ""))
        message = "Аккаунт TikTok подключён. Вернитесь в Studio."
        code = 200
    except ValueError as exc:
        message, code = str(exc), 400
    response = HTMLResponse("<!doctype html><html lang='ru'><meta charset='utf-8'><title>Store Studio</title>"
        "<body style='font:18px system-ui;background:#101216;color:#eee;padding:48px;max-width:700px'>"
        f"<h1>Подключение TikTok</h1><p>{html.escape(message)}</p><a style='color:#b6a1ff' href='/#publishing'>Вернуться в Studio</a></body></html>", status_code=code)
    response.delete_cookie("studio_tiktok_oauth", path="/api/publishing/oauth/callback")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/accounts/{account_id}/refresh")
def refresh(account_id: str):
    return pub.refresh_account(account_id)


@router.post("/accounts/{account_id}/disconnect")
def disconnect(account_id: str):
    return pub.disconnect(account_id)


@router.delete("/accounts/{account_id}")
def delete_account(account_id: str):
    with pub.LOCK, core.database() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM publishing_tasks WHERE account_id=?", (account_id,)).fetchone():
            raise ValueError("У аккаунта есть история отправок. Его можно отключить, сохранив историю.")
        db.execute("DELETE FROM publishing_oauth WHERE account_id=?", (account_id,))
        db.execute("DELETE FROM publishing_accounts WHERE id=?", (account_id,))
    return {"ok": True}


@router.post("/library/sync")
def sync():
    with pub.LOCK:
        return pub.sync_generated()


@router.post("/library/upload")
def upload(files: list[UploadFile] = File(...)):
    if len(files) > 100:
        raise ValueError("За один раз можно добавить до 100 видео.")
    folder = core.ROOT / "assets/publishing"
    folder.mkdir(parents=True, exist_ok=True)
    imported, errors = [], []
    for file in files:
        target = folder / (uuid.uuid4().hex + ".mp4")
        try:
            name = Path(file.filename or "video.mp4").name
            if Path(name).suffix.lower() != ".mp4":
                raise ValueError("Нужен файл MP4.")
            if shutil.disk_usage(folder).free < 250_000_000:
                raise ValueError("Недостаточно места на диске.")
            total = 0
            with target.open("wb") as output:
                while block := file.file.read(1024 * 1024):
                    total += len(block)
                    if total > 4_000_000_000:
                        raise ValueError("Видео превышает 4 ГБ.")
                    output.write(block)
            with pub.LOCK:
                media_id = pub.add_media(target, name)
                with core.database() as db:
                    actual = db.execute("SELECT path FROM publishing_media WHERE id=?", (media_id,)).fetchone()[0]
                if actual != str(target):
                    target.unlink(missing_ok=True)
                imported.append(media_id)
        except (ValueError, OSError) as exc:
            target.unlink(missing_ok=True)
            errors.append(f"{Path(file.filename or 'video').name}: {exc}")
        finally:
            file.file.close()
    return {"imported": imported, "errors": errors}


@router.get("/media/{media_id}")
def media_file(media_id: str):
    with core.database() as db:
        row = db.execute("SELECT path FROM publishing_media WHERE id=?", (media_id,)).fetchone()
    if not row or not Path(row[0]).is_file():
        raise HTTPException(404)
    return FileResponse(row[0], media_type="video/mp4")


@router.post("/start")
def start(payload: dict):
    if not pub.public_configuration()["ready"]:
        raise ValueError("Сначала настройте подключение приложения TikTok и авторизуйте аккаунты.")
    result = pub.enqueue(payload["assignments"]) if "assignments" in payload else {"ok": True}
    core.set_meta("publishing_paused", False)
    return result


@router.post("/quick-start")
def quick_start():
    return pub.quick_start()


@router.post("/media/{media_id}/already-posted")
def already_posted(media_id: str, payload: dict):
    profile = pub.account(str(payload.get("account_id", "")))
    with pub.LOCK, core.database() as db:
        db.execute("BEGIN IMMEDIATE")
        media = db.execute("SELECT * FROM publishing_media WHERE id=?", (media_id,)).fetchone()
        if not media:
            raise ValueError("Видео не найдено.")
        if db.execute("SELECT 1 FROM publishing_tasks WHERE fingerprint=? AND status NOT IN ('failed','cancelled')", (media["fingerprint"],)).fetchone():
            raise ValueError("Для видео уже есть публикация или активная отправка.")
        db.execute("INSERT INTO publishing_tasks(id,media_id,account_id,fingerprint,status,post_info,progress,remote,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, media_id, profile["id"], media["fingerprint"], "published", "{}", 100, '{"manual":true}', core.now(), core.now()))
    return {"ok": True}


@router.post("/pause")
def pause():
    core.set_meta("publishing_paused", True)
    return {"ok": True}


@router.post("/tasks/{task_id}/{action}")
def action(task_id: str, action: str):
    return pub.task_action(task_id, action)
