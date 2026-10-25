"""Durable, global 40-second publishing queue with content-based deduplication."""
from pathlib import Path
from urllib.parse import urlencode
import hashlib
import json
import logging
import secrets
import sqlite3
import threading
import time
import uuid

import core
from tiktok_api import TikTokClient, UncertainRequest, protect, unprotect, validate_proxy, proxy_label

INTERVAL = 40
CALLBACK = "http://127.0.0.1:8765/api/publishing/oauth/callback"
LOCK = threading.RLock()
_account_locks = {}


def initialize():
    with core.database() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS publishing_accounts (
            id TEXT PRIMARY KEY, label TEXT NOT NULL, open_id TEXT UNIQUE,
            username TEXT DEFAULT '', nickname TEXT DEFAULT '', secret TEXT DEFAULT '',
            proxy TEXT DEFAULT '', creator TEXT DEFAULT '{}', checked REAL DEFAULT 0,
            error TEXT DEFAULT '', created TEXT, updated TEXT
        );
        CREATE TABLE IF NOT EXISTS publishing_media (
            id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, path TEXT NOT NULL,
            job_id TEXT, name TEXT, caption TEXT, duration REAL, size INTEGER, modified INTEGER, created TEXT
        );
        CREATE TABLE IF NOT EXISTS publishing_tasks (
            id TEXT PRIMARY KEY, media_id TEXT NOT NULL, account_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL, status TEXT NOT NULL, post_info TEXT NOT NULL,
            progress REAL DEFAULT 0, publish_id TEXT DEFAULT '', error TEXT DEFAULT '',
            remote TEXT DEFAULT '{}', next_check REAL DEFAULT 0,
            started REAL DEFAULT 0, created TEXT, updated TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS publishing_no_duplicates ON publishing_tasks(fingerprint)
            WHERE status NOT IN ('failed','cancelled');
        CREATE INDEX IF NOT EXISTS publishing_queue ON publishing_tasks(status,created);
        CREATE TABLE IF NOT EXISTS publishing_oauth (
            state TEXT PRIMARY KEY, account_id TEXT, secret TEXT, expires REAL
        );
        """)


def update_task(task_id, **fields):
    fields["updated"] = core.now()
    with core.database() as db:
        db.execute("UPDATE publishing_tasks SET " + ",".join(key + "=?" for key in fields) + " WHERE id=?", (*fields.values(), task_id))


def account(account_id):
    with core.database() as db:
        row = db.execute("SELECT * FROM publishing_accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise ValueError("Аккаунт не найден.")
    return dict(row)


def configuration():
    saved = core.get_meta("publishing_config", {})
    return {**saved, "client_secret": unprotect(saved.get("secret", "")).get("value", "")}


def public_configuration():
    saved = core.get_meta("publishing_config", {})
    return {"client_key": saved.get("client_key", ""), "secret_saved": bool(saved.get("secret")),
            "callback": CALLBACK, "ready": bool(saved.get("client_key") and saved.get("secret"))}


def save_configuration(payload):
    with LOCK:
        previous = core.get_meta("publishing_config", {})
        key = str(payload.get("client_key", "")).strip()
        secret = str(payload.get("client_secret", "")).strip()
        if not key or len(key) > 300 or len(secret) > 1000:
            raise ValueError("Введите Client key и Client secret приложения TikTok.")
        if previous.get("client_key") and previous["client_key"] != key:
            with core.database() as db:
                if db.execute("SELECT 1 FROM publishing_accounts WHERE secret<>''").fetchone():
                    raise ValueError("Сначала отключите аккаунты, подключённые к прежнему Client key.")
        sealed = protect({"value": secret}) if secret else previous.get("secret", "")
        if not sealed:
            raise ValueError("Введите Client secret.")
        core.set_meta("publishing_config", {"client_key": key, "secret": sealed})
    return public_configuration()


def save_account(payload):
    account_id = str(payload.get("id") or uuid.uuid4().hex)
    label = str(payload.get("label", "")).strip()[:80]
    if not label:
        raise ValueError("Введите название аккаунта.")
    with LOCK, core.database() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT * FROM publishing_accounts WHERE id=?", (account_id,)).fetchone()
        proxy = old["proxy"] if old else ""
        if payload.get("clear_proxy"):
            proxy = ""
        elif payload.get("proxy"):
            proxy = protect({"value": validate_proxy(payload["proxy"])})
        if old:
            if db.execute("SELECT 1 FROM publishing_tasks WHERE account_id=? AND status IN ('preparing','initiating','uploading','processing')", (account_id,)).fetchone():
                raise ValueError("Дождитесь завершения отправок этого аккаунта перед изменением профиля.")
            db.execute("UPDATE publishing_accounts SET label=?,proxy=?,updated=? WHERE id=?", (label, proxy, core.now(), account_id))
        else:
            if db.execute("SELECT COUNT(*) FROM publishing_accounts").fetchone()[0] >= 5:
                raise ValueError("Можно добавить до пяти аккаунтов.")
            db.execute("INSERT INTO publishing_accounts(id,label,proxy,created,updated) VALUES (?,?,?,?,?)", (account_id, label, proxy, core.now(), core.now()))
        if "quick" in payload:
            quick = payload["quick"]
            if not isinstance(quick, dict):
                raise ValueError("Некорректные настройки быстрого залива.")
            creator = json.loads(old["creator"]) if old else {}
            enabled = bool(quick.get("enabled", False))
            privacy = str(quick.get("privacy", ""))
            brand = str(quick.get("brand", "none"))
            if brand not in ("none", "own", "paid"):
                raise ValueError("Выберите тип рекламного контента.")
            if enabled and (not old or not old["secret"] or privacy not in creator.get("privacy_level_options", [])):
                raise ValueError("Для быстрого залива сначала подключите аккаунт и выберите видимость.")
            row = db.execute("SELECT value FROM meta WHERE key='publishing_quick_accounts'").fetchone()
            preferences = json.loads(row[0]) if row else {}
            preferences[account_id] = {"enabled": enabled, "privacy": privacy, "brand": brand, "is_aigc": bool(quick.get("is_aigc", False))}
            db.execute("INSERT INTO meta VALUES ('publishing_quick_accounts',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(preferences),))
    return {"id": account_id}


def disconnect(account_id):
    account(account_id)
    with LOCK, core.database() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM publishing_tasks WHERE account_id=? AND status IN ('preparing','initiating','uploading','processing')", (account_id,)).fetchone():
            raise ValueError("Сначала дождитесь подтверждения текущих публикаций.")
        db.execute("UPDATE publishing_tasks SET status='cancelled',updated=? WHERE account_id=? AND status='queued'", (core.now(), account_id))
        db.execute("UPDATE publishing_accounts SET secret='',creator='{}',checked=0,error='',updated=? WHERE id=?", (core.now(), account_id))
        db.execute("DELETE FROM publishing_oauth WHERE account_id=?", (account_id,))
    return {"ok": True}


def oauth_start(account_id):
    saved = public_configuration()
    if not saved["ready"]:
        raise ValueError("Сначала сохраните Client key и Client secret в настройках залива.")
    account(account_id)
    state, verifier, cookie = secrets.token_urlsafe(32), secrets.token_urlsafe(64), secrets.token_urlsafe(32)
    with core.database() as db:
        db.execute("DELETE FROM publishing_oauth WHERE expires<? OR account_id=?", (time.time(), account_id))
        db.execute("INSERT INTO publishing_oauth VALUES (?,?,?,?)", (state, account_id,
                   protect({"verifier": verifier, "cookie": cookie}), time.time() + 600))
    url = "https://www.tiktok.com/v2/auth/authorize/?" + urlencode({"client_key": saved["client_key"],
        "scope": "video.publish", "response_type": "code", "redirect_uri": CALLBACK, "state": state,
        "code_challenge": hashlib.sha256(verifier.encode()).hexdigest(), "code_challenge_method": "S256"})
    return url, cookie


def _new_client(row):
    return TikTokClient(unprotect(row["proxy"]).get("value", ""))


def _save_tokens(account_id, tokens):
    tokens = {**tokens, "expires_at": time.time() + int(tokens.get("expires_in", 0))}
    with core.database() as db:
        db.execute("UPDATE publishing_accounts SET secret=?,open_id=?,error='',updated=? WHERE id=?",
                   (protect(tokens), tokens["open_id"], core.now(), account_id))


def oauth_finish(state, code, cookie):
    with LOCK, core.database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM publishing_oauth WHERE state=?", (state,)).fetchone()
        if not row or row["expires"] < time.time():
            raise ValueError("Авторизация истекла. Нажмите «Подключить TikTok» ещё раз.")
        values = unprotect(row["secret"])
        if not cookie or not secrets.compare_digest(values["cookie"], cookie):
            raise ValueError("Откройте авторизацию из окна Studio в том же браузере.")
        db.execute("DELETE FROM publishing_oauth WHERE state=?", (state,))
        account_id = row["account_id"]
    row = account(account_id)
    client = _new_client(row)
    try:
        config = configuration()
        tokens = client.token({"client_key": config["client_key"], "client_secret": config["client_secret"],
            "code": code, "grant_type": "authorization_code", "redirect_uri": CALLBACK, "code_verifier": values["verifier"]})
        if "video.publish" not in tokens.get("scope", "").split(","):
            raise ValueError("Аккаунт не предоставил доступ к публикации видео.")
        if row["open_id"] and row["open_id"] != tokens["open_id"]:
            raise ValueError("Вы вошли в другой аккаунт. Создайте для него отдельный профиль в Studio.")
        with LOCK:
            try:
                _save_tokens(account_id, tokens)
            except sqlite3.IntegrityError:
                raise ValueError("Этот аккаунт уже подключён в другом профиле Studio.") from None
        creator = client.creator(tokens["access_token"])
        _save_creator(account_id, creator)
    finally:
        client.close()


def _save_creator(account_id, creator):
    with core.database() as db:
        db.execute("UPDATE publishing_accounts SET creator=?,username=?,nickname=?,checked=?,error='',updated=? WHERE id=?",
                   (json.dumps(creator, ensure_ascii=False), creator.get("creator_username", ""), creator.get("creator_nickname", ""), time.time(), core.now(), account_id))


def authorized_client(account_id):
    with LOCK:
        lock = _account_locks.setdefault(account_id, threading.RLock())
    with lock:
        row = account(account_id)
        tokens = unprotect(row["secret"])
        if not tokens.get("access_token"):
            raise ValueError("Сначала подключите аккаунт TikTok.")
        client = _new_client(row)
        try:
            if tokens.get("expires_at", 0) < time.time() + 120:
                config = configuration()
                new = client.token({"client_key": config["client_key"], "client_secret": config["client_secret"],
                    "grant_type": "refresh_token", "refresh_token": tokens.get("refresh_token", "")})
                if new.get("open_id") != tokens.get("open_id"):
                    raise ValueError("TikTok вернул другой аккаунт при обновлении авторизации.")
                tokens = {**tokens, **new}
                _save_tokens(account_id, tokens)
            return client, tokens["access_token"]
        except Exception:
            client.close()
            raise


def refresh_account(account_id):
    client = None
    try:
        client, token = authorized_client(account_id)
        _save_creator(account_id, client.creator(token))
    except ValueError as exc:
        with core.database() as db:
            db.execute("UPDATE publishing_accounts SET error=? WHERE id=?", (str(exc), account_id))
        raise
    finally:
        if client:
            client.close()
    return {"ok": True}


def fingerprint(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def add_media(path, name=None, job_id=None, caption=""):
    path = Path(path).resolve()
    stat = path.stat()
    info = core.probe(path)
    if not info["video"] or path.suffix.lower() != ".mp4":
        raise ValueError("Для залива добавьте видео MP4.")
    digest = fingerprint(path)
    if path.stat().st_mtime_ns != stat.st_mtime_ns or path.stat().st_size != stat.st_size:
        raise ValueError("Файл изменился при добавлении. Повторите импорт.")
    with core.database() as db:
        old = db.execute("SELECT * FROM publishing_media WHERE fingerprint=?", (digest,)).fetchone()
        if old:
            if not Path(old["path"]).is_file():
                db.execute("UPDATE publishing_media SET path=?,size=?,modified=? WHERE id=?", (str(path), stat.st_size, stat.st_mtime_ns, old["id"]))
            return old["id"]
        media_id = uuid.uuid4().hex
        db.execute("INSERT INTO publishing_media VALUES (?,?,?,?,?,?,?,?,?,?)", (media_id, digest, str(path), job_id,
                   name or path.name, caption, info["duration"], stat.st_size, stat.st_mtime_ns, core.now()))
    return media_id


def sync_generated():
    with core.database() as db:
        jobs = db.execute("SELECT j.*,b.name batch_name FROM jobs j JOIN batches b ON b.id=j.batch_id WHERE j.status='done' AND j.id NOT IN (SELECT job_id FROM publishing_media WHERE job_id IS NOT NULL)").fetchall()
    imported = 0
    for row in jobs:
        if Path(row["output"]).is_file():
            add_media(row["output"], f"{row['batch_name']} · видео {row['ordinal']:02d}", row["id"], json.loads(row["spec"])["caption"])
            imported += 1
    return {"imported": imported}


def public_state():
    quick_preferences = core.get_meta("publishing_quick_accounts", {})
    with core.database() as db:
        accounts = []
        for row in db.execute("SELECT * FROM publishing_accounts ORDER BY created"):
            accounts.append({key: row[key] for key in ("id", "label", "username", "nickname", "checked", "error")}
                            | {"connected": bool(row["secret"]), "proxy_label": proxy_label(unprotect(row["proxy"]).get("value", "")), "creator": json.loads(row["creator"]), "quick": quick_preferences.get(row["id"], {})})
        media = [dict(row) for row in db.execute("SELECT * FROM publishing_media ORDER BY created DESC")]
        tasks = [dict(row) for row in db.execute("SELECT t.*,a.label account_label,a.username,m.name media_name FROM publishing_tasks t JOIN publishing_accounts a ON a.id=t.account_id JOIN publishing_media m ON m.id=t.media_id ORDER BY t.created")]
    blocked = {row["fingerprint"]: row["status"] for row in tasks if row["status"] not in ("failed", "cancelled")}
    for row in media:
        row["exists"] = Path(row["path"]).is_file()
        row["blocked"] = blocked.get(row["fingerprint"], "")
        # Do not expose filesystem paths unnecessarily in the scheduling view.
        row.pop("path", None)
    for row in tasks:
        row["post_info"] = json.loads(row["post_info"])
        row["remote"] = json.loads(row["remote"])
    result = {"config": public_configuration(), "accounts": accounts, "media": media, "tasks": tasks,
            "paused": core.get_meta("publishing_paused", True), "interval": INTERVAL,
            "next_at": core.get_meta("publishing_next_at", 0), "server_time": time.time()}
    plan = quick_plan(result)
    result["quick"] = {key: value for key, value in plan.items() if key != "assignments"}
    return result


def enqueue(assignments, max_count=100):
    if not isinstance(assignments, list) or not assignments or (max_count is not None and len(assignments) > max_count):
        raise ValueError("Выберите от 1 до 100 видео и назначьте им аккаунты.")
    prepared, seen = [], set()
    for item in assignments:
        if not isinstance(item, dict):
            raise ValueError("Некорректное назначение видео.")
        with core.database() as db:
            media = db.execute("SELECT * FROM publishing_media WHERE id=?", (str(item.get("media_id", "")),)).fetchone()
        if not media or not Path(media["path"]).is_file():
            raise ValueError("Выбранный видеофайл недоступен.")
        if media["fingerprint"] in seen:
            raise ValueError("Одно видео можно назначить только одному аккаунту.")
        seen.add(media["fingerprint"])
        if fingerprint(media["path"]) != media["fingerprint"]:
            raise ValueError("Видео изменилось после добавления. Добавьте новый файл.")
        row = account(str(item.get("account_id", "")))
        creator = json.loads(row["creator"])
        privacy = str(item.get("privacy", ""))
        if not row["secret"]:
            raise ValueError("Подключите аккаунт «" + row["label"] + "» к TikTok.")
        if privacy not in creator.get("privacy_level_options", []):
            raise ValueError("Выберите доступную видимость публикации для «" + row["label"] + "».")
        caption = str(item.get("caption", media["caption"])).strip()
        if len(caption.encode("utf-16-le")) // 2 > 2200:
            raise ValueError("Описание TikTok должно быть не длиннее 2200 символов UTF-16.")
        brand = item.get("brand", "none")
        if brand not in ("none", "own", "paid"):
            raise ValueError("Неизвестный тип рекламного контента.")
        post = {"title": caption, "privacy_level": privacy,
                "disable_comment": not bool(item.get("allow_comment", False)),
                "disable_duet": not bool(item.get("allow_duet", False)),
                "disable_stitch": not bool(item.get("allow_stitch", False)),
                "brand_content_toggle": brand == "paid", "brand_organic_toggle": brand == "own",
                "is_aigc": bool(item.get("is_aigc", False))}
        validate_post(post, creator, media["duration"])
        prepared.append((uuid.uuid4().hex, media["id"], row["id"], media["fingerprint"], "queued", json.dumps(post, ensure_ascii=False), core.now(), core.now()))
    with LOCK, core.database() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.executemany("INSERT INTO publishing_tasks(id,media_id,account_id,fingerprint,status,post_info,created,updated) VALUES (?,?,?,?,?,?,?,?)", prepared)
        except sqlite3.IntegrityError:
            raise ValueError("Одно из видео уже отправлено, находится в очереди или ждёт подтверждения. Повтор заблокирован.") from None
    return {"queued": len(prepared)}


def quick_plan(snapshot):
    """Choose new files using explicitly saved recipients and publication settings."""
    result = {"ready": False, "count": 0, "reason": "", "recipients": [], "assignments": [], "skipped": 0}
    if not snapshot["config"]["ready"]:
        result["reason"] = "Один раз настрой подключение приложения TikTok и авторизуй аккаунты."
        return result
    profiles = [row for row in snapshot["accounts"] if row["quick"].get("enabled")]
    if not profiles:
        result["reason"] = "Один раз включи «Быстрый залив» в настройках нужных аккаунтов и выбери видимость."
        return result
    for profile in profiles:
        if not profile["connected"] or profile["quick"].get("privacy") not in profile["creator"].get("privacy_level_options", []):
            result["reason"] = "Проверь подключение и сохранённую видимость аккаунта «" + profile["label"] + "»."
            return result
    result["recipients"] = [{"label": row["label"], "privacy": row["quick"]["privacy"]} for row in profiles]
    cursor = int(core.get_meta("publishing_quick_cursor", 0))
    for media in reversed(snapshot["media"]):
        if media["blocked"] or not media["exists"]:
            continue
        if len(media["caption"].encode("utf-16-le")) // 2 > 2200:
            result["skipped"] += 1
            continue
        eligible = [row for row in profiles if media["duration"] <= int(row["creator"].get("max_video_post_duration_sec", 0))]
        if not eligible:
            result["skipped"] += 1
            continue
        profile = eligible[cursor % len(eligible)]
        result["assignments"].append({"media_id": media["id"], "account_id": profile["id"],
            "privacy": profile["quick"]["privacy"], "brand": profile["quick"].get("brand", "none"),
            "is_aigc": profile["quick"].get("is_aigc", False), "caption": media["caption"]})
        cursor += 1
    result.update(ready=True, count=len(result["assignments"]))
    return result


def quick_start():
    with LOCK:
        # Discover new renders on this click; no separate library refresh is required.
        sync_generated()
        snapshot = public_state()
        plan = quick_plan(snapshot)
        if not plan["ready"]:
            raise ValueError(plan["reason"])
        waiting = sum(row["status"] == "queued" for row in snapshot["tasks"])
        if not plan["assignments"]:
            if waiting:
                core.set_meta("publishing_paused", False)
                return {"queued": 0, "resumed": waiting, "skipped": plan["skipped"]}
            return {"queued": 0, "resumed": 0, "skipped": plan["skipped"]}
        result = enqueue(plan["assignments"], max_count=None)
        core.set_meta("publishing_quick_cursor", int(core.get_meta("publishing_quick_cursor", 0)) + result["queued"])
        core.set_meta("publishing_paused", False)
        return {**result, "resumed": waiting, "skipped": plan["skipped"]}


def validate_post(post, creator, duration):
    if post["privacy_level"] not in creator.get("privacy_level_options", []):
        raise ValueError("Доступная видимость изменилась. Обновите аккаунт и назначение.")
    if duration > int(creator.get("max_video_post_duration_sec", 0)):
        raise ValueError("Видео длиннее доступной для аккаунта длительности.")
    for permission in ("comment", "duet", "stitch"):
        if creator.get(permission + "_disabled") and not post["disable_" + permission]:
            raise ValueError("В аккаунте запрещено действие: " + permission)


def task_action(task_id, action):
    with LOCK, core.database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM publishing_tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError("Отправка не найдена.")
        if action == "cancel" and row["status"] == "queued":
            db.execute("UPDATE publishing_tasks SET status='cancelled',updated=? WHERE id=?", (core.now(), task_id))
        elif action == "retry" and row["status"] == "failed":
            if db.execute("SELECT 1 FROM publishing_tasks WHERE fingerprint=? AND status NOT IN ('failed','cancelled')", (row["fingerprint"],)).fetchone():
                raise ValueError("Это видео уже занято другой отправкой или опубликовано.")
            db.execute("UPDATE publishing_tasks SET status='queued',publish_id='',remote='{}',progress=0,error='',updated=? WHERE id=?", (core.now(), task_id))
        elif action == "check" and row["publish_id"] and row["status"] in ("processing", "uncertain"):
            db.execute("UPDATE publishing_tasks SET status='processing',next_check=0,updated=? WHERE id=?", (core.now(), task_id))
        else:
            raise ValueError("Это действие недоступно для текущего состояния отправки.")
    return {"ok": True}


class Publisher:
    def __init__(self):
        self.stop = threading.Event()
        self.thread = None

    def start(self):
        self.stop.clear()
        with core.database() as db:
            # Never repeat an init request after a crash. Reconcile an existing publish_id.
            db.execute("UPDATE publishing_tasks SET status=CASE WHEN publish_id<>'' THEN 'processing' ELSE 'uncertain' END,error='Отправка прервана. Проверяем результат без повторной публикации.',next_check=0,updated=? WHERE status IN ('initiating','uploading')", (core.now(),))
            db.execute("UPDATE publishing_tasks SET status='queued',updated=? WHERE status='preparing'", (core.now(),))
        core.set_meta("publishing_paused", True)
        self.thread = threading.Thread(target=self.run, name="tiktok-publisher", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=48)

    def run(self):
        while not self.stop.is_set():
            try:
                self.step()
            except Exception:
                # Messages/URLs from third-party HTTP exceptions may include credentials.
                logging.error("Publishing scheduler failed; waiting before next iteration.")
            self.stop.wait(.5)

    def step(self):
        with core.database() as db:
            pending = db.execute("SELECT * FROM publishing_tasks WHERE status='processing' AND next_check<=? ORDER BY next_check LIMIT 1", (time.time(),)).fetchone()
        if pending:
            self.check(dict(pending))
        if self.stop.is_set():
            return
        with LOCK, core.database() as db:
            db.execute("BEGIN IMMEDIATE")
            paused = db.execute("SELECT value FROM meta WHERE key='publishing_paused'").fetchone()
            clock = db.execute("SELECT value FROM meta WHERE key='publishing_next_at'").fetchone()
            if not paused or json.loads(paused[0]) or (clock and json.loads(clock[0]) > time.time()):
                return
            row = db.execute("SELECT * FROM publishing_tasks WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return
            db.execute("UPDATE publishing_tasks SET status='preparing',updated=? WHERE id=? AND status='queued'", (core.now(), row["id"]))
        self.send(dict(row))

    def send(self, task):
        client = None
        initiated = False
        try:
            with core.database() as db:
                media = dict(db.execute("SELECT * FROM publishing_media WHERE id=?", (task["media_id"],)).fetchone())
            path = Path(media["path"])
            if fingerprint(path) != task["fingerprint"]:
                raise ValueError("Видео изменилось. Отправка остановлена.")
            client, token = authorized_client(task["account_id"])
            if self.stop.is_set():
                update_task(task["id"], status="queued")
                return
            creator = client.creator(token)
            _save_creator(task["account_id"], creator)
            post = json.loads(task["post_info"])
            validate_post(post, creator, media["duration"])
            if self.stop.is_set():
                update_task(task["id"], status="queued")
                return
            started = time.time()
            # Commit intent and cooldown before the external request, including crash recovery.
            with core.database() as db:
                db.execute("UPDATE publishing_tasks SET status='initiating',started=?,updated=? WHERE id=?", (started, core.now(), task["id"]))
                db.execute("INSERT INTO meta VALUES ('publishing_next_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(started + INTERVAL),))
            initiated = True
            data, chunk, count = client.initialize(token, post, path.stat().st_size)
            task["publish_id"] = data["publish_id"]
            update_task(task["id"], status="uploading", publish_id=task["publish_id"])
            client.upload(data["upload_url"], path, chunk, count,
                          lambda value: update_task(task["id"], progress=value), self.stop.is_set)
            update_task(task["id"], status="processing", progress=100, next_check=time.time()+5, error="")
        except (UncertainRequest, InterruptedError) as exc:
            update_task(task["id"], status="processing" if task.get("publish_id") else "uncertain", next_check=time.time()+10,
                        error=str(exc) or "Передача прервана. Проверяем результат без создания новой публикации.")
        except (ValueError, OSError) as exc:
            update_task(task["id"], status="processing" if task.get("publish_id") else "failed", next_check=time.time()+10,
                        error=str(exc)[:600])
        except Exception:
            update_task(task["id"], status="processing" if task.get("publish_id") else "uncertain" if initiated else "failed", next_check=time.time()+10,
                        error="Ошибка отправки. Повтор возможен только после подтверждения результата.")
        finally:
            if client:
                client.close()

    def check(self, task):
        client = None
        try:
            client, token = authorized_client(task["account_id"])
            if self.stop.is_set():
                return
            remote = client.status(token, task["publish_id"])
            status = remote.get("status")
            if status == "PUBLISH_COMPLETE":
                update_task(task["id"], status="published", progress=100, remote=json.dumps(remote), error="")
            elif status == "FAILED":
                update_task(task["id"], status="failed", remote=json.dumps(remote), error="TikTok отклонил публикацию: " + str(remote.get("fail_reason", "unknown"))[:200])
            elif status == "PROCESSING_UPLOAD" and time.time() - task["started"] > 3600:
                update_task(task["id"], status="uncertain", remote=json.dumps(remote), error="Загрузка не завершена. Повтор заблокирован до подтверждения результата TikTok.")
            else:
                update_task(task["id"], remote=json.dumps(remote), next_check=time.time()+20, error="")
        except (ValueError, OSError) as exc:
            update_task(task["id"], next_check=time.time()+60, error=str(exc)[:600])
        finally:
            if client:
                client.close()
