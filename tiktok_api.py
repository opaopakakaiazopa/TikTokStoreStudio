"""TikTok Desktop OAuth and Direct Post transport. Never log request URLs/tokens."""
import base64
import ctypes
import json
import logging
import os
from urllib.parse import urlsplit

import httpx


API = "https://open.tiktokapis.com"
# Signed upload URLs contain credentials in their query strings.
logging.getLogger("httpx").setLevel(logging.WARNING)


class UncertainRequest(Exception):
    """The server may have accepted a request; initiating again could duplicate it."""


def protect(value):
    return _dpapi(json.dumps(value, ensure_ascii=False).encode(), False)


def unprotect(value):
    return json.loads(_dpapi(value, True)) if value else {}


def _dpapi(value, decrypt):
    if os.name != "nt":
        raise ValueError("Хранилище авторизации использует защиту учётной записи Windows.")
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    raw = base64.b64decode(value) if decrypt else value
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        method = crypt.CryptUnprotectData
        method.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = method(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        method = crypt.CryptProtectData
        method.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = method(ctypes.byref(source), "Store Studio", None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise ValueError("Не удалось открыть защищённую авторизацию. Используйте ту же учётную запись Windows.")
    try:
        data = ctypes.string_at(target.data, target.size)
        return data if decrypt else base64.b64encode(data).decode("ascii")
    finally:
        kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def validate_proxy(value):
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in ("http", "https", "socks5", "socks5h") and parsed.hostname and parsed.port and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
    except ValueError:
        valid = False
    if not valid or len(value) > 2000:
        raise ValueError("Прокси: http://логин:пароль@адрес:порт или socks5://адрес:порт.")
    return value


def proxy_label(value):
    parsed = urlsplit(value)
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}" if value else "Прямое соединение"


def chunk_plan(size):
    if size <= 0 or size > 4_000_000_000:
        raise ValueError("Размер видео должен быть больше нуля и не превышать 4 ГБ.")
    # TikTok uses floor division: merge the trailing bytes into the final chunk.
    chunk = min(size, 10_000_000)
    count = max(1, size // chunk)
    return chunk, count


class TikTokClient:
    def __init__(self, proxy="", transport=None):
        self.http = httpx.Client(proxy=proxy or None, transport=transport, trust_env=False,
                                 follow_redirects=False, timeout=httpx.Timeout(45, connect=15))

    def close(self):
        self.http.close()

    def token(self, body):
        try:
            response = self.http.post(API + "/v2/oauth/token/", data=body)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise ValueError("Не удалось получить авторизацию TikTok. Проверьте подключение и прокси.") from None
        if response.status_code != 200 or not data.get("access_token"):
            raise ValueError("TikTok отклонил авторизацию: " + str(data.get("error", "ошибка ответа")))
        return data

    def call(self, endpoint, token, body=None, initiating=False):
        try:
            response = self.http.post(API + endpoint, headers={"Authorization": "Bearer " + token}, json=body or {})
            data = response.json()
        except (httpx.HTTPError, ValueError):
            if initiating:
                raise UncertainRequest("Соединение прервалось при создании публикации. Повторная отправка заблокирована во избежание дубля.") from None
            raise ValueError("Нет ответа TikTok. Проверьте подключение и прокси.") from None
        if initiating and response.status_code >= 500:
            raise UncertainRequest("Ответ TikTok не позволяет подтвердить создание публикации. Автоповтор заблокирован.")
        error = data.get("error", {})
        if initiating and (not isinstance(error, dict) or not error.get("code")):
            raise UncertainRequest("TikTok вернул неполный ответ. Автоповтор заблокирован.")
        if not isinstance(error, dict) or error.get("code") != "ok" or response.status_code != 200:
            code = error.get("code", "invalid_response") if isinstance(error, dict) else "invalid_response"
            raise ValueError("TikTok: " + str(code)[:150])
        if not isinstance(data.get("data"), dict):
            if initiating:
                raise UncertainRequest("TikTok вернул неполный ответ. Автоповтор заблокирован.")
            raise ValueError("TikTok вернул неполный ответ.")
        return data["data"]

    def creator(self, token):
        return self.call("/v2/post/publish/creator_info/query/", token)

    def status(self, token, publish_id):
        return self.call("/v2/post/publish/status/fetch/", token, {"publish_id": publish_id})

    def initialize(self, token, post_info, size):
        chunk, count = chunk_plan(size)
        data = self.call("/v2/post/publish/video/init/", token, {
            "post_info": post_info,
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}
        }, initiating=True)
        if not data.get("publish_id") or not data.get("upload_url"):
            raise UncertainRequest("TikTok не вернул идентификатор загрузки. Автоповтор заблокирован.")
        return data, chunk, count

    def upload(self, url, path, chunk, count, progress, should_stop):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or not (parsed.hostname == "tiktokapis.com" or parsed.hostname.endswith(".tiktokapis.com")) or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError("TikTok вернул неизвестный адрес загрузки.")
        size = path.stat().st_size
        with path.open("rb") as source:
            for index in range(count):
                if should_stop():
                    raise InterruptedError()
                start = index * chunk
                length = size - start if index == count - 1 else chunk
                content = source.read(length)
                if len(content) != length:
                    raise ValueError("Файл изменился во время загрузки.")
                try:
                    response = self.http.put(url, content=content, headers={"Content-Type": "video/mp4",
                        "Content-Length": str(length), "Content-Range": f"bytes {start}-{start+length-1}/{size}"})
                except httpx.HTTPError:
                    raise UncertainRequest("Передача прервалась. Проверяем ту же публикацию, не создавая новую.") from None
                expected = 201 if index == count - 1 else 206
                if response.status_code != expected:
                    raise UncertainRequest("TikTok не подтвердил передачу файла. Проверяем состояние публикации.")
                progress((start + length) / size * 100)
