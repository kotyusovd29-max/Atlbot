#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Колесо фортуны для amoCRM.
Сервер без внешних зависимостей: только стандартная библиотека Python 3.

- Менеджер логинится почтой из amoCRM (пароль = его почта).
- Когда менеджер переводит сделку в этап «Успешно реализовано» (status_id=142),
  amoCRM шлёт вебхук на POST /webhook/amocrm — менеджеру начисляется 1 коин.
  Коин за одну сделку начисляется только один раз (защита от повторов).
- 1 коин = 1 прокрут колеса фортуны. Приз выбирает сервер криптостойким
  рандомайзером (secrets.randbelow) пропорционально процентам, которые
  задаёт админ.
- Админы (config.json -> admins) управляют призами и видят историю.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DB_PATH = os.path.join(BASE_DIR, "data.sqlite")
INDEX_PATH = os.path.join(BASE_DIR, "index.html")
PORT = int(os.environ.get("PORT", "8000"))

DEFAULT_CONFIG = {
    "amocrm": {
        "subdomain": "",
        "client_id": "",
        "client_secret": "",
        "access_token": "",
        "refresh_token": "",
        "success_status_id": 142
    },
    "admins": [
        "dmitryikotusov@f5.com.ru",
        "orlov@f5.com.ru"
    ],
    "session_secret": ""
}

DEFAULT_PRIZES = [
    ("5 коинов", 5, "#e74c3c"),
    ("Кофе на компанию", 25, "#f39c12"),
    ("Мерч компании", 15, "#27ae60"),
    ("Премиум-лид", 20, "#2980b9"),
    ("Полдня без задач", 10, "#8e44ad"),
    ("Повезёт в следующий раз", 25, "#7f8c8d"),
]

_db_lock = threading.Lock()


# ---------------------------------------------------------------- config

def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        print("config read error:", e)
    changed = False
    if not cfg.get("session_secret"):
        cfg["session_secret"] = secrets.token_hex(32)
        changed = True
    if changed or not os.path.exists(CONFIG_PATH):
        save_config(cfg)
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


CONFIG = load_config()


# ---------------------------------------------------------------- db

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with _db_lock, db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE,
            name TEXT DEFAULT '',
            amo_user_id INTEGER UNIQUE,
            is_admin INTEGER DEFAULT 0,
            created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS awards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER UNIQUE,
            user_id INTEGER,
            created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS prizes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            weight REAL DEFAULT 0,
            color TEXT DEFAULT '#888888',
            position INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS spins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            prize_id INTEGER,
            prize_name TEXT DEFAULT '',
            created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS bonuses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            comment TEXT DEFAULT '',
            created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS xp_grants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            comment TEXT DEFAULT '',
            created_at INTEGER
        );
        """)
        try:
            conn.execute("ALTER TABLE spins ADD COLUMN prize_name TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        if conn.execute("SELECT COUNT(*) c FROM prizes").fetchone()["c"] == 0:
            for i, (name, weight, color) in enumerate(DEFAULT_PRIZES):
                conn.execute(
                    "INSERT INTO prizes(name, weight, color, position) VALUES(?,?,?,?)",
                    (name, weight, color, i),
                )


def is_admin_email(email):
    admins = [a.strip().lower() for a in CONFIG.get("admins", [])]
    return (email or "").strip().lower() in admins


def get_or_create_user(conn, email=None, amo_user_id=None, name=""):
    row = None
    if email:
        row = conn.execute("SELECT * FROM users WHERE email=?",
                           (email.strip().lower(),)).fetchone()
    if row is None and amo_user_id:
        row = conn.execute("SELECT * FROM users WHERE amo_user_id=?",
                           (amo_user_id,)).fetchone()
    if row:
        changed = {}
        if name and not row["name"]:
            changed["name"] = name
        if email and not row["email"]:
            changed["email"] = email.strip().lower()
        if amo_user_id and not row["amo_user_id"]:
            changed["amo_user_id"] = amo_user_id
        if is_admin_email(email or row["email"]) and not row["is_admin"]:
            changed["is_admin"] = 1
        for k, v in changed.items():
            conn.execute("UPDATE users SET %s=? WHERE id=?" % k, (v, row["id"]))
        return conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
    email = (email or "").strip().lower() or None
    admin = 1 if is_admin_email(email) else 0
    cur = conn.execute(
        "INSERT INTO users(email, name, amo_user_id, is_admin, created_at) VALUES(?,?,?,?,?)",
        (email, name, amo_user_id, admin, int(time.time())),
    )
    return conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()


def coin_stats(conn, user_id):
    earned = conn.execute("SELECT COUNT(*) c FROM awards WHERE user_id=?",
                          (user_id,)).fetchone()["c"]
    bonus = conn.execute("SELECT COALESCE(SUM(amount), 0) s FROM bonuses WHERE user_id=?",
                         (user_id,)).fetchone()["s"]
    spent = conn.execute("SELECT COUNT(*) c FROM spins WHERE user_id=?",
                         (user_id,)).fetchone()["c"]
    earned += int(bonus or 0)
    return {"earned": earned, "spent": spent, "available": max(0, earned - spent)}


# Медали по образцу лестницы Dota 2: 7 рангов по 5 звёзд и Титан.
# Значки рисует страница сама, чужие картинки не используются.
RANK_TIERS = [
    ("herald", "Herald", "Рекрут", "#d7b56d", "#6f8f4e"),
    ("guardian", "Guardian", "Страж", "#d5dde8", "#7f8c9b"),
    ("crusader", "Crusader", "Рыцарь", "#7ed0cb", "#2f8f8c"),
    ("archon", "Archon", "Герой", "#8dffc0", "#e0b341"),
    ("legend", "Legend", "Легенда", "#f2c14e", "#c0392b"),
    ("ancient", "Ancient", "Властелин", "#c9a6ff", "#6d4ca8"),
    ("divine", "Divine", "Божество", "#8ebfff", "#f0c14a"),
    ("immortal", "Immortal", "Титан", "#ffb347", "#ff5a1f"),
]
DEAL_XP = 100


def rank_ladder():
    levels = []
    xp = 0
    step = 100
    for tier in RANK_TIERS:
        stars = 1 if tier[0] == "immortal" else 5
        for star in range(1, stars + 1):
            levels.append({
                "id": tier[0],
                "name": tier[1],
                "title": tier[2],
                "star": star,
                "stars_max": stars,
                "xp": xp,
                "colors": [tier[3], tier[4]],
            })
            xp += step
            step += 25
    return levels


def user_xp(conn, user_id):
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) s FROM xp_grants WHERE user_id=?",
        (user_id,),
    ).fetchone()
    return max(0, int(row["s"] or 0))


def rank_state(conn, user_id):
    xp = user_xp(conn, user_id)
    levels = rank_ladder()
    current = levels[0]
    nxt = None
    for i, level in enumerate(levels):
        if xp >= level["xp"]:
            current = level
            nxt = levels[i + 1] if i + 1 < len(levels) else None
        else:
            break
    if nxt is None:
        into, span, percent = 1, 1, 100
    else:
        into = xp - current["xp"]
        span = max(1, nxt["xp"] - current["xp"])
        percent = max(0, min(100, int(round(100.0 * into / span))))
    return {
        "xp": xp,
        "id": current["id"],
        "name": current["name"],
        "title": current["title"],
        "star": current["star"],
        "stars_max": current["stars_max"],
        "colors": current["colors"],
        "into": into,
        "span": span,
        "percent": percent,
        "next_xp": None if nxt is None else nxt["xp"],
        "next_title": None if nxt is None else nxt["title"],
        "next_star": None if nxt is None else nxt["star"],
        "maxed": nxt is None,
    }


def active_prizes(conn):
    rows = conn.execute(
        "SELECT * FROM prizes WHERE weight > 0 ORDER BY position, id").fetchall()
    return [dict(r) for r in rows]


def pick_prize(prizes):
    """Качественный рандомайзер: криптостойкий secrets.randbelow,
    выбор пропорционален весам (процентам). Сумма не обязана быть 100."""
    total_units = sum(int(round(p["weight"] * 1000)) for p in prizes)
    if total_units <= 0:
        return None
    r = secrets.randbelow(total_units)
    acc = 0
    for p in prizes:
        acc += int(round(p["weight"] * 1000))
        if r < acc:
            return p
    return prizes[-1]


# ---------------------------------------------------------------- amoCRM

def amo_configured():
    a = CONFIG.get("amocrm", {})
    return bool(a.get("subdomain") and a.get("access_token"))


def amo_request(path):
    a = CONFIG["amocrm"]
    url = "https://%s.amocrm.ru%s" % (a["subdomain"], path)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + a["access_token"],
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401 and refresh_amo_token():
            req = urllib.request.Request(url, headers={
                "Authorization": "Bearer " + CONFIG["amocrm"]["access_token"],
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        raise


def refresh_amo_token():
    a = CONFIG["amocrm"]
    if not (a.get("client_id") and a.get("client_secret") and a.get("refresh_token")):
        return False
    url = "https://%s.amocrm.ru/oauth2/access_token" % a["subdomain"]
    body = json.dumps({
        "client_id": a["client_id"],
        "client_secret": a["client_secret"],
        "grant_type": "refresh_token",
        "refresh_token": a["refresh_token"],
        "redirect_uri": "http://89.169.3.47:8000/",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        CONFIG["amocrm"]["access_token"] = data.get("access_token", "")
        CONFIG["amocrm"]["refresh_token"] = data.get("refresh_token", "")
        save_config(CONFIG)
        return bool(CONFIG["amocrm"]["access_token"])
    except Exception as e:
        print("amo token refresh failed:", e)
        return False


def sync_users_from_amo():
    """Подтягивает менеджеров из amoCRM и создаёт им аккаунты (пароль = почта)."""
    if not amo_configured():
        return {"synced": 0, "reason": "amoCRM не настроен в config.json"}
    synced = 0
    page = 1
    with _db_lock, db() as conn:
        while True:
            data = amo_request("/api/v4/users?limit=250&page=%d" % page)
            users = (data or {}).get("_embedded", {}).get("users", [])
            if not users:
                break
            for u in users:
                email = (u.get("email") or "").strip().lower()
                get_or_create_user(conn, email=email or None,
                                   amo_user_id=u.get("id"),
                                   name=u.get("name") or "")
                synced += 1
            page += 1
            if page > 20:
                break
    return {"synced": synced}


# ---------------------------------------------------------------- sessions

def sign(data: bytes) -> str:
    return hmac.new(CONFIG["session_secret"].encode(), data,
                    hashlib.sha256).hexdigest()


def make_session(user_id):
    payload = json.dumps({"uid": user_id, "exp": int(time.time()) + 7 * 86400},
                         separators=(",", ":")).encode()
    b64 = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return b64 + "." + sign(payload)


def read_session(token):
    try:
        b64, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
        if not hmac.compare_digest(sign(payload), sig):
            return None
        data = json.loads(payload)
        if data.get("exp", 0) < time.time():
            return None
        return int(data["uid"])
    except Exception:
        return None


# ---------------------------------------------------------------- webhook

def parse_amo_webhook(raw: bytes):
    """amoCRM шлёт form-encoded: leads[status][0][id], [status_id],
    [responsible_user_id]. На всякий случай понимаем и JSON."""
    text = raw.decode("utf-8", "replace").strip()
    leads = {}
    if text.startswith("{"):
        try:
            data = json.loads(text)
            for section in ("status", "update", "add"):
                for item in (data.get("leads", {}).get(section) or []):
                    leads[str(item.get("id"))] = item
        except Exception:
            pass
    else:
        qs = urllib.parse.parse_qs(text)
        pat = re.compile(r"^leads\[(?:status|update|add)\]\[(\d+)\]\[(\w+)\]$")
        for key, values in qs.items():
            m = pat.match(key)
            if not m:
                continue
            idx, field = m.group(1), m.group(2)
            leads.setdefault(idx, {})[field] = values[0]
    return list(leads.values())


def process_webhook(raw: bytes):
    success_id = int(CONFIG.get("amocrm", {}).get("success_status_id", 142))
    awarded = []
    with _db_lock, db() as conn:
        for lead in parse_amo_webhook(raw):
            try:
                status_id = int(lead.get("status_id") or 0)
                lead_id = int(lead.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if status_id != success_id or not lead_id:
                continue
            try:
                responsible = int(lead.get("responsible_user_id") or 0)
            except (TypeError, ValueError):
                responsible = 0
            if not responsible:
                continue
            user = get_or_create_user(conn, amo_user_id=responsible)
            cur = conn.execute(
                "INSERT OR IGNORE INTO awards(lead_id, user_id, created_at) VALUES(?,?,?)",
                (lead_id, user["id"], int(time.time())),
            )
            if cur.rowcount:
                conn.execute(
                    "INSERT INTO xp_grants(user_id, amount, comment, created_at) VALUES(?,?,?,?)",
                    (user["id"], DEAL_XP, "Успешная сделка #%s" % lead_id, int(time.time())),
                )
                awarded.append({"lead_id": lead_id, "user_id": user["id"]})
    return awarded


# ---------------------------------------------------------------- http

class ReuseServer(ThreadingHTTPServer):
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AmoWheel/1.0"

    def log_message(self, fmt, *args):
        pass

    # -------- helpers

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8",
              extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._headers_sent = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, data, code=200, extra_headers=None):
        self._send(code, json.dumps(data, ensure_ascii=False),
                   "application/json; charset=utf-8", extra_headers)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(min(length, 5 * 1024 * 1024)) if length else b""

    def _json_body(self):
        try:
            return json.loads(self._body().decode("utf-8") or "{}")
        except Exception:
            return {}

    def _current_user(self, conn):
        cookie = self.headers.get("Cookie") or ""
        token = None
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("session="):
                token = part[len("session="):]
                break
        if not token:
            return None
        uid = read_session(urllib.parse.unquote(token))
        if not uid:
            return None
        return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

    def _require_user(self, conn):
        user = self._current_user(conn)
        if user is None:
            self._json({"error": "unauthorized"}, 401)
        return user

    # -------- routes

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/" or path == "/index.html":
            with open(INDEX_PATH, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return
        with db() as conn:
            if path == "/api/me":
                user = self._require_user(conn)
                if user is None:
                    return
                self._json({
                    "email": user["email"],
                    "name": user["name"],
                    "is_admin": bool(user["is_admin"]),
                    "coins": coin_stats(conn, user["id"]),
                    "rank": rank_state(conn, user["id"]),
                    "amo_connected": amo_configured(),
                })
            elif path == "/api/prizes":
                user = self._require_user(conn)
                if user is None:
                    return
                self._json({"prizes": active_prizes(conn)})
            elif path == "/api/history":
                user = self._require_user(conn)
                if user is None:
                    return
                if user["is_admin"]:
                    rows = conn.execute(
                        """SELECT s.created_at, u.email, u.name, s.prize_name AS prize
                           FROM spins s JOIN users u ON u.id=s.user_id
                           ORDER BY s.id DESC LIMIT 100""").fetchall()
                else:
                    rows = conn.execute(
                        """SELECT s.created_at, u.email, u.name, s.prize_name AS prize
                           FROM spins s JOIN users u ON u.id=s.user_id
                           WHERE s.user_id=? ORDER BY s.id DESC LIMIT 50""",
                        (user["id"],)).fetchall()
                self._json({"history": [dict(r) for r in rows]})
            elif path == "/api/users":
                user = self._require_user(conn)
                if user is None:
                    return
                if not user["is_admin"]:
                    self._json({"error": "forbidden"}, 403)
                    return
                rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
                out = []
                for r in rows:
                    st = coin_stats(conn, r["id"])
                    rk = rank_state(conn, r["id"])
                    out.append({
                        "email": r["email"], "name": r["name"],
                        "is_admin": bool(r["is_admin"]),
                        "amo_user_id": r["amo_user_id"],
                        "rank_title": rk["title"],
                        "rank_star": rk["star"],
                        "rank_id": rk["id"],
                        **st, **{"xp": rk["xp"]},
                    })
                self._json({"users": out})
            else:
                self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/webhook/amocrm":
            awarded = process_webhook(self._body())
            self._json({"ok": True, "awarded": awarded})
            return
        with db() as conn:
            if path == "/api/login":
                data = self._json_body()
                email = (data.get("email") or "").strip().lower()
                password = (data.get("password") or "").strip().lower()
                if not email or "@" not in email:
                    self._json({"error": "Введите почту из amoCRM"}, 400)
                    return
                if password != email:
                    self._json({"error": "Пароль — это ваша почта из amoCRM"}, 403)
                    return
                with _db_lock:
                    user = get_or_create_user(conn, email=email)
                token = make_session(user["id"])
                self._json({"ok": True}, 200, {
                    "Set-Cookie": "session=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d"
                                  % (urllib.parse.quote(token), 7 * 86400)
                })
            elif path == "/api/logout":
                self._json({"ok": True}, 200, {
                    "Set-Cookie": "session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
                })
            elif path == "/api/spin":
                user = self._require_user(conn)
                if user is None:
                    return
                with _db_lock:
                    stats = coin_stats(conn, user["id"])
                    if stats["available"] <= 0:
                        self._json({"error": "no_coins",
                                    "message": "Нет коинов. Закройте сделку в успех!"},
                                   403)
                        return
                    prizes = active_prizes(conn)
                    if not prizes:
                        self._json({"error": "no_prizes",
                                    "message": "Призы не настроены"}, 409)
                        return
                    prize = pick_prize(prizes)
                    cur = conn.execute(
                        "INSERT INTO spins(user_id, prize_id, prize_name, created_at) VALUES(?,?,?,?)",
                        (user["id"], prize["id"], prize["name"], int(time.time())),
                    )
                total = sum(p["weight"] for p in prizes)
                # rotation: указатель сверху (-90°). Сектора рисуются от -90° по часовой.
                start = 0.0
                rotation = 0.0
                for p in prizes:
                    size = 360.0 * p["weight"] / total
                    if p["id"] == prize["id"]:
                        margin = size * 0.12
                        point = start + margin + secrets.randbelow(
                            max(1, int((size - 2 * margin) * 1000))) / 1000.0
                        rotation = (360.0 - point) % 360.0
                        break
                    start += size
                stats = coin_stats(conn, user["id"])
                self._json({
                    "ok": True,
                    "spin_id": cur.lastrowid,
                    "prize": {"id": prize["id"], "name": prize["name"],
                              "color": prize["color"]},
                    "rotation": round(rotation, 3),
                    "coins": stats,
                })
            elif path == "/api/prizes":
                user = self._require_user(conn)
                if user is None:
                    return
                if not user["is_admin"]:
                    self._json({"error": "forbidden"}, 403)
                    return
                data = self._json_body()
                items = data.get("prizes")
                if not isinstance(items, list):
                    self._json({"error": "prizes must be a list"}, 400)
                    return
                cleaned = []
                for i, item in enumerate(items[:60]):
                    name = str(item.get("name") or "").strip()[:80]
                    try:
                        weight = float(item.get("weight") or 0)
                    except (TypeError, ValueError):
                        weight = 0
                    weight = max(0.0, min(100000.0, weight))
                    color = str(item.get("color") or "#888888")[:7]
                    if not re.match(r"^#[0-9a-fA-F]{6}$", color):
                        color = "#888888"
                    if name:
                        cleaned.append((name, weight, color, i))
                with _db_lock:
                    conn.execute("DELETE FROM prizes")
                    for name, weight, color, i in cleaned:
                        conn.execute(
                            "INSERT INTO prizes(name, weight, color, position) VALUES(?,?,?,?)",
                            (name, weight, color, i))
                self._json({"ok": True, "prizes": active_prizes(conn)})
            elif path == "/api/sync":
                user = self._require_user(conn)
                if user is None:
                    return
                if not user["is_admin"]:
                    self._json({"error": "forbidden"}, 403)
                    return
                try:
                    self._json(sync_users_from_amo())
                except Exception as e:
                    self._json({"error": str(e)}, 502)
            elif path == "/api/users":
                admin = self._require_user(conn)
                if admin is None:
                    return
                if not admin["is_admin"]:
                    self._json({"error": "forbidden"}, 403)
                    return
                data = self._json_body()
                email = (data.get("email") or "").strip().lower()
                name = str(data.get("name") or "").strip()[:80]
                if "@" not in email:
                    self._json({"error": "Укажите почту пользователя"}, 400)
                    return
                with _db_lock:
                    created = get_or_create_user(conn, email=email, name=name)
                self._json({"ok": True, "email": created["email"], "name": created["name"]})
            elif path == "/api/bonus":
                admin = self._require_user(conn)
                if admin is None:
                    return
                if not admin["is_admin"]:
                    self._json({"error": "forbidden"}, 403)
                    return
                data = self._json_body()
                email = (data.get("email") or "").strip().lower()
                try:
                    amount = int(data.get("amount"))
                except (TypeError, ValueError):
                    amount = 0
                comment = str(data.get("comment") or "").strip()[:200]
                if "@" not in email:
                    self._json({"error": "Укажите почту пользователя"}, 400)
                    return
                if amount < 1 or amount > 1000:
                    self._json({"error": "Количество коинов: от 1 до 1000"}, 400)
                    return
                with _db_lock:
                    target = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                    if target is None:
                        target = get_or_create_user(conn, email=email)
                    conn.execute(
                        "INSERT INTO bonuses(user_id, amount, comment, created_at) VALUES(?,?,?,?)",
                        (target["id"], amount, comment, int(time.time())),
                    )
                    stats = coin_stats(conn, target["id"])
                self._json({"ok": True, "email": email, "coins": stats})
            elif path == "/api/xp":
                admin = self._require_user(conn)
                if admin is None:
                    return
                if not admin["is_admin"]:
                    self._json({"error": "forbidden"}, 403)
                    return
                data = self._json_body()
                email = (data.get("email") or "").strip().lower()
                try:
                    amount = int(data.get("amount"))
                except (TypeError, ValueError):
                    amount = 0
                if "@" not in email:
                    self._json({"error": "Укажите почту пользователя"}, 400)
                    return
                if amount < 1 or amount > 100000:
                    self._json({"error": "Опыт: от 1 до 100000"}, 400)
                    return
                with _db_lock:
                    target = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                    if target is None:
                        target = get_or_create_user(conn, email=email)
                    conn.execute(
                        "INSERT INTO xp_grants(user_id, amount, comment, created_at) VALUES(?,?,?,?)",
                        (target["id"], amount, "Начислил администратор", int(time.time())),
                    )
                    state = rank_state(conn, target["id"])
                self._json({"ok": True, "email": email, "rank": state})
            else:
                self._json({"error": "not found"}, 404)

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except Exception as exc:
            print("request error:", exc)
            if not getattr(self, "_headers_sent", False):
                try:
                    self._json({"error": "server error"}, 500)
                except Exception:
                    pass


def main():
    init_db()
    if amo_configured():
        try:
            print("amo sync:", sync_users_from_amo())
        except Exception as e:
            print("amo sync failed:", e)
    server = ReuseServer(("0.0.0.0", PORT), Handler)
    print("AmoWheel listening on 0.0.0.0:%d" % PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
