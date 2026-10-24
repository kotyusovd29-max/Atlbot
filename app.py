#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Страница пакетов ТТН и Telegram-бот. Только стандартная библиотека Python 3."""

import http.client
import json
import mimetypes
import os
import re
import secrets
import socket
import sqlite3
import ssl
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DB_PATH = os.path.join(BASE_DIR, "data.sqlite")
INDEX_PATH = os.path.join(BASE_DIR, "index.html")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
MARKER_PATH = os.path.join(BASE_DIR, ".ttn")
PORT = int(os.environ.get("PORT", "8000"))
PUBLIC_URL = "https://pr1.kotusov.f5-vibe.ru"
TZ = timezone(timedelta(hours=3))
MAX_BYTES = 25 * 1024 * 1024

BTN = "Отправить пакет документов"
BTN_OLD = "Отправить новый пакет документов"
WELCOME = "Здравствуйте. Нажмите «Отправить пакет документов», чтобы прислать ТТН."
PROMPT_LOAD = "Отправьте ТТН погрузки."
PROMPT_UNLOAD = "Документ загружен. Как будете готовы, отправьте ТТН отгрузки."
NEED_FILE = "Пришлите фото или файл документа."
NEED_BUTTON = "Нажмите «Отправить пакет документов»."
DONE = "Файлы отправлены."
FILE_FAIL = "Не удалось получить файл. Пришлите его ещё раз."
TOO_BIG = "Файл больше 20 МБ, Telegram его не отдаёт. Пришлите фото или файл меньшего размера."

PREVIEW_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}

DEFAULT_CONFIG = {
    "bot_token": "",
    "webhook_secret": "",
    "public_url": PUBLIC_URL,
}

_db_lock = threading.Lock()
_bot_lock = threading.Lock()
_send_lock = threading.Lock()
_last_send = 0.0
_flood_wait = 0


class TelegramBusy(Exception):
    def __init__(self, retry_after):
        self.retry_after = max(1, int(retry_after or 1))


class TelegramFileTooBig(Exception):
    pass


_ssl_ctx = ssl.create_default_context()


class IPv4HTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        port = self.port or 443
        infos = socket.getaddrinfo(self.host, port, socket.AF_INET, socket.SOCK_STREAM)
        last = None
        for family, socktype, proto, _canon, addr in infos:
            sock = socket.socket(family, socktype, proto)
            try:
                sock.settimeout(self.timeout)
                sock.connect(addr)
                ctx = getattr(self, "_context", None) or _ssl_ctx
                self.sock = ctx.wrap_socket(sock, server_hostname=self.host)
                return
            except Exception as exc:
                last = exc
                try:
                    sock.close()
                except Exception:
                    pass
        raise last or OSError("telegram connect")


def telegram_open(method, path, body=None, timeout=20):
    conn = IPv4HTTPSConnection("api.telegram.org", 443, timeout=timeout, context=_ssl_ctx)
    headers = {"User-Agent": "ttn-bot", "Connection": "close"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=body, headers=headers)
    return conn, conn.getresponse()


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for key in DEFAULT_CONFIG:
                if isinstance(raw.get(key), str):
                    cfg[key] = raw[key]
    except FileNotFoundError:
        pass
    except Exception as exc:
        print("config read error:", type(exc).__name__)
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


CONFIG = load_config()


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with _db_lock, db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS drivers (
            telegram_user_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_user_id INTEGER NOT NULL,
            driver_name TEXT NOT NULL,
            driver_username TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            status TEXT NOT NULL,
            loading_path TEXT,
            loading_name TEXT,
            loading_mime TEXT,
            unloading_path TEXT,
            unloading_name TEXT,
            unloading_mime TEXT
        );
        CREATE TABLE IF NOT EXISTS dialogs (
            telegram_user_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            package_id INTEGER,
            generation INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS seen_updates (
            update_id INTEGER PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS saved_files (
            file_id TEXT PRIMARY KEY,
            package_id INTEGER NOT NULL,
            slot TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_packages_created ON packages(created_at);
        CREATE INDEX IF NOT EXISTS idx_packages_driver ON packages(telegram_user_id);
        """)
    with open(MARKER_PATH, "a", encoding="utf-8"):
        pass


def local_dt(ts):
    return datetime.fromtimestamp(int(ts), TZ)


def driver_name(user):
    parts = [user.get("first_name") or "", user.get("last_name") or ""]
    name = " ".join(p for p in parts if p).strip()
    if name:
        return name
    if user.get("username"):
        return user["username"]
    return "Водитель %s" % user.get("id", "")


def upsert_driver(conn, user):
    uid = int(user["id"])
    name = driver_name(user)
    username = user.get("username") or ""
    conn.execute(
        """
        INSERT INTO drivers (telegram_user_id, name, username, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(telegram_user_id) DO UPDATE SET
            name = excluded.name,
            username = excluded.username,
            updated_at = excluded.updated_at
        """,
        (uid, name, username, int(time.time())),
    )
    return uid, name, username


def dialog_row(conn, uid):
    row = conn.execute(
        "SELECT state, package_id, generation FROM dialogs WHERE telegram_user_id = ?",
        (uid,),
    ).fetchone()
    if row:
        return row["state"], row["package_id"], int(row["generation"])
    return "idle", None, 0


def set_dialog(conn, uid, state, package_id, generation):
    conn.execute(
        """
        INSERT INTO dialogs (telegram_user_id, state, package_id, generation)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(telegram_user_id) DO UPDATE SET
            state = excluded.state,
            package_id = excluded.package_id,
            generation = excluded.generation
        """,
        (uid, state, package_id, generation),
    )


def normalized_text(text):
    return " ".join((text or "").split())


def is_start(text):
    head = normalized_text(text).split(" ", 1)[0]
    return head == "/start" or head.startswith("/start@")


def is_new_packet(text):
    value = normalized_text(text)
    if value in (BTN, BTN_OLD) or value.casefold() in (BTN.casefold(), BTN_OLD.casefold()):
        return True
    return value == "/new" or "новый пакет" in value.casefold()


def confirm_text(slot):
    if slot == "loading":
        return PROMPT_UNLOAD
    return DONE


def extract_file(message):
    photos = message.get("photo") or []
    if photos:
        photo = photos[-1]
        if photo.get("file_id"):
            return {
                "file_id": photo["file_id"],
                "name": "photo.jpg",
                "mime": "image/jpeg",
            }
    document = message.get("document") or {}
    if document.get("file_id"):
        return {
            "file_id": document["file_id"],
            "name": document.get("file_name") or "document",
            "mime": document.get("mime_type") or "application/octet-stream",
        }
    return None


def safe_ext(file_name, file_path):
    for candidate in (file_name, file_path):
        ext = os.path.splitext(candidate or "")[1].lower()
        if re.fullmatch(r"\.[a-z0-9]{1,8}", ext or ""):
            return ext
    return ""


def keyboard():
    return {
        "keyboard": [[{"text": BTN}]],
        "resize_keyboard": True,
    }


def _telegram_error(method, status, raw):
    detail = ""
    retry_after = 0
    try:
        parsed_error = json.loads(raw.decode("utf-8", "replace"))
        detail = str(parsed_error.get("description") or "")[:200]
        retry_after = int((parsed_error.get("parameters") or {}).get("retry_after") or 0)
    except Exception:
        detail = ""
    print("telegram http", method, status, detail)
    if status == 429 or retry_after:
        raise TelegramBusy(retry_after or 2)
    if "too big" in detail.lower():
        raise TelegramFileTooBig()
    raise RuntimeError("telegram")


def tg_call(method, payload=None, query=None, timeout=20):
    token = (CONFIG.get("bot_token") or "").strip()
    if not token:
        raise RuntimeError("no token")
    path = "/bot%s/%s" % (token, method)
    body = None
    if query:
        path += "?" + urllib.parse.urlencode(query)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    conn = None
    try:
        conn, resp = telegram_open("POST" if body is not None else "GET", path, body, timeout)
        raw = resp.read()
        status = resp.status
    except TelegramBusy:
        raise
    except Exception as exc:
        print("telegram error", method, type(exc).__name__)
        raise
    finally:
        if conn is not None:
            conn.close()
    if status >= 400:
        _telegram_error(method, status, raw)
    parsed = json.loads(raw.decode("utf-8"))
    if not parsed.get("ok"):
        print("telegram api", method, "not ok")
        raise RuntimeError("telegram")
    return parsed.get("result")


def reply(chat_id, text):
    global _last_send, _flood_wait
    with _send_lock:
        try:
            tg_call("sendMessage", {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": keyboard(),
            })
            _last_send = time.time()
            return True
        except TelegramBusy as exc:
            _flood_wait = max(_flood_wait, exc.retry_after)
            print("reply busy", exc.retry_after)
            return False
        except Exception:
            print("reply failed")
            return False


def _save_telegram_file(file_path, dest_path):
    token = (CONFIG.get("bot_token") or "").strip()
    parts = [part for part in file_path.split("/") if part and part != ".."]
    if not parts:
        raise RuntimeError("file path")
    path = "/file/bot%s/%s" % (token, "/".join(urllib.parse.quote(part) for part in parts))
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp = dest_path + ".part"
    conn = None
    try:
        conn, resp = telegram_open("GET", path, timeout=12)
        if resp.status >= 400:
            raw = resp.read(800)
            _telegram_error("file", resp.status, raw)
        total = 0
        with open(tmp, "wb") as out:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BYTES:
                    raise TelegramFileTooBig()
                out.write(chunk)
        if total <= 0:
            raise RuntimeError("empty file")
        os.replace(tmp, dest_path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def fetch_file(file_id, dest_path):
    last = None
    for attempt in range(2):
        try:
            meta = tg_call("getFile", query={"file_id": file_id}, timeout=15)
            file_path = (meta or {}).get("file_path") or ""
            if not file_path:
                raise RuntimeError("file path")
            _save_telegram_file(file_path, dest_path)
            return file_path
        except TelegramFileTooBig:
            raise
        except Exception as exc:
            last = exc
            print("download attempt", attempt + 1, type(exc).__name__)
            time.sleep(0.5 + attempt)
    raise last or RuntimeError("download")


def show_welcome(conn, user):
    uid, _name, _username = upsert_driver(conn, user)
    _state, _package_id, generation = dialog_row(conn, uid)
    set_dialog(conn, uid, "idle", None, generation + 1)
    return uid


def start_packet(conn, user):
    uid, _name, _username = upsert_driver(conn, user)
    _state, _package_id, generation = dialog_row(conn, uid)
    generation += 1
    set_dialog(conn, uid, "await_loading", None, generation)
    return uid


def begin_loading(conn, user):
    uid, name, username = upsert_driver(conn, user)
    state, _package_id, generation = dialog_row(conn, uid)
    if state != "await_loading":
        return None
    cur = conn.execute(
        """
        INSERT INTO packages (
            telegram_user_id, driver_name, driver_username, created_at, status
        ) VALUES (?, ?, ?, ?, 'loading')
        """,
        (uid, name, username, int(time.time())),
    )
    return {
        "id": cur.lastrowid,
        "generation": generation,
        "uid": uid,
    }


def saved_slot(file_id):
    if not file_id:
        return None
    with _db_lock, db() as conn:
        row = conn.execute(
            "SELECT slot FROM saved_files WHERE file_id = ?",
            (file_id,),
        ).fetchone()
    return row["slot"] if row else None


def attach_file(conn, package_id, slot, rel, name, mime, file_id):
    if slot == "loading":
        conn.execute(
            """
            UPDATE packages
            SET loading_path = ?, loading_name = ?, loading_mime = ?, status = 'loading'
            WHERE id = ?
            """,
            (rel, name, mime, package_id),
        )
    else:
        conn.execute(
            """
            UPDATE packages
            SET unloading_path = ?, unloading_name = ?, unloading_mime = ?, status = 'complete'
            WHERE id = ?
            """,
            (rel, name, mime, package_id),
        )
    if file_id:
        conn.execute(
            "INSERT OR REPLACE INTO saved_files (file_id, package_id, slot) VALUES (?, ?, ?)",
            (file_id, package_id, slot),
        )


def state_matches(conn, uid, generation, expected):
    state, package_id, current = dialog_row(conn, uid)
    if current != generation or state != expected:
        return False, None
    return True, package_id


def place_file(user, info, tmp_path, ext):
    existing = saved_slot(info.get("file_id"))
    if existing:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return "ok", existing
    uid = int(user["id"])
    with _db_lock, db() as conn:
        state, package_id, generation = dialog_row(conn, uid)
        slot = None
        if state == "await_loading":
            opened = begin_loading(conn, user)
            if opened:
                package_id = opened["id"]
                generation = opened["generation"]
                slot = "loading"
        elif state == "await_unloading" and package_id:
            owned = conn.execute(
                """
                SELECT loading_path FROM packages
                WHERE id = ? AND telegram_user_id = ?
                """,
                (package_id, uid),
            ).fetchone()
            if owned and owned["loading_path"]:
                slot = "unloading"
        if slot is None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return "need_button", "loading"
        folder = os.path.join(UPLOAD_DIR, str(package_id))
        os.makedirs(folder, exist_ok=True)
        filename = slot + ext
        dest = os.path.join(folder, filename)
        os.replace(tmp_path, dest)
        rel = "uploads/%s/%s" % (package_id, filename)
        attach_file(
            conn, package_id, slot, rel,
            info.get("name") or filename, info.get("mime") or "", info.get("file_id") or "",
        )
        if slot == "loading":
            set_dialog(conn, uid, "await_unloading", package_id, generation)
        else:
            set_dialog(conn, uid, "idle", None, generation)
    return "ok", slot


def say(chat_id, text):
    if reply(chat_id, text):
        return "ok"
    return "retry"


def take_file(user, info):
    uid = int(user["id"])
    tmp_dir = os.path.join(UPLOAD_DIR, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_path = os.path.join(tmp_dir, "%s-%s" % (uid, secrets.token_hex(8)))
    try:
        remote_path = fetch_file(info["file_id"], tmp_path)
    except TelegramFileTooBig:
        print("download too big")
        return "too_big", None
    except Exception as exc:
        print("download failed", type(exc).__name__)
        again = saved_slot(info.get("file_id"))
        if again:
            return "ok", again
        return "fail", None
    ext = safe_ext(info.get("name"), remote_path)
    return place_file(user, info, tmp_path, ext)


def handle_message(message):
    user = message.get("from") or {}
    chat = message.get("chat") or {}
    if "id" not in user or "id" not in chat:
        return "ok"
    text = message.get("text") or ""
    incoming = extract_file(message)
    print("in", chat.get("id"), "text" if text else ("file" if incoming else "other"))
    chat_id = chat["id"]

    if incoming:
        existing = saved_slot(incoming.get("file_id"))
        if existing:
            return say(chat_id, confirm_text(existing))

    if is_start(text) and not incoming:
        with _db_lock, db() as conn:
            show_welcome(conn, user)
        return say(chat_id, WELCOME)

    if is_new_packet(text) and not incoming:
        with _db_lock, db() as conn:
            start_packet(conn, user)
        return say(chat_id, PROMPT_LOAD)

    with _db_lock, db() as conn:
        upsert_driver(conn, user)
        state, _package_id, _generation = dialog_row(conn, int(user["id"]))

    if not incoming:
        return say(chat_id, NEED_BUTTON if state == "idle" else NEED_FILE)

    if state == "idle":
        return say(chat_id, NEED_BUTTON)

    result, slot = take_file(user, incoming)
    if result == "too_big":
        return say(chat_id, TOO_BIG)
    if result == "fail":
        return say(chat_id, FILE_FAIL)
    if result != "ok":
        return say(chat_id, NEED_BUTTON)
    return say(chat_id, confirm_text(slot))


def handle_update(update):
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        return "ok"
    with _bot_lock:
        with _db_lock, db() as conn:
            seen = conn.execute(
                "SELECT 1 FROM seen_updates WHERE update_id = ?",
                (update_id,),
            ).fetchone()
        if seen:
            return "ok"
        message = update.get("message") or {}
        try:
            result = handle_message(message) if message else "ok"
        except Exception as exc:
            print("update error", type(exc).__name__)
            return "retry"
        if result == "retry":
            return "retry"
        with _db_lock, db() as conn:
            conn.execute("INSERT OR IGNORE INTO seen_updates (update_id) VALUES (?)", (update_id,))
        return "ok"


def list_drivers():
    with _db_lock, db() as conn:
        rows = conn.execute(
            """
            SELECT d.telegram_user_id, d.name, d.username
            FROM drivers d
            WHERE EXISTS (
                SELECT 1 FROM packages p WHERE p.telegram_user_id = d.telegram_user_id
            )
            ORDER BY d.name COLLATE NOCASE, d.telegram_user_id
            """
        ).fetchall()
    return [
        {"id": row["telegram_user_id"], "name": row["name"], "username": row["username"]}
        for row in rows
    ]


def file_public(path, name, mime):
    if not path:
        return None
    preview = mime in PREVIEW_MIME
    return {"name": name or "document", "mime": mime or "", "url": path_to_url(path), "preview": preview}


def path_to_url(rel):
    parts = rel.split("/")
    if len(parts) != 3 or parts[0] != "uploads":
        return ""
    return "/files/%s/%s" % (parts[1], "loading" if parts[2].startswith("loading") else "unloading")


def list_packages(driver_id, day):
    clauses = []
    args = []
    if driver_id:
        clauses.append("p.telegram_user_id = ?")
        args.append(driver_id)
    if day:
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ)
        end = start + timedelta(days=1)
        clauses.append("p.created_at >= ? AND p.created_at < ?")
        args.extend([int(start.timestamp()), int(end.timestamp())])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    query = """
        SELECT p.*,
               COALESCE(d.name, p.driver_name) AS show_name,
               COALESCE(d.username, p.driver_username) AS show_username
        FROM packages p
        LEFT JOIN drivers d ON d.telegram_user_id = p.telegram_user_id
        %s
        ORDER BY p.created_at DESC, p.id DESC
    """ % where
    with _db_lock, db() as conn:
        rows = conn.execute(query, args).fetchall()
    items = []
    for row in rows:
        moment = local_dt(row["created_at"])
        items.append({
            "id": row["id"],
            "driver_id": row["telegram_user_id"],
            "driver_name": row["show_name"],
            "driver_username": row["show_username"],
            "date": moment.strftime("%Y-%m-%d"),
            "date_label": moment.strftime("%d.%m.%Y"),
            "time_label": moment.strftime("%H:%M"),
            "status": row["status"],
            "loading": file_public(row["loading_path"], row["loading_name"], row["loading_mime"]),
            "unloading": file_public(row["unloading_path"], row["unloading_name"], row["unloading_mime"]),
        })
    return items


def resolve_stored_file(package_id, slot):
    column = "loading_path" if slot == "loading" else "unloading_path"
    with _db_lock, db() as conn:
        row = conn.execute(
            "SELECT %s AS path FROM packages WHERE id = ?" % column,
            (package_id,),
        ).fetchone()
    if not row or not row["path"]:
        return None, None, None
    rel = row["path"].replace("\\", "/")
    prefix = "uploads/%s/" % int(package_id)
    name = os.path.basename(rel)
    if not rel.startswith(prefix) or name != rel[len(prefix):] or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return None, None, None
    root = os.path.realpath(os.path.join(UPLOAD_DIR, str(int(package_id))))
    path = os.path.realpath(os.path.join(root, name))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return None, None, None
    name_col = "loading_name" if slot == "loading" else "unloading_name"
    mime_col = "loading_mime" if slot == "loading" else "unloading_mime"
    with _db_lock, db() as conn:
        meta = conn.execute(
            "SELECT %s AS name, %s AS mime FROM packages WHERE id = ?" % (name_col, mime_col),
            (package_id,),
        ).fetchone()
    mime = (meta["mime"] if meta else "") or mimetypes.guess_type(path)[0] or "application/octet-stream"
    name = (meta["name"] if meta else "") or os.path.basename(path)
    return path, mime, name


def _is_text_update(update):
    message = update.get("message") or {}
    return bool(message.get("text")) and not extract_file(message)


def poll_telegram():
    global _flood_wait
    token = (CONFIG.get("bot_token") or "").strip()
    if not token:
        print("bot token empty, polling disabled")
        return
    try:
        tg_call("deleteWebhook", {"drop_pending_updates": False})
        print("polling started")
    except Exception:
        print("deleteWebhook failed")
        time.sleep(3)
    offset = None
    while True:
        query = {"timeout": 1, "allowed_updates": json.dumps(["message"])}
        if offset is not None:
            query["offset"] = offset
        try:
            updates = tg_call("getUpdates", query=query, timeout=8) or []
        except TelegramBusy as exc:
            print("poll busy", exc.retry_after)
            time.sleep(exc.retry_after)
            continue
        except Exception as exc:
            print("poll error", type(exc).__name__)
            time.sleep(2)
            continue
        answered = set()
        for update in updates:
            if not _is_text_update(update):
                continue
            result = handle_update(update)
            update_id = update.get("update_id")
            if result == "retry":
                break
            if isinstance(update_id, int):
                answered.add(update_id)
        for update in updates:
            update_id = update.get("update_id")
            if update_id not in answered:
                result = handle_update(update)
                if result == "retry":
                    wait = max(_flood_wait, 1)
                    _flood_wait = 0
                    print("retry in", wait)
                    time.sleep(wait)
                    break
            if isinstance(update_id, int):
                offset = update_id + 1


def ensure_secret():
    global CONFIG
    if CONFIG.get("webhook_secret"):
        return
    CONFIG["webhook_secret"] = secrets.token_hex(32)
    save_config(CONFIG)


class ReuseServer(ThreadingHTTPServer):
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("http", self.command, self.path.split("?", 1)[0], args[1] if len(args) > 1 else "")

    def _send(self, code, body, content_type, extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in extra or []:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def _query(self):
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    def do_GET(self):
        path, query = self._query()
        if path in ("/", "/index.html"):
            try:
                with open(INDEX_PATH, "rb") as f:
                    body = f.read()
            except OSError:
                self._send(404, "Страница не найдена", "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")
            return
        if path == "/api/health":
            self._json(200, {"ok": True, "bot": bool((CONFIG.get("bot_token") or "").strip())})
            return
        if path == "/api/packages":
            self._packages(query)
            return
        match = re.fullmatch(r"/files/(\d+)/(loading|unloading)", path)
        if match:
            self._file(int(match.group(1)), match.group(2))
            return
        self._send(404, "Не найдено", "text/plain; charset=utf-8")

    def _packages(self, query):
        raw_driver = (query.get("driver") or [""])[0].strip()
        raw_day = (query.get("date") or [""])[0].strip()
        driver_id = None
        if raw_driver:
            if not raw_driver.isdigit():
                self._json(400, {"ok": False, "error": "driver"})
                return
            driver_id = int(raw_driver)
        if raw_day and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_day):
            self._json(400, {"ok": False, "error": "date"})
            return
        try:
            packages = list_packages(driver_id, raw_day or None)
            drivers = list_drivers()
        except ValueError:
            self._json(400, {"ok": False, "error": "date"})
            return
        self._json(200, {"ok": True, "drivers": drivers, "packages": packages})

    def _file(self, package_id, slot):
        path, mime, name = resolve_stored_file(package_id, slot)
        if not path:
            self._send(404, "Файл не найден", "text/plain; charset=utf-8")
            return
        inline = mime in PREVIEW_MIME
        disp = "inline" if inline else "attachment"
        filename = urllib.parse.quote(name)
        try:
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(size))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Disposition", "%s; filename*=UTF-8''%s" % (disp, filename))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except Exception as exc:
            print("file send", type(exc).__name__)

    def do_POST(self):
        path, _query = self._query()
        if path != "/telegram/webhook":
            self._json(404, {"ok": False})
            return
        secret = CONFIG.get("webhook_secret") or ""
        header = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secret or not hmac_equal(header, secret):
            self._json(403, {"ok": False})
            return
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0 or length > 2_000_000:
            self._json(400, {"ok": False})
            return
        raw = self.rfile.read(length)
        try:
            update = json.loads(raw.decode("utf-8"))
        except Exception:
            self._json(400, {"ok": False})
            return
        result = handle_update(update)
        if result == "retry":
            self._json(500, {"ok": False})
            return
        self._json(200, {"ok": True})


def hmac_equal(left, right):
    import hmac
    return hmac.compare_digest(left or "", right or "")


def main():
    init_db()
    ensure_secret()
    threading.Thread(target=poll_telegram, daemon=True).start()
    server = ReuseServer(("0.0.0.0", PORT), Handler)
    print("listening", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
