#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
設備台帳・QRコード管理サービス

使い方:
    python equipment_ledger.py            # 起動時に「ポート解放 <y/n>」と聞かれます
    python equipment_ledger.py --port 8080

    y : 0.0.0.0 で待ち受け → 同じネットワークの他の端末(スマホ等)からアクセス可能
    n : 127.0.0.1 で待ち受け → このパソコンだけ(ローカルホスト)

データは同じフォルダの equipment.db (SQLite) に保存されます。
操作ログ(いつ・どのIPが・何をしたか)は同じフォルダの ledger.log に保存されます。
QRコード生成に segno を使います。未インストールなら初回起動時に自動で入れます。
"""
import io
import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from contextlib import contextmanager
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------
# 依存ライブラリ (segno: 純Pythonの軽量QRコード生成)
# --------------------------------------------------------------------------
def ensure_segno():
    try:
        import segno  # noqa: F401
        return
    except ImportError:
        pass
    print("QRコード用ライブラリ(segno)をインストールしています...")
    attempts = [
        [sys.executable, "-m", "pip", "install", "segno"],
        [sys.executable, "-m", "pip", "install", "--user", "segno"],
        [sys.executable, "-m", "pip", "install", "--break-system-packages", "segno"],
    ]
    for cmd in attempts:
        try:
            subprocess.check_call(cmd)
            import importlib
            importlib.invalidate_caches()
            import segno  # noqa: F401
            return
        except Exception:
            continue
    print("segno を自動インストールできませんでした。次を実行してから再度起動してください:")
    print("    pip install segno")
    sys.exit(1)


ensure_segno()
import segno  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "equipment.db")
INFO = {"open": False, "port": 8000, "base_url": "http://127.0.0.1:8000"}
DUE_SOON_DAYS = 30
STATUSES = ["稼働中", "停止中", "廃棄"]

# --------------------------------------------------------------------------
# 操作ログ (同じフォルダの ledger.log に保存。2MBごとに5世代までローテーション)
#   書式: 日時 | IPアドレス | 端末 | 操作 | 内容
# --------------------------------------------------------------------------
LOG_PATH = os.path.join(BASE_DIR, "ledger.log")
_ctx = threading.local()
_logger = logging.getLogger("ledger")


def setup_logging():
    _logger.setLevel(logging.INFO)
    _logger.propagate = False
    h = RotatingFileHandler(LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    _logger.addHandler(h)


def device_name(ua):
    ua = ua or ""
    for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                      ("Windows", "Windows"), ("Macintosh", "Mac"), ("Linux", "Linux")):
        if key in ua:
            return name
    return "不明"


def audit(action, detail=""):
    ip = getattr(_ctx, "ip", "-")
    dev = getattr(_ctx, "device", "-")
    detail = str(detail).replace("\n", " ").replace("\r", " ").replace(" | ", " / ")
    _logger.info("%s | %s | %s | %s", ip, dev, action, detail)


# --------------------------------------------------------------------------
# データベース
# --------------------------------------------------------------------------
@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS equipment(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                location TEXT NOT NULL,
                category TEXT DEFAULT '',
                model TEXT DEFAULT '',
                serial TEXT DEFAULT '',
                installed_on TEXT,
                status TEXT DEFAULT '稼働中',
                notes TEXT DEFAULT '',
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS parts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                equipment_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                spec TEXT DEFAULT '',
                quantity INTEGER DEFAULT 1,
                installed_on TEXT,
                last_replaced TEXT,
                interval_months INTEGER,
                notes TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                equipment_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                kind TEXT NOT NULL,
                note TEXT DEFAULT '',
                part_id INTEGER,
                part_name TEXT DEFAULT '',
                prev_replaced TEXT,
                ip TEXT DEFAULT '',
                created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_parts_eq ON parts(equipment_id);
            CREATE INDEX IF NOT EXISTS idx_logs_eq ON logs(equipment_id);
            """
        )
        cols = [r["name"] for r in c.execute("PRAGMA table_info(logs)")]
        if "ip" not in cols:  # 旧バージョンのDBを引き継ぐ
            c.execute("ALTER TABLE logs ADD COLUMN ip TEXT DEFAULT ''")


# --------------------------------------------------------------------------
# ユーティリティ
# --------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, code, msg):
        self.code, self.msg = code, msg


class Raw:
    def __init__(self, body, ctype, headers=None):
        self.body, self.ctype, self.headers = body, ctype, headers or {}


def now():
    return datetime.now().isoformat(timespec="seconds")


def today():
    return date.today()


def add_months(d, m):
    y = d.year + (d.month - 1 + m) // 12
    mo = (d.month - 1 + m) % 12 + 1
    leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
    dim = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mo - 1]
    return date(y, mo, min(d.day, dim))


def to_int(v, default=None):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def clean_date(v):
    v = (v or "").strip() if isinstance(v, str) else ""
    if not v:
        return None
    try:
        date.fromisoformat(v)
    except ValueError:
        raise ApiError(400, "日付の形式が正しくありません")
    return v


def s(v):
    return v.strip() if isinstance(v, str) else ""


def lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


def part_due(p):
    """次回交換予定日(ISO文字列)。前回交換日(なければ設置日)＋交換周期。"""
    base = p.get("last_replaced") or p.get("installed_on")
    iv = p.get("interval_months")
    if not base or not iv:
        return None
    try:
        return add_months(date.fromisoformat(base), int(iv)).isoformat()
    except Exception:
        return None


def enrich_part(row, counts=None):
    p = dict(row)
    nd = part_due(p)
    p["next_due"] = nd
    p["days_left"] = (date.fromisoformat(nd) - today()).days if nd else None
    p["replace_count"] = (counts or {}).get(p["id"], 0)
    return p


def touch(c, eid):
    c.execute("UPDATE equipment SET updated_at=? WHERE id=?", (now(), eid))


# --------------------------------------------------------------------------
# 入力の整形
# --------------------------------------------------------------------------
def eq_clean(b):
    d = {
        "name": s(b.get("name")),
        "location": s(b.get("location")),
        "category": s(b.get("category")),
        "model": s(b.get("model")),
        "serial": s(b.get("serial")),
        "installed_on": clean_date(b.get("installed_on")),
        "status": s(b.get("status")) or "稼働中",
        "notes": s(b.get("notes")),
    }
    if not d["name"]:
        raise ApiError(400, "設備の名称を入力してください")
    if not d["location"]:
        raise ApiError(400, "設置場所を入力してください")
    if d["status"] not in STATUSES:
        d["status"] = "稼働中"
    return d


def part_clean(b):
    name = s(b.get("name"))
    if not name:
        return None
    iv = to_int(b.get("interval_months"))
    return {
        "name": name,
        "spec": s(b.get("spec")),
        "quantity": max(1, to_int(b.get("quantity"), 1)),
        "installed_on": clean_date(b.get("installed_on")),
        "last_replaced": clean_date(b.get("last_replaced")),
        "interval_months": iv if iv and iv > 0 else None,
        "notes": s(b.get("notes")),
    }


def part_insert(c, eid, p):
    cur = c.execute(
        "INSERT INTO parts(equipment_id,name,spec,quantity,installed_on,last_replaced,interval_months,notes)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (eid, p["name"], p["spec"], p["quantity"], p["installed_on"], p["last_replaced"],
         p["interval_months"], p["notes"]),
    )
    return cur.lastrowid


def add_log(c, eid, d, kind, note="", part_id=None, part_name="", prev=None):
    c.execute(
        "INSERT INTO logs(equipment_id,date,kind,note,part_id,part_name,prev_replaced,ip,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (eid, d, kind, note, part_id, part_name, prev, getattr(_ctx, "ip", ""), now()),
    )


def eq_name(c, eid):
    r = c.execute("SELECT name FROM equipment WHERE id=?", (eid,)).fetchone()
    return r["name"] if r else "?"


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
def api_info(qs, b):
    return {**INFO, "statuses": STATUSES}


def api_meta(qs, b):
    with conn() as c:
        locs = [r[0] for r in c.execute(
            "SELECT DISTINCT location FROM equipment WHERE location<>'' ORDER BY location")]
        cats = [r[0] for r in c.execute(
            "SELECT DISTINCT category FROM equipment WHERE category<>'' ORDER BY category")]
    return {"locations": locs, "categories": cats}


def replace_counts(c):
    return {r["part_id"]: r["n"] for r in c.execute(
        "SELECT part_id, COUNT(*) n FROM logs WHERE kind='部品交換' AND part_id IS NOT NULL GROUP BY part_id")}


def api_dashboard(qs, b):
    with conn() as c:
        eqs = [dict(r) for r in c.execute("SELECT * FROM equipment")]
        total = len(eqs)
        active = sum(1 for e in eqs if e["status"] == "稼働中")
        locations = len({e["location"] for e in eqs})
        names = {e["id"]: e for e in eqs}
        counts = replace_counts(c)
        due = []
        for r in c.execute("SELECT * FROM parts"):
            p = enrich_part(r, counts)
            e = names.get(p["equipment_id"])
            if not e or e["status"] == "廃棄":
                continue
            if p["days_left"] is not None and p["days_left"] <= DUE_SOON_DAYS:
                due.append({
                    "part_id": p["id"], "part_name": p["name"], "equipment_id": e["id"],
                    "equipment_name": e["name"], "location": e["location"],
                    "next_due": p["next_due"], "days_left": p["days_left"],
                })
        due.sort(key=lambda x: x["days_left"])
        logs = [dict(r) for r in c.execute(
            "SELECT l.*, e.name AS equipment_name FROM logs l JOIN equipment e ON e.id=l.equipment_id"
            " ORDER BY l.created_at DESC, l.id DESC LIMIT 8")]
    return {
        "counts": {"total": total, "active": active, "locations": locations, "due": len(due)},
        "due": due[:12],
        "logs": logs,
    }


def api_eq_list(qs, b):
    q = (qs.get("q", [""])[0]).strip().lower()
    loc = qs.get("location", [""])[0]
    status = qs.get("status", [""])[0]
    only_due = qs.get("due", [""])[0] == "1"
    tokens = q.split()
    out = []
    with conn() as c:
        parts_by = {}
        for r in c.execute("SELECT * FROM parts ORDER BY id"):
            parts_by.setdefault(r["equipment_id"], []).append(enrich_part(r))
        for e in c.execute("SELECT * FROM equipment ORDER BY updated_at DESC, id DESC"):
            e = dict(e)
            if loc and e["location"] != loc:
                continue
            if status and e["status"] != status:
                continue
            ps = parts_by.get(e["id"], [])
            if tokens:
                hay = " ".join([e["name"], e["location"], e["category"], e["model"],
                                e["serial"], e["notes"]] +
                               [p["name"] + " " + p["spec"] for p in ps]).lower()
                if not all(t in hay for t in tokens):
                    continue
            days = [p["days_left"] for p in ps if p["days_left"] is not None and p["days_left"] <= DUE_SOON_DAYS]
            alert = min(days) if days and e["status"] != "廃棄" else None
            if only_due and alert is None:
                continue
            out.append({
                "id": e["id"], "name": e["name"], "location": e["location"],
                "category": e["category"], "status": e["status"],
                "parts": [p["name"] for p in ps], "alert": alert,
            })
    return out


def get_detail(c, eid):
    e = c.execute("SELECT * FROM equipment WHERE id=?", (eid,)).fetchone()
    if not e:
        raise ApiError(404, "設備が見つかりません")
    counts = replace_counts(c)
    parts = [enrich_part(r, counts) for r in c.execute(
        "SELECT * FROM parts WHERE equipment_id=? ORDER BY id", (eid,))]
    logs = [dict(r) for r in c.execute(
        "SELECT * FROM logs WHERE equipment_id=? ORDER BY date DESC, id DESC", (eid,))]
    d = dict(e)
    d["parts"], d["logs"] = parts, logs
    return d


def api_eq_get(qs, b, eid):
    with conn() as c:
        return get_detail(c, int(eid))


def api_eq_create(qs, b):
    d = eq_clean(b)
    with conn() as c:
        cur = c.execute(
            "INSERT INTO equipment(name,location,category,model,serial,installed_on,status,notes,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (d["name"], d["location"], d["category"], d["model"], d["serial"], d["installed_on"],
             d["status"], d["notes"], now(), now()),
        )
        eid = cur.lastrowid
        for p in b.get("parts") or []:
            pc = part_clean(p)
            if pc:
                part_insert(c, eid, pc)
        add_log(c, eid, today().isoformat(), "登録", "設備を登録しました")
        n_parts = len(c.execute("SELECT 1 FROM parts WHERE equipment_id=?", (eid,)).fetchall())
        audit("設備を追加", f"#{eid}「{d['name']}」 場所:{d['location']} 部品{n_parts}件")
    return {"id": eid}


def api_eq_update(qs, b, eid):
    d = eq_clean(b)
    with conn() as c:
        if not c.execute("SELECT 1 FROM equipment WHERE id=?", (eid,)).fetchone():
            raise ApiError(404, "設備が見つかりません")
        c.execute(
            "UPDATE equipment SET name=?,location=?,category=?,model=?,serial=?,installed_on=?,status=?,notes=?,updated_at=?"
            " WHERE id=?",
            (d["name"], d["location"], d["category"], d["model"], d["serial"], d["installed_on"],
             d["status"], d["notes"], now(), eid),
        )
        audit("設備を編集", f"#{eid}「{d['name']}」 場所:{d['location']} 状態:{d['status']}")
    return {"ok": True}


def api_eq_delete(qs, b, eid):
    with conn() as c:
        name = eq_name(c, eid)
        c.execute("DELETE FROM logs WHERE equipment_id=?", (eid,))
        c.execute("DELETE FROM parts WHERE equipment_id=?", (eid,))
        c.execute("DELETE FROM equipment WHERE id=?", (eid,))
        audit("設備を削除", f"#{eid}「{name}」")
    return {"ok": True}


def api_part_create(qs, b, eid):
    p = part_clean(b)
    if not p:
        raise ApiError(400, "部品の名称を入力してください")
    with conn() as c:
        if not c.execute("SELECT 1 FROM equipment WHERE id=?", (eid,)).fetchone():
            raise ApiError(404, "設備が見つかりません")
        pid = part_insert(c, int(eid), p)
        add_log(c, int(eid), today().isoformat(), "部品追加", "", pid, p["name"])
        touch(c, eid)
        audit("部品を追加", f"設備 #{eid}「{eq_name(c, eid)}」に部品「{p['name']}」")
    return {"id": pid}


def api_part_update(qs, b, pid):
    p = part_clean(b)
    if not p:
        raise ApiError(400, "部品の名称を入力してください")
    with conn() as c:
        row = c.execute("SELECT equipment_id FROM parts WHERE id=?", (pid,)).fetchone()
        if not row:
            raise ApiError(404, "部品が見つかりません")
        c.execute(
            "UPDATE parts SET name=?,spec=?,quantity=?,installed_on=?,last_replaced=?,interval_months=?,notes=? WHERE id=?",
            (p["name"], p["spec"], p["quantity"], p["installed_on"], p["last_replaced"],
             p["interval_months"], p["notes"], pid),
        )
        touch(c, row["equipment_id"])
        audit("部品を編集", f"設備 #{row['equipment_id']}「{eq_name(c, row['equipment_id'])}」 部品「{p['name']}」")
    return {"ok": True}


def api_part_delete(qs, b, pid):
    with conn() as c:
        row = c.execute("SELECT * FROM parts WHERE id=?", (pid,)).fetchone()
        if row:
            c.execute("DELETE FROM parts WHERE id=?", (pid,))
            c.execute("UPDATE logs SET part_id=NULL WHERE part_id=?", (pid,))
            touch(c, row["equipment_id"])
            audit("部品を削除", f"設備 #{row['equipment_id']}「{eq_name(c, row['equipment_id'])}」 部品「{row['name']}」")
    return {"ok": True}


def api_part_replace(qs, b, pid):
    d = clean_date(b.get("date")) or today().isoformat()
    note = s(b.get("note"))
    with conn() as c:
        row = c.execute("SELECT * FROM parts WHERE id=?", (pid,)).fetchone()
        if not row:
            raise ApiError(404, "部品が見つかりません")
        add_log(c, row["equipment_id"], d, "部品交換", note, row["id"], row["name"], row["last_replaced"])
        # 過去日で記録した場合は、より新しい交換日を上書きしない
        if not row["last_replaced"] or d >= row["last_replaced"]:
            c.execute("UPDATE parts SET last_replaced=? WHERE id=?", (d, pid))
        touch(c, row["equipment_id"])
        audit("部品を交換", f"設備 #{row['equipment_id']}「{eq_name(c, row['equipment_id'])}」 部品「{row['name']}」 交換日:{d}"
                         + (f" メモ:{note}" if note else ""))
    return {"ok": True}


def api_log_create(qs, b, eid):
    d = clean_date(b.get("date")) or today().isoformat()
    kind = s(b.get("kind")) or "メモ"
    note = s(b.get("note"))
    if not note:
        raise ApiError(400, "内容を入力してください")
    with conn() as c:
        if not c.execute("SELECT 1 FROM equipment WHERE id=?", (eid,)).fetchone():
            raise ApiError(404, "設備が見つかりません")
        add_log(c, int(eid), d, kind, note)
        touch(c, eid)
        audit(f"記録を追加({kind})", f"設備 #{eid}「{eq_name(c, eid)}」 {note}")
    return {"ok": True}


def api_log_delete(qs, b, lid):
    with conn() as c:
        row = c.execute("SELECT * FROM logs WHERE id=?", (lid,)).fetchone()
        if row:
            if row["kind"] == "部品交換" and row["part_id"]:
                p = c.execute("SELECT * FROM parts WHERE id=?", (row["part_id"],)).fetchone()
                if p and p["last_replaced"] == row["date"]:
                    c.execute("UPDATE parts SET last_replaced=? WHERE id=?", (row["prev_replaced"], p["id"]))
            c.execute("DELETE FROM logs WHERE id=?", (lid,))
            touch(c, row["equipment_id"])
            audit("履歴を削除", f"設備 #{row['equipment_id']}「{eq_name(c, row['equipment_id'])}」 {row['kind']} {row['part_name'] or ''} {row['date']}")
    return {"ok": True}


def api_qr(qs, b, eid):
    base = (qs.get("base", [""])[0] or INFO["base_url"]).strip().rstrip("/")
    if not re.match(r"^https?://", base):
        raise ApiError(400, "ベースURLは http:// または https:// で始めてください")
    url = f"{base}/e/{int(eid)}"
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=1, border=1, xmldecl=False,
                                     omitsize=True, dark="#1d1d1f")
    return Raw(buf.getvalue(), "image/svg+xml")


ROUTES = [
    ("GET", r"/api/info", api_info),
    ("GET", r"/api/meta", api_meta),
    ("GET", r"/api/dashboard", api_dashboard),
    ("GET", r"/api/equipment", api_eq_list),
    ("POST", r"/api/equipment", api_eq_create),
    ("GET", r"/api/equipment/(\d+)", api_eq_get),
    ("PUT", r"/api/equipment/(\d+)", api_eq_update),
    ("DELETE", r"/api/equipment/(\d+)", api_eq_delete),
    ("POST", r"/api/equipment/(\d+)/parts", api_part_create),
    ("POST", r"/api/equipment/(\d+)/logs", api_log_create),
    ("PUT", r"/api/parts/(\d+)", api_part_update),
    ("DELETE", r"/api/parts/(\d+)", api_part_delete),
    ("POST", r"/api/parts/(\d+)/replace", api_part_replace),
    ("DELETE", r"/api/logs/(\d+)", api_log_delete),
    ("GET", r"/api/qr/(\d+)\.svg", api_qr),
]


# --------------------------------------------------------------------------
# HTTPサーバー
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "EquipmentLedger/1.0"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except ValueError:
            raise ApiError(400, "リクエストの形式が正しくありません")

    def _handle(self, method):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        _ctx.ip = self.client_address[0]
        _ctx.device = device_name(self.headers.get("User-Agent"))
        try:
            if method == "GET" and not path.startswith("/api/"):
                audit("画面を開く", path)
                return self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            body = self._read_json() if method in ("POST", "PUT") else {}
            for m, pat, fn in ROUTES:
                if m != method:
                    continue
                mm = re.fullmatch(pat, path)
                if mm:
                    res = fn(qs, body, *mm.groups())
                    if isinstance(res, Raw):
                        return self._send(200, res.body, res.ctype, res.headers)
                    return self._send(200, res)
            raise ApiError(404, "見つかりません")
        except ApiError as e:
            if method != "GET":
                audit("失敗", f"{method} {path} → {e.msg}")
            self._send(e.code, {"error": e.msg})
        except Exception as e:  # noqa: BLE001
            print("ERROR:", repr(e))
            audit("サーバーエラー", f"{method} {path} → {e!r}")
            self._send(500, {"error": "サーバーでエラーが発生しました"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


# --------------------------------------------------------------------------
# フロントエンド (1ファイルのSPA)
# --------------------------------------------------------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>設備台帳</title>
<style>
:root{
  color-scheme:light dark;
  --bg:#f5f5f7; --card:#ffffff; --ink:#1d1d1f; --sub:#6e6e73; --faint:#aeaeb2;
  --line:rgba(0,0,0,.09); --fill:rgba(118,118,128,.12); --seg-on:#ffffff;
  --glass:rgba(245,245,247,.80);
  --blue:#0071e3; --blue-tint:rgba(0,113,227,.10); --ring:rgba(0,113,227,.35);
  --ok:#248a3d; --ok-bg:rgba(52,199,89,.15);
  --warn:#b25000; --warn-bg:rgba(255,149,0,.16);
  --bad:#d70015; --bad-bg:rgba(255,59,48,.12);
  --r:20px;
  --font:-apple-system,BlinkMacSystemFont,"SF Pro Text","Hiragino Sans","Hiragino Kaku Gothic ProN","Noto Sans JP","Yu Gothic UI",Meiryo,sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#000000; --card:#1c1c1e; --ink:#f5f5f7; --sub:#98989d; --faint:#636366;
  --line:rgba(255,255,255,.14); --seg-on:#636366; --glass:rgba(0,0,0,.72);
  --blue:#2997ff; --blue-tint:rgba(41,151,255,.18); --ring:rgba(41,151,255,.45);
  --ok:#30d158; --ok-bg:rgba(48,209,88,.18);
  --warn:#ff9f0a; --warn-bg:rgba(255,159,10,.18);
  --bad:#ff453a; --bad-bg:rgba(255,69,58,.18);
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;touch-action:manipulation}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--font);font-size:16px;line-height:1.6;letter-spacing:.01em;-webkit-font-smoothing:antialiased;overflow-x:hidden}
a{color:inherit;text-decoration:none;-webkit-tap-highlight-color:transparent}
button,input,select,textarea{font:inherit;color:inherit;letter-spacing:inherit;-webkit-tap-highlight-color:transparent}
/* iOSは16px未満の入力欄で自動ズームするため16px固定 */
input,select,textarea{font-size:16px}
:focus-visible{outline:3px solid var(--ring);outline-offset:2px}
h1,h2,h3{margin:0;font-weight:700;letter-spacing:-.01em}

/* ---- ヘッダー ---- */
header.top{position:sticky;top:0;z-index:20;background:var(--glass);backdrop-filter:saturate(180%) blur(20px);-webkit-backdrop-filter:saturate(180%) blur(20px);border-bottom:1px solid var(--line);padding-top:env(safe-area-inset-top)}
.bar{max-width:1080px;margin:0 auto;padding:0 20px;height:52px;display:flex;align-items:center;justify-content:space-between}
.logo{font-weight:600;font-size:17px;letter-spacing:.02em}
nav{display:flex;gap:2px}
nav a{padding:6px 14px;border-radius:99px;color:var(--sub);font-size:14px;transition:color .15s,background .15s}
nav a:hover{color:var(--ink)}
nav a.on{color:var(--ink);background:var(--fill)}
main{max-width:1080px;margin:0 auto;padding:36px 20px 96px}
.ptitle{font-size:clamp(30px,5vw,40px);margin:4px 0 26px;letter-spacing:-.02em}
.sub{color:var(--sub)}

/* ---- ホーム ---- */
.hero{padding:48px 0 36px;text-align:center}
.hero h1{font-size:clamp(38px,8vw,68px);font-weight:700;letter-spacing:-.02em;line-height:1.2}
.hero p{margin:12px 0 30px;color:var(--sub);font-size:clamp(17px,2.4vw,21px)}
.searchbar{display:flex;align-items:center;gap:10px;max-width:600px;margin:0 auto;background:var(--card);border-radius:16px;padding:6px 6px 6px 16px;box-shadow:0 0 0 1px var(--line);transition:box-shadow .2s}
.searchbar::before{content:"";flex:none;width:18px;height:18px;background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%238e8e93' stroke-width='2' stroke-linecap='round'%3E%3Ccircle cx='11' cy='11' r='7'/%3E%3Cpath d='m20 20-3.5-3.5'/%3E%3C/svg%3E") center/contain no-repeat}
.searchbar:focus-within{box-shadow:0 0 0 4px var(--ring)}
.searchbar input{flex:1;width:auto;min-width:0;border:0;background:transparent;outline:0;box-shadow:none!important;padding:8px 0;min-height:0;border-radius:0;font-size:17px}
.searchbar.big{max-width:none;margin:0 0 14px}
.actions{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:0 0 56px}
.action{background:var(--card);border-radius:24px;padding:26px 24px;display:flex;flex-direction:column;gap:2px;min-height:178px;transition:transform .15s}
.action:active{transform:scale(.98)}
.action .ic{width:46px;height:46px;border-radius:12px;display:flex;align-items:center;justify-content:center;margin-bottom:auto}
.action .ic svg.i{width:24px;height:24px;stroke-width:1.7}
.action:nth-child(1) .ic{background:var(--blue-tint);color:var(--blue)}
.action:nth-child(2) .ic{background:var(--ok-bg);color:var(--ok)}
.action:nth-child(3) .ic{background:var(--warn-bg);color:var(--warn)}
.action b{font-size:20px;font-weight:700;letter-spacing:-.01em}
.action small{color:var(--sub);font-size:14px}
svg.i{width:30px;height:30px;fill:none;stroke:currentColor;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.stats{display:flex;gap:44px;flex-wrap:wrap;margin-bottom:56px;justify-content:center}
.stat{text-align:center}
.stat b{display:block;font-size:40px;font-weight:700;line-height:1.15;letter-spacing:-.02em}
.stat span{color:var(--sub);font-size:13px}
.sec{margin:0 0 48px}
.sec>h2{font-size:24px;margin-bottom:14px;letter-spacing:-.015em}

/* ---- リスト (iOSのグループ表示) ---- */
.list{background:var(--card);border-radius:var(--r);overflow:hidden}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 20px;position:relative}
.row+.row::before{content:"";position:absolute;top:0;left:20px;right:0;height:1px;background:var(--line)}
a.row{transition:background .12s}
a.row::after{content:"";flex:none;width:8px;height:8px;border-right:2px solid var(--faint);border-top:2px solid var(--faint);transform:rotate(45deg);margin-left:2px}
a.row:active{background:var(--fill)}
.row .t{min-width:0;margin-right:auto}
.row .t b{font-weight:600}
.row .t small{display:block;color:var(--sub);font-size:13px;line-height:1.5}
.empty{padding:40px 20px;text-align:center;color:var(--sub)}
.badge{display:inline-block;font-size:12px;font-weight:500;padding:1px 10px;border-radius:99px;background:var(--fill);color:var(--sub);white-space:nowrap;line-height:1.7}
.badge.ok{background:var(--ok-bg);color:var(--ok)}
.badge.warn{background:var(--warn-bg);color:var(--warn)}
.badge.bad{background:var(--bad-bg);color:var(--bad)}
.badge.off{color:var(--faint)}

/* ---- 検索・カード ---- */
.filters{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:26px}
.filters select{border:0;background-color:var(--card);border-radius:99px;padding:8px 38px 8px 16px;min-height:40px;-webkit-appearance:none;appearance:none;cursor:pointer;font-size:16px;max-width:100%}
.chk{display:flex;gap:8px;align-items:center;color:var(--sub);font-size:14px;cursor:pointer;padding:0 6px}
.chk input,.pick input{accent-color:var(--blue);width:18px;height:18px}
.grid{columns:260px;column-gap:16px}
.card{display:block;break-inside:avoid;background:var(--card);border-radius:var(--r);padding:20px 22px;margin:0 0 16px;transition:transform .15s}
a.card:active{transform:scale(.98)}
.eq-top{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px;min-height:4px}
.card h3{font-size:18px;line-height:1.45}
.card .loc{margin:2px 0 0;color:var(--sub);font-size:14px}
.card .cat{margin:10px 0 0;font-size:13px;color:var(--sub)}
.pv{list-style:none;margin:14px 0 0;padding:14px 0 0;border-top:1px solid var(--line);font-size:13.5px;color:var(--sub)}
.pv li{padding:1px 0}
.pv li.more{opacity:.7}

/* ---- 詳細 ---- */
.detail-head{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;flex-wrap:wrap;margin-bottom:28px}
.detail-head h1{font-size:clamp(28px,5vw,42px);line-height:1.3;letter-spacing:-.02em}
.detail-head .loc{margin:4px 0 12px;color:var(--sub);font-size:17px}
.tools{display:flex;gap:8px;flex-wrap:wrap}
.two{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;margin-bottom:48px}
.panel{background:var(--card);border-radius:var(--r);padding:24px}
dl.kv{margin:0;display:grid;grid-template-columns:auto minmax(0,1fr);gap:10px 22px}
dl.kv dt{color:var(--sub);font-size:14px}
dl.kv dd{margin:0;word-break:break-word}
.sec-h{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
.sec-h h2{font-size:24px;letter-spacing:-.015em}
.part{display:flex;justify-content:space-between;gap:12px 16px;padding:16px 22px;flex-wrap:wrap;position:relative}
.part+.part::before{content:"";position:absolute;top:0;left:22px;right:0;height:1px;background:var(--line)}
.part b{font-weight:600}
.meta{color:var(--sub);font-size:13.5px;margin-top:2px}
.pa{display:flex;gap:2px;align-items:center;flex-wrap:wrap}
.tl{list-style:none;margin:0;padding:0}
.tl li{display:grid;grid-template-columns:96px minmax(0,1fr) auto;gap:14px;padding:14px 22px;align-items:baseline;position:relative}
.tl li+li::before{content:"";position:absolute;top:0;left:22px;right:0;height:1px;background:var(--line)}
.tl time{color:var(--sub);font-size:13px}
.tl .k{font-size:12px;font-weight:500;color:var(--sub);background:var(--fill);border-radius:99px;padding:1px 10px;margin-right:8px;white-space:nowrap}
.tl .k.rep{color:var(--blue);background:var(--blue-tint)}
.ip{color:var(--faint);font-size:11.5px;margin-left:8px;white-space:nowrap}

/* ---- ボタン ---- */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;border:0;background:var(--blue-tint);color:var(--blue);border-radius:99px;padding:10px 22px;min-height:40px;cursor:pointer;font-weight:500;transition:background .15s,opacity .15s,transform .1s;white-space:nowrap}
.btn:active{transform:scale(.97);opacity:.85}
.btn.primary{background:var(--blue);color:#fff}
.btn.primary:hover{opacity:.92}
.btn.ghost{background:transparent;color:var(--sub)}
.btn.sm{padding:6px 16px;min-height:32px;font-size:14px}
.link{border:0;background:none;color:var(--blue);cursor:pointer;padding:6px 8px;font-size:14px}
.link.danger{color:var(--bad)}

/* ---- フォーム ---- */
form.f{display:grid;grid-template-columns:minmax(0,1fr);gap:20px}
.formpanel{max-width:680px;margin:0 auto}
.ptitle.narrow{max-width:680px;margin-left:auto;margin-right:auto}
.fld{display:flex;flex-direction:column;gap:6px;min-width:0}
.fld.full{grid-column:1/-1}
.fld label{font-size:13px;color:var(--sub);padding-left:2px}
.fld label i{color:var(--bad);font-style:normal}
input[type=text],input[type=date],input[type=number],select.in,textarea{display:block;width:100%;max-width:100%;min-width:0;border:0;background:var(--fill);border-radius:12px;padding:12px 14px;min-height:48px;outline:0;transition:box-shadow .15s;-webkit-appearance:none;appearance:none}
textarea{resize:vertical;min-height:96px;line-height:1.5}
input:focus,textarea:focus,select:focus{box-shadow:0 0 0 3px var(--ring)}
input[type=date]{text-align:left;line-height:1.4}
input[type=date]::-webkit-date-and-time-value{text-align:left;min-height:1.4em;margin:0}
input[type=date]::-webkit-datetime-edit{padding:0}
select.in,.filters select{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1.5l5 5 5-5' fill='none' stroke='%238e8e93' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 14px center}
select.in{padding-right:38px}
.prow{display:grid;grid-template-columns:minmax(0,1fr);gap:12px;background:var(--bg);padding:14px;border-radius:16px;margin-bottom:12px}
.prow input{background:var(--card)}
.prow .link{justify-self:end;color:var(--bad);padding:2px 4px}
.prow-h{display:none}
.pc{min-width:0}
.pl{display:block;font-size:12px;color:var(--sub);margin:0 0 4px 2px}
.formbar{display:flex;gap:10px;justify-content:flex-end;margin-top:8px;grid-column:1/-1}
.note{font-size:13px;color:var(--sub);margin-top:2px}
.warnbox{background:var(--warn-bg);color:var(--warn);border-radius:12px;padding:12px 14px;font-size:13.5px;margin-bottom:16px}

/* ---- QR ---- */
.qrlayout{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr);gap:16px;align-items:start;margin-bottom:32px}
.plist{max-height:340px;overflow:auto;margin-top:12px;border-top:1px solid var(--line);overscroll-behavior:contain}
.pick{display:flex;gap:12px;align-items:center;padding:12px 4px;border-bottom:1px solid var(--line);cursor:pointer}
.pick small{display:block;color:var(--sub);font-size:13px}
.pickbar{display:flex;gap:6px;align-items:center;margin-top:12px;flex-wrap:wrap}
.pickbar .cnt{margin-left:auto;color:var(--sub);font-size:13px}
.opts .fld{margin-bottom:18px}
.seg{display:flex;background:var(--fill);border-radius:10px;padding:2px;gap:2px}
.seg label{flex:1;text-align:center;cursor:pointer;font-size:14px;color:var(--ink);padding:0}
.seg input{display:none}
.seg label span{display:block;padding:7px 0;border-radius:8px;transition:background .2s}
.seg input:checked+span{background:var(--seg-on);box-shadow:0 1px 3px rgba(0,0,0,.16);font-weight:600}
.qrgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(calc(var(--qr,160px) + 40px),1fr));gap:16px}
.qrcard{margin:0;background:#fff;color:#1d1d1f;border-radius:var(--r);padding:20px;text-align:center;display:flex;flex-direction:column;align-items:center;gap:6px}
.qrcard img{width:var(--qr,160px);height:var(--qr,160px)}
.qrcard b{font-weight:600;line-height:1.4;font-size:14px}
.qrcard span,.qrcard small{color:#6e6e73;font-size:12px;line-height:1.4}
.qrcard a.dl{font-size:12px;color:#0071e3;margin-top:2px}

/* ---- シート(ダイアログ) ---- */
dialog{border:0;border-radius:28px;padding:0;width:min(480px,calc(100vw - 32px));max-height:90vh;overflow-x:hidden;overflow-y:auto;overscroll-behavior:contain;background:var(--card);color:var(--ink);box-shadow:0 24px 70px rgba(0,0,0,.28)}
dialog:focus,dialog:focus-visible{outline:none}
dialog::backdrop{background:rgba(0,0,0,.35);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px)}
dialog form{padding:26px 24px 22px;display:grid;gap:18px;min-width:0}
dialog h3{font-size:21px;letter-spacing:-.01em}
dialog .g2{display:grid;grid-template-columns:minmax(0,1fr);gap:18px}
.mactions{display:flex;gap:10px;justify-content:flex-end;margin-top:4px}
.toast{position:fixed;left:50%;bottom:calc(28px + env(safe-area-inset-bottom));transform:translateX(-50%);background:rgba(40,40,42,.92);color:#fff;backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);padding:11px 22px;border-radius:99px;font-size:14px;z-index:100;box-shadow:0 8px 30px rgba(0,0,0,.25);max-width:calc(100vw - 32px)}
html.lock{overflow:hidden}
body.lock{position:fixed;left:0;right:0;width:100%;overflow:hidden}
footer.foot{max-width:1080px;margin:0 auto;padding:0 20px 48px;color:var(--sub);font-size:13px;text-align:center}
footer.foot a{color:var(--blue)}

.tabbar{display:none}
@media (max-width:900px),(pointer:coarse){
  header.top nav{display:none}
  .bar{height:48px}
  .tabbar{display:flex;position:fixed;left:0;right:0;bottom:0;z-index:30;background:var(--glass);backdrop-filter:saturate(180%) blur(20px);-webkit-backdrop-filter:saturate(180%) blur(20px);border-top:1px solid var(--line);padding:6px 8px calc(6px + env(safe-area-inset-bottom))}
  .tabbar a{flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;font-size:10.5px;color:var(--sub);padding:4px 0}
  .tabbar a svg{width:26px;height:26px;fill:none;stroke:currentColor;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
  .tabbar a.on{color:var(--blue)}
  main{padding:20px 16px calc(104px + env(safe-area-inset-bottom))}
  footer.foot{padding-bottom:calc(96px + env(safe-area-inset-bottom))}
  .toast{bottom:calc(86px + env(safe-area-inset-bottom))}
  .hero{padding:20px 0 24px}
  .btn{min-height:44px}
  .btn.sm{min-height:38px}
  .link{padding:10px 8px}
  /* メニュー3つは1つのグループリストに */
  .actions{grid-template-columns:1fr;gap:0;background:var(--card);border-radius:var(--r);overflow:hidden;margin-bottom:36px}
  .action{border-radius:0;min-height:0;flex-direction:row;flex-wrap:wrap;align-items:center;column-gap:14px;padding:14px 18px;position:relative;background:none}
  .action+.action::before{content:"";position:absolute;top:0;left:78px;right:0;height:1px;background:var(--line)}
  .action .ic{margin:0;grid-row:1/3}
  .action div{display:flex;flex-direction:column;flex:1;min-width:0}
  .action::after{content:"";width:8px;height:8px;border-right:2px solid var(--faint);border-top:2px solid var(--faint);transform:rotate(45deg)}
  .action:active{transform:none;background:var(--fill)}
  .action b{font-size:17px}
  .stats{gap:26px;margin-bottom:36px}
  .stat b{font-size:32px}
  .two,.qrlayout{grid-template-columns:minmax(0,1fr)}
  .two{margin-bottom:36px}
  form.f{grid-template-columns:minmax(0,1fr)}
  .panel{padding:20px}
  .card{padding:18px 20px}
  .part{padding:16px 18px}
  .part+.part::before{left:18px}
  .tl li{grid-template-columns:minmax(0,1fr) auto;padding:14px 18px;gap:4px 12px}
  .tl li+li::before{left:18px}
  .tl time{grid-column:1/-1}
  .row{padding:14px 18px}
  .row+.row::before{left:18px}
  .pick{padding:14px 4px}
  .formbar .btn{flex:1}
  /* 部品入力は1行ずつカード化して縦に並べる */
  .prow-h{display:none}
  .prow{grid-template-columns:minmax(0,1fr);background:var(--bg);padding:14px;border-radius:16px;margin-bottom:12px;gap:12px}
  .prow input{background:var(--card)}
  .prow .link{justify-self:end;color:var(--bad);padding:2px 4px}
  .pl{display:block;font-size:12px;color:var(--sub);margin:0 0 4px 2px}
  /* ボトムシート */
  dialog{width:100%;max-width:100%;margin:auto 0 0;border-radius:28px 28px 0 0;max-height:92vh}
  dialog::before{content:"";display:block;width:36px;height:5px;border-radius:3px;background:var(--faint);opacity:.6;margin:8px auto 0}
  dialog form{padding:14px 18px calc(18px + env(safe-area-inset-bottom))}
  .mactions .btn{flex:1}
  /* 入力欄はすべて縦1列 */
  .filters{flex-direction:column;align-items:stretch}
  .filters select{width:100%}
  .chk{padding:6px 2px}
}
@media print{
  :root{--bg:#fff;--card:#fff;--ink:#1d1d1f;--sub:#6e6e73;--line:#ddd}
  header.top,.tabbar,.noprint,footer.foot,.ptitle,.toast{display:none!important}
  body{background:#fff}
  main{padding:0;max-width:none}
  .qrgrid{gap:0}
  .qrcard{border-radius:0;border:1px dashed #bbb;break-inside:avoid;padding:12px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>
<header class="top"><div class="bar">
  <a href="/" data-link class="logo">設備台帳</a>
  <nav>
    <a href="/search" data-link data-nav="search">探す</a>
    <a href="/new" data-link data-nav="new">追加</a>
    <a href="/qr" data-link data-nav="qr">QR</a>
  </nav>
</div></header>
<main id="app"></main>
<div class="tabbar" aria-label="メニュー">
  <a href="/" data-link data-nav="home"><svg viewBox="0 0 24 24"><path d="M4 11 12 4l8 7v8a1 1 0 0 1-1 1h-4v-6H9v6H5a1 1 0 0 1-1-1z"/></svg>ホーム</a>
  <a href="/search" data-link data-nav="search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>探す</a>
  <a href="/new" data-link data-nav="new"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>追加</a>
  <a href="/qr" data-link data-nav="qr"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3h-3zM20 14v.01M14 20h.01M17 20h4v-3"/></svg>QR</a>
</div>

<script>
const $ = (s, el=document) => el.querySelector(s);
const $$ = (s, el=document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const todayStr = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; };
const fmtDate = s => s ? s.replaceAll('-', '/') : '';
const ST = {'稼働中':'ok','停止中':'warn','廃棄':'off'};
const IC = {
  search:'<svg class="i" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
  plus:'<svg class="i" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  qr:'<svg class="i" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3h-3zM20 14v.01M14 20h.01M17 20h4v-3"/></svg>'
};
let INFO = {};

async function api(path, o = {}) {
  const r = await fetch('/api' + path, {
    method: o.method || 'GET',
    headers: {'Content-Type': 'application/json'},
    body: o.body ? JSON.stringify(o.body) : undefined
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || 'エラーが発生しました');
  return data;
}
function toast(msg) {
  const t = document.createElement('div'); t.className = 'toast'; t.textContent = msg;
  document.body.appendChild(t); setTimeout(() => t.remove(), 2400);
}
const view = html => { $('#app').innerHTML = html; };

function dueBadge(d) {
  if (d === null || d === undefined) return '';
  if (d < 0) return `<span class="badge bad">交換期限を${-d}日超過</span>`;
  if (d === 0) return `<span class="badge bad">今日が交換期限</span>`;
  if (d <= 30) return `<span class="badge warn">あと${d}日</span>`;
  return `<span class="badge ok">あと${d}日</span>`;
}

/* ---------- ルーター ---------- */
function go(path) { history.pushState({}, '', path); route(); }
document.addEventListener('click', e => {
  const a = e.target.closest('a[data-link]');
  if (a && !e.metaKey && !e.ctrlKey) { e.preventDefault(); go(a.getAttribute('href')); }
});
window.addEventListener('popstate', route);

async function route() {
  const u = new URL(location.href), p = u.pathname; let m;
  $$('[data-nav]').forEach(a => { const n = a.dataset.nav; a.classList.toggle('on', n === 'home' ? p === '/' : p.startsWith('/' + n) || (n === 'search' && p.startsWith('/e/') && !p.endsWith('/edit'))); });
  window.scrollTo(0, 0);
  try {
    if (p === '/') return await viewDashboard();
    if (p === '/search') return await viewSearch(u.searchParams);
    if (p === '/new') return await viewForm();
    if (m = p.match(/^\/e\/(\d+)\/edit$/)) return await viewForm(+m[1]);
    if (m = p.match(/^\/e\/(\d+)$/)) return await viewDetail(+m[1]);
    if (p === '/qr') return await viewQR(u.searchParams);
    view('<div class="empty">ページが見つかりません</div>');
  } catch (err) {
    view(`<div class="empty">${esc(err.message)}<br><br><a class="btn" href="/" data-link>ダッシュボードへ</a></div>`);
  }
}

/* ---------- ダッシュボード ---------- */
async function viewDashboard() {
  const d = await api('/dashboard');
  const c = d.counts;
  view(`
  <section class="hero">
    <h1>設備台帳</h1>
    <p>設備・部品・交換履歴を、QRコードでつなぐ。</p>
    <form class="searchbar" id="hs">
      <input name="q" placeholder="設備名・場所・部品名で検索" autocomplete="off">
      <button class="btn primary">検索</button>
    </form>
  </section>

  <section class="actions">
    <a class="action" href="/search" data-link><span class="ic">${IC.search}</span><div><b>設備を探す</b><small>${c.total}件の設備</small></div></a>
    <a class="action" href="/new" data-link><span class="ic">${IC.plus}</span><div><b>設備を追加</b><small>名称・場所・部品を登録</small></div></a>
    <a class="action" href="/qr" data-link><span class="ic">${IC.qr}</span><div><b>QRコードを作る</b><small>貼り付け用ラベルを印刷</small></div></a>
  </section>

  <div class="stats">
    <div class="stat"><b>${c.total}</b><span>登録設備</span></div>
    <div class="stat"><b>${c.active}</b><span>稼働中</span></div>
    <div class="stat"><b>${c.locations}</b><span>設置場所</span></div>
    <div class="stat"><b>${c.due}</b><span>交換が近い部品</span></div>
  </div>

  <section class="sec">
    <h2>交換時期が近い部品</h2>
    <div class="list">${d.due.length ? d.due.map(x => `
      <a class="row" href="/e/${x.equipment_id}" data-link>
        <div class="t"><b>${esc(x.part_name)}</b><small>${esc(x.equipment_name)}・${esc(x.location)}　目安 ${fmtDate(x.next_due)}</small></div>
        ${dueBadge(x.days_left)}
      </a>`).join('') : '<div class="empty">30日以内に交換時期を迎える部品はありません</div>'}
    </div>
  </section>

  <section class="sec">
    <h2>最近の記録</h2>
    <div class="list">${d.logs.length ? d.logs.map(l => `
      <a class="row" href="/e/${l.equipment_id}" data-link>
        <div class="t"><b>${esc(l.equipment_name)}</b>
          <small>${esc(l.kind)}${l.part_name ? '：' + esc(l.part_name) : ''}${l.note && l.kind !== '登録' ? '　' + esc(l.note) : ''}</small></div>
        <span class="sub" style="font-size:13px">${fmtDate(l.date)}</span>
      </a>`).join('') : '<div class="empty">まだ記録がありません。最初の設備を追加しましょう。</div>'}
    </div>
  </section>`);
  $('#hs').onsubmit = e => { e.preventDefault(); go('/search?q=' + encodeURIComponent(new FormData(e.target).get('q'))); };
}

/* ---------- 検索 ---------- */
const eqCard = e => `
<a class="card" href="/e/${e.id}" data-link>
  <div class="eq-top">${e.status !== '稼働中' ? `<span class="badge ${ST[e.status]}">${esc(e.status)}</span>` : ''}${dueBadge(e.alert)}</div>
  <h3>${esc(e.name)}</h3>
  <p class="loc">${esc(e.location)}</p>
  ${e.category ? `<p class="cat">${esc(e.category)}</p>` : ''}
  ${e.parts.length ? `<ul class="pv">${e.parts.slice(0, 3).map(p => `<li>${esc(p)}</li>`).join('')}${e.parts.length > 3 ? `<li class="more">ほか${e.parts.length - 3}点</li>` : ''}</ul>` : ''}
</a>`;

async function viewSearch(sp) {
  const meta = await api('/meta');
  const st = {q: sp.get('q') || '', loc: sp.get('location') || '', status: sp.get('status') || '', due: sp.get('due') === '1'};
  view(`
  <h1 class="ptitle">設備を探す</h1>
  <div class="searchbar big"><input id="q" value="${esc(st.q)}" placeholder="設備名・場所・部品名・型式など" autocomplete="off"></div>
  <div class="filters">
    <select id="loc"><option value="">すべての場所</option>${meta.locations.map(l => `<option ${l === st.loc ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>
    <select id="stt"><option value="">すべての状態</option>${['稼働中','停止中','廃棄'].map(l => `<option ${l === st.status ? 'selected' : ''}>${l}</option>`).join('')}</select>
    <label class="chk"><input type="checkbox" id="due" ${st.due ? 'checked' : ''}> 交換時期が近いもの</label>
  </div>
  <div id="res"></div>`);
  let timer;
  async function load() {
    const q = $('#q').value, loc = $('#loc').value, status = $('#stt').value, due = $('#due').checked;
    const params = new URLSearchParams(); if (q) params.set('q', q); if (loc) params.set('location', loc); if (status) params.set('status', status); if (due) params.set('due', '1');
    history.replaceState({}, '', '/search' + (params.toString() ? '?' + params : ''));
    const list = await api('/equipment?' + params);
    $('#res').innerHTML = list.length
      ? `<div class="sub" style="margin-bottom:14px;font-size:13px">${list.length}件</div><div class="grid">${list.map(eqCard).join('')}</div>`
      : `<div class="empty">該当する設備がありません<br><br><a class="btn" href="/new" data-link>設備を追加する</a></div>`;
  }
  $('#q').oninput = () => { clearTimeout(timer); timer = setTimeout(load, 180); };
  ['#loc', '#stt', '#due'].forEach(s => $(s).onchange = load);
  load();
}

/* ---------- ダイアログ ---------- */
/* モーダル表示中は背面のページをスクロールさせない（iOS Safari対応） */
let lockY = 0;
function lockScroll() { lockY = window.scrollY; document.body.style.top = -lockY + 'px'; document.body.classList.add('lock'); document.documentElement.classList.add('lock'); }
function unlockScroll() { document.body.classList.remove('lock'); document.documentElement.classList.remove('lock'); document.body.style.top = ''; window.scrollTo(0, lockY); }
document.addEventListener('touchmove', e => { if (document.body.classList.contains('lock') && !e.target.closest('dialog')) e.preventDefault(); }, {passive: false});

function modal(title, body, onSubmit, ok = '保存') {
  const d = document.createElement('dialog');
  d.innerHTML = `<form><h3>${title}</h3>${body}<div class="mactions"><button type="button" class="btn ghost" data-x>キャンセル</button><button class="btn primary">${ok}</button></div></form>`;
  d.setAttribute('tabindex', '-1'); d.setAttribute('autofocus', '');
  document.body.appendChild(d); lockScroll(); d.showModal();
  d.focus({preventScroll: true});
  if (document.activeElement && document.activeElement !== d) document.activeElement.blur();
  const f = $('form', d);
  let downOnBackdrop = false;
  d.addEventListener('pointerdown', e => { downOnBackdrop = e.target === d; });
  d.addEventListener('click', e => { if (downOnBackdrop && e.target === d) d.close(); });
  $('[data-x]', d).onclick = () => d.close();
  d.onclose = () => { d.remove(); unlockScroll(); };
  f.onsubmit = async ev => {
    ev.preventDefault();
    try { await onSubmit(Object.fromEntries(new FormData(f))); d.close(); }
    catch (err) { toast(err.message); }
  };
}
const fld = (label, name, val = '', type = 'text', extra = '') =>
  `<div class="fld"><label>${label}</label><input type="${type}" name="${name}" value="${esc(val)}" ${extra}></div>`;

function partDialog(eid, p) {
  p = p || {};
  modal(p.id ? '部品を編集' : '部品を追加', `
    ${fld('部品の名称 <i>*</i>', 'name', p.name, 'text', 'required')}
    ${fld('型番・仕様', 'spec', p.spec)}
    <div class="g2">${fld('数量', 'quantity', p.quantity || 1, 'number', 'min="1"')}${fld('交換周期（月）', 'interval_months', p.interval_months || '', 'number', 'min="1" placeholder="例: 12"')}</div>
    <div class="g2">${fld('設置日', 'installed_on', p.installed_on || '', 'date')}${fld('前回交換日', 'last_replaced', p.last_replaced || '', 'date')}</div>
    <div class="fld"><label>メモ</label><textarea name="notes">${esc(p.notes)}</textarea></div>`,
  async v => {
    await api(p.id ? '/parts/' + p.id : `/equipment/${eid}/parts`, {method: p.id ? 'PUT' : 'POST', body: v});
    toast(p.id ? '更新しました' : '追加しました'); route();
  });
}
function replaceDialog(p) {
  modal(`${esc(p.name)} を交換`, `
    ${fld('交換日', 'date', todayStr(), 'date', 'required')}
    <div class="fld"><label>メモ（任意）</label><textarea name="note" placeholder="交換した人、型番の変更など"></textarea></div>`,
  async v => { await api(`/parts/${p.id}/replace`, {method: 'POST', body: v}); toast('交換を記録しました'); route(); }, '記録する');
}
function logDialog(eid) {
  modal('記録を追加', `
    <div class="g2">${fld('日付', 'date', todayStr(), 'date', 'required')}
      <div class="fld"><label>種類</label><select class="in" name="kind">${['点検','修理','清掃','調整','メモ'].map(k => `<option>${k}</option>`).join('')}</select></div></div>
    <div class="fld"><label>内容 <i>*</i></label><textarea name="note" required></textarea></div>`,
  async v => { await api(`/equipment/${eid}/logs`, {method: 'POST', body: v}); toast('追加しました'); route(); }, '追加する');
}

/* ---------- 詳細 ---------- */
async function viewDetail(id) {
  const e = await api('/equipment/' + id);
  const alert = Math.min(...e.parts.filter(p => p.days_left !== null && p.days_left <= 30).map(p => p.days_left), Infinity);
  view(`
  <div class="detail-head">
    <div>
      <div class="eq-top"><span class="badge ${ST[e.status]}">${esc(e.status)}</span>${alert !== Infinity && e.status !== '廃棄' ? dueBadge(alert) : ''}</div>
      <h1>${esc(e.name)}</h1>
      <p class="loc">${esc(e.location)}</p>
    </div>
    <div class="tools">
      <a class="btn primary" href="/qr?ids=${e.id}&show=1" data-link>QRコード</a>
      <a class="btn" href="/e/${e.id}/edit" data-link>編集</a>
      <button class="btn ghost" id="del">削除</button>
    </div>
  </div>

  <div class="two">
    <div class="panel"><dl class="kv">
      <dt>分類</dt><dd>${esc(e.category) || '—'}</dd>
      <dt>型式</dt><dd>${esc(e.model) || '—'}</dd>
      <dt>製造番号</dt><dd>${esc(e.serial) || '—'}</dd>
      <dt>設置日</dt><dd>${fmtDate(e.installed_on) || '—'}</dd>
      <dt>設備ID</dt><dd>${e.id}</dd>
    </dl></div>
    <div class="panel"><div class="sub" style="font-size:13.5px;margin-bottom:6px">備考</div>${e.notes ? esc(e.notes).replace(/\n/g, '<br>') : '<span class="sub">なし</span>'}</div>
  </div>

  <section class="sec">
    <div class="sec-h"><h2>使用部品</h2><button class="btn sm" id="addpart">部品を追加</button></div>
    <div class="list">${e.parts.length ? e.parts.map(p => `
      <div class="part">
        <div>
          <b>${esc(p.name)}</b> ${p.spec ? `<span class="sub">${esc(p.spec)}</span>` : ''}
          <div class="meta">${p.quantity > 1 ? `数量 ${p.quantity}　` : ''}前回交換 ${fmtDate(p.last_replaced) || '未記録'}　交換${p.replace_count}回${p.interval_months ? `　周期 ${p.interval_months}ヶ月` : ''}</div>
          ${p.next_due ? `<div class="meta">次回の目安 ${fmtDate(p.next_due)}　${dueBadge(p.days_left)}</div>` : ''}
          ${p.notes ? `<div class="meta">${esc(p.notes)}</div>` : ''}
        </div>
        <div class="pa">
          <button class="btn sm" data-act="rep" data-id="${p.id}">交換を記録</button>
          <button class="link" data-act="edit" data-id="${p.id}">編集</button>
          <button class="link danger" data-act="delp" data-id="${p.id}">削除</button>
        </div>
      </div>`).join('') : '<div class="empty">部品はまだ登録されていません</div>'}
    </div>
  </section>

  <section class="sec">
    <div class="sec-h"><h2>履歴</h2><button class="btn sm" id="addlog">記録を追加</button></div>
    <ul class="tl list">${e.logs.length ? e.logs.map(l => `
      <li><time>${fmtDate(l.date)}</time>
        <div><span class="k ${l.kind === '部品交換' ? 'rep' : ''}">${esc(l.kind)}</span>${l.part_name ? `<b style="font-weight:500">${esc(l.part_name)}</b> ` : ''}<span class="sub">${esc(l.kind === '登録' ? '' : l.note)}</span>${l.ip ? `<span class="ip">${esc(l.ip)}</span>` : ''}</div>
        <button class="link danger" data-act="dell" data-id="${l.id}">削除</button></li>`).join('') : '<li class="empty" style="display:block">履歴はありません</li>'}
    </ul>
  </section>`);

  $('#addpart').onclick = () => partDialog(id);
  $('#addlog').onclick = () => logDialog(id);
  $('#del').onclick = async () => {
    if (!confirm(`「${e.name}」を削除します。部品と履歴も消えます。よろしいですか？`)) return;
    await api('/equipment/' + id, {method: 'DELETE'}); toast('削除しました'); go('/search');
  };
  $('#app').onclick = async ev => {
    const b = ev.target.closest('[data-act]'); if (!b) return;
    const pid = +b.dataset.id, p = e.parts.find(x => x.id === pid);
    try {
      if (b.dataset.act === 'rep') replaceDialog(p);
      if (b.dataset.act === 'edit') partDialog(id, p);
      if (b.dataset.act === 'delp' && confirm(`部品「${p.name}」を削除しますか？`)) { await api('/parts/' + pid, {method: 'DELETE'}); toast('削除しました'); route(); }
      if (b.dataset.act === 'dell' && confirm('この履歴を削除しますか？')) { await api('/logs/' + pid, {method: 'DELETE'}); toast('削除しました'); route(); }
    } catch (err) { toast(err.message); }
  };
}

/* ---------- 追加・編集フォーム ---------- */
async function viewForm(id) {
  const [meta, e] = await Promise.all([api('/meta'), id ? api('/equipment/' + id) : Promise.resolve({status: '稼働中'})]);
  const rowHtml = () => `<div class="prow">
    <div class="pc"><span class="pl">部品の名称</span><input type="text" data-k="name" placeholder="例: V-ベルト"></div>
    <div class="pc"><span class="pl">型番・仕様</span><input type="text" data-k="spec" placeholder="例: B-52"></div>
    <div class="pc"><span class="pl">前回交換日</span><input type="date" data-k="last_replaced" aria-label="前回交換日"></div>
    <div class="pc"><span class="pl">交換周期（月）</span><input type="number" inputmode="numeric" data-k="interval_months" min="1" placeholder="例: 12" aria-label="交換周期"></div>
    <button type="button" class="link" data-rm>この行を削除</button></div>`;
  view(`
  <h1 class="ptitle narrow">${id ? '設備を編集' : '設備を追加'}</h1>
  <div class="panel formpanel"><form class="f" id="form">
    <div class="fld"><label>設備の名称 <i>*</i></label><input type="text" name="name" value="${esc(e.name)}" required placeholder="例: 第2エアコンプレッサー"></div>
    <div class="fld"><label>設置場所 <i>*</i></label><input type="text" name="location" value="${esc(e.location)}" required list="locs" placeholder="例: 機械室 B1"></div>
    <datalist id="locs">${meta.locations.map(l => `<option value="${esc(l)}">`).join('')}</datalist>
    <div class="fld"><label>分類</label><input type="text" name="category" value="${esc(e.category)}" list="cats" placeholder="例: 空調 / 電気 / 搬送"></div>
    <datalist id="cats">${meta.categories.map(l => `<option value="${esc(l)}">`).join('')}</datalist>
    <div class="fld"><label>状態</label><select class="in" name="status">${['稼働中','停止中','廃棄'].map(s => `<option ${s === e.status ? 'selected' : ''}>${s}</option>`).join('')}</select></div>
    <div class="fld"><label>型式</label><input type="text" name="model" value="${esc(e.model)}"></div>
    <div class="fld"><label>製造番号</label><input type="text" name="serial" value="${esc(e.serial)}"></div>
    <div class="fld"><label>設置日</label><input type="date" name="installed_on" value="${esc(e.installed_on)}"></div>
    <div class="fld full"><label>備考</label><textarea name="notes">${esc(e.notes)}</textarea></div>
    ${id ? '' : `<div class="fld full" style="margin-top:8px">
      <label style="font-size:15px;color:var(--ink);font-weight:600">使用部品（任意）</label>
      <div class="note">あとから設備ページでも追加できます。交換周期を入れると、次回の交換時期をお知らせします。</div>
      <div class="prow-h" style="margin-top:8px"><span>部品の名称</span><span>型番・仕様</span><span>前回交換日</span><span>周期(月)</span><span></span></div>
      <div id="prows">${rowHtml()}</div>
      <div><button type="button" class="btn sm" id="addrow">部品の行を追加</button></div>
    </div>`}
    <div class="formbar"><a class="btn ghost" href="${id ? '/e/' + id : '/'}" data-link>キャンセル</a><button class="btn primary">${id ? '保存する' : '登録する'}</button></div>
  </form></div>`);
  if (!id) {
    $('#addrow').onclick = () => $('#prows').insertAdjacentHTML('beforeend', rowHtml());
    $('#prows').onclick = ev => { if (ev.target.matches('[data-rm]')) ev.target.closest('.prow').remove(); };
  }
  $('#form').onsubmit = async ev => {
    ev.preventDefault();
    const body = Object.fromEntries(new FormData(ev.target));
    if (!id) body.parts = $$('.prow').map(r => Object.fromEntries($$('[data-k]', r).map(i => [i.dataset.k, i.value]))).filter(p => p.name.trim());
    try {
      const res = await api(id ? '/equipment/' + id : '/equipment', {method: id ? 'PUT' : 'POST', body});
      toast(id ? '保存しました' : '登録しました'); go('/e/' + (id || res.id));
    } catch (err) { toast(err.message); }
  };
}

/* ---------- QR作成 ---------- */
async function viewQR(sp) {
  const list = await api('/equipment');
  list.sort((a, b) => a.location.localeCompare(b.location, 'ja') || a.name.localeCompare(b.name, 'ja'));
  const sel = new Set((sp.get('ids') || '').split(',').filter(Boolean).map(Number));
  view(`
  <h1 class="ptitle">QRコードを作る</h1>
  <div class="qrlayout noprint">
    <div class="panel">
      <div class="searchbar" style="max-width:none;padding:2px 2px 2px 18px"><input id="fs" placeholder="設備を絞り込む" autocomplete="off"></div>
      <div class="pickbar"><button class="btn sm" id="all">表示中をすべて選択</button><button class="btn sm ghost" id="none">解除</button><span class="cnt" id="cnt"></span></div>
      <div class="plist" id="plist"></div>
    </div>
    <div class="panel opts">
      ${INFO.open ? '' : '<div class="warnbox">いまはローカルホスト限定で起動中です。スマホなど他の端末で読み取るには、起動時にポートを開放（y）してください。</div>'}
      <div class="fld"><label>QRコードの移動先アドレス</label><input type="text" id="base" value="${esc(INFO.base_url)}">
        <div class="note">読み取った端末から開けるアドレスを指定します。例: http://192.168.1.10:8000</div></div>
      <div class="fld"><label>サイズ</label>
        <div class="seg"><label><input type="radio" name="sz" value="110"><span>小</span></label><label><input type="radio" name="sz" value="160" checked><span>中</span></label><label><input type="radio" name="sz" value="220"><span>大</span></label></div></div>
      <button class="btn primary" id="gen" style="width:100%">QRコードを表示</button>
    </div>
  </div>
  <div id="out"></div>`);

  const draw = () => {
    const f = $('#fs').value.trim().toLowerCase();
    const shown = list.filter(e => !f || (e.name + ' ' + e.location + ' ' + e.category).toLowerCase().includes(f));
    $('#plist').innerHTML = shown.length ? shown.map(e => `
      <label class="pick"><input type="checkbox" value="${e.id}" ${sel.has(e.id) ? 'checked' : ''}>
        <span><b style="font-weight:500">${esc(e.name)}</b><small>${esc(e.location)}</small></span></label>`).join('')
      : '<div class="empty">該当する設備がありません</div>';
    $('#cnt').textContent = sel.size + '件を選択中';
    return shown;
  };
  let shownNow = draw();
  $('#fs').oninput = () => { shownNow = draw(); };
  $('#plist').onchange = ev => { const id = +ev.target.value; ev.target.checked ? sel.add(id) : sel.delete(id); $('#cnt').textContent = sel.size + '件を選択中'; };
  $('#all').onclick = () => { shownNow.forEach(e => sel.add(e.id)); draw(); };
  $('#none').onclick = () => { sel.clear(); draw(); };

  const gen = () => {
    if (!sel.size) return toast('設備を選択してください');
    const base = $('#base').value.trim();
    if (!/^https?:\/\//.test(base)) return toast('アドレスは http:// から入力してください');
    const px = $('input[name=sz]:checked').value;
    const items = list.filter(e => sel.has(e.id));
    $('#out').innerHTML = `
      <div class="noprint" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
        <h2 style="font-size:20px">${items.length}件のQRコード</h2>
        <button class="btn" onclick="window.print()">印刷する</button></div>
      <div class="qrgrid" style="--qr:${px}px">${items.map(e => {
        const src = `/api/qr/${e.id}.svg?base=${encodeURIComponent(base)}`;
        return `<figure class="qrcard"><img src="${src}" alt="${esc(e.name)}のQRコード">
          <b>${esc(e.name)}</b><span>${esc(e.location)}</span><small>ID ${e.id}</small>
          <a class="dl noprint" href="${src}" download="qr-${e.id}.svg">SVGで保存</a></figure>`;
      }).join('')}</div>`;
    $('#out').scrollIntoView({behavior: 'smooth', block: 'start'});
  };
  $('#gen').onclick = gen;
  if (sp.get('show') === '1' && sel.size) gen();
}

(async () => { INFO = await api('/info'); route(); })();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# 起動
# --------------------------------------------------------------------------
def ask_open():
    while True:
        ans = input("ポートを開放しますか？ 同じネットワークの他の端末からアクセスできるようになります <y/n>: ").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("y か n で入力してください。")


def main():
    port = 8000
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except (IndexError, ValueError):
            print("--port の後に数字を指定してください")
            sys.exit(1)

    open_port = ask_open()
    host = "0.0.0.0" if open_port else "127.0.0.1"
    init_db()
    setup_logging()

    server = None
    for p in range(port, port + 20):
        try:
            server = ThreadingHTTPServer((host, p), Handler)
            port = p
            break
        except OSError:
            continue
    if server is None:
        print("使用できるポートが見つかりませんでした。--port で別の番号を指定してください。")
        sys.exit(1)

    INFO["open"] = open_port
    INFO["port"] = port
    INFO["base_url"] = f"http://{lan_ip() if open_port else '127.0.0.1'}:{port}"

    print()
    print("  設備台帳を起動しました")
    print(f"  このPC          : http://127.0.0.1:{port}")
    if open_port:
        print(f"  他の端末から    : {INFO['base_url']}")
        print("  ※ つながらない場合は、OSのファイアウォールでこのポートを許可してください")
        print("  ※ 認証はありません。信頼できる社内ネットワークでのみ使用してください")
    else:
        print("  ローカルホスト限定です（他の端末からはアクセスできません）")
    print(f"  データ保存先    : {DB_PATH}")
    print(f"  ログ保存先      : {LOG_PATH}")
    print("  終了するには Ctrl+C")
    print()

    audit("サーバー起動", f"{'開放(0.0.0.0)' if open_port else 'ローカルのみ'} ポート{port}")
    try:
        webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します")
        audit("サーバー停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()