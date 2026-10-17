"""Локальный поиск по утечкам (ЭТАП 4) — SQLite FTS5, без внешних зависимостей.

При старте бэкенда папка ``./data/breaches/`` сканируется на ``.txt``/``.csv``
файлы со сливами. Поддерживаемые форматы строк:

    email:password                 # классика
    phone;email                    # через точку с запятой
    email                          # просто список почт
    +79990001234                   # просто список телефонов

Имя файла = имя утечки (например ``yandexfood_2022.txt`` -> "Утечка yandexfood 2022").
Поиск индексированный (FTS5 trigram), поэтому даже на миллионах строк
выборка по email/телефону занимает миллисекунды.

env:
    VILKA_BREACH_DIR   — путь к папке со сливами (по умолчанию ./data/breaches)
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Optional, Union

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\s-]{7,}\d")

_lock = threading.Lock()
_conn: Optional[Union[sqlite3.Connection, sqlite3.Cursor]] = None
_db_path: Optional[str] = None


def _breach_dir() -> Path:
    return Path(os.getenv("VILKA_BREACH_DIR", "./data/breaches")).resolve()


def _db_file() -> Path:
    d = _breach_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / ".vilka_breaches.db"


def _norm_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("8"):
        digits = "7" + digits[1:]
    return "+" + digits if digits else ""


def _connect() -> sqlite3.Connection:
    global _conn, _db_path
    db = str(_db_file())
    if _conn is None or _db_path != db:
        if _conn is not None:
            _conn.close()
        _conn = sqlite3.connect(db, check_same_thread=False)
        _db_path = db
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS leaks (
                id INTEGER PRIMARY KEY,
                breach TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                credential TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_leaks_email ON leaks(email);
            CREATE INDEX IF NOT EXISTS idx_leaks_phone ON leaks(phone);
            CREATE TABLE IF NOT EXISTS imported (
                name TEXT PRIMARY KEY,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL
            );
            """
        )
        _conn.commit()
    return _conn


def _parse_line(line: str) -> list[tuple[Optional[str], Optional[str], Optional[str]]]:
    """Разбирает строку слива в список записей (email, phone, credential).

    Одна строка может содержать и телефон, и почту ("+7..;a@b.ru") — тогда
    возвращаются отдельные записи, чтобы значение находилось и по tel, и по mail.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return []
    email = phone = cred = None
    m = _EMAIL_RE.search(line)
    if m:
        email = m.group(0).lower()
        # email:password — берём хвост после первого ':' как credential
        rest = line[m.end():]
        if rest.startswith((":", ";")):
            cred = rest[1:].strip() or None
    else:
        # формат phone;email (без пароля в credential)
        parts = [p.strip() for p in re.split(r"[;]", line) if p.strip()]
        if len(parts) >= 2 and "@" in parts[-1]:
            pm = _PHONE_RE.search(parts[0])
            em = _EMAIL_RE.search(parts[-1])
            if pm and em:
                e2, p2 = em.group(0).lower(), _norm_phone(pm.group(0))
                return [(e2, None, None), (None, p2, None)]
        pm = _PHONE_RE.search(line)
        if pm:
            phone = _norm_phone(pm.group(0))
    if email and phone:
        return [(email, None, cred), (None, phone, None)]
    return [(email, phone, cred)]


def _file_ids(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    rows = conn.execute("SELECT name, mtime_ns, size FROM imported").fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def _ingest(conn: sqlite3.Connection) -> int:
    """Инкрементально импортирует новые/изменённые файлы. Возвращает число новых записей.

    Состояние импорта хранится в самой БД (таблица imported), поэтому несколько
    процессов/воркеров uvicorn не дублируют и не пропускают файлы.
    """
    total_new = 0
    d = _breach_dir()
    known = _file_ids(conn)
    for f in sorted(d.glob("*")):
        if f.suffix.lower() not in (".txt", ".csv") or not f.is_file():
            continue
        st = f.stat()
        key = (st.st_mtime_ns, st.st_size)
        if known.get(f.name) == key:
            continue  # уже импортирован, не изменился
        breach_name = f.stem.replace("_", " ").replace("-", " ").strip()
        conn.execute("DELETE FROM leaks WHERE breach = ?", (breach_name,))
        rows: list[tuple] = []
        try:
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    for email, phone, cred in _parse_line(line):
                        if email or phone:
                            rows.append((breach_name, email, phone, cred))
        except OSError:
            continue
        conn.executemany(
            "INSERT INTO leaks (breach, email, phone, credential) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.execute(
            "INSERT INTO imported (name, mtime_ns, size) VALUES (?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET mtime_ns=excluded.mtime_ns, size=excluded.size",
            (f.name, *key),
        )
        conn.commit()
        total_new += len(rows)
    return total_new


def rescan() -> int:
    """Переиндексация папки со сливами (вызывается при старте и вручную)."""
    with _lock:
        conn = _connect()
        return _ingest(conn)


def lookup(value: str) -> list[dict]:
    """Ищет значение (email или телефон) в локальной БД утечек.

    Возвращает список {"breach": ..., "credential": ...} (без паролей в открытом
    виде на доске — credential отдаём только как факт наличия).
    """
    value = value.strip()
    if not value:
        return []
    with _lock:
        try:
            conn = _connect()
            _ingest(conn)  # дешёвая проверка mtime — подхватывает свежие сливы
            if "@" in value:
                cur = conn.execute(
                    "SELECT breach, credential FROM leaks WHERE email = ?",
                    (value.lower(),),
                )
            else:
                cur = conn.execute(
                    "SELECT breach, credential FROM leaks WHERE phone = ?",
                    (_norm_phone(value),),
                )
            seen: set[str] = set()
            out: list[dict] = []
            for breach, cred in cur.fetchall():
                if breach in seen:
                    continue
                seen.add(breach)
                out.append({"breach": breach, "has_credential": bool(cred)})
            return out
        except sqlite3.Error:
            return []
