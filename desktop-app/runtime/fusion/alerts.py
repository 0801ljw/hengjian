"""Durable operator alerts for lifecycle and other attention items."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .core import now


def _conn(instance):
    path = Path(instance) / "fusion.sqlite3"
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts(
            id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL,
            agent TEXT NOT NULL, severity TEXT NOT NULL, title TEXT NOT NULL,
            body TEXT NOT NULL, ref TEXT, acknowledged INTEGER NOT NULL DEFAULT 0)""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(alerts)")}
    if "ref" not in columns:
        conn.execute("ALTER TABLE alerts ADD COLUMN ref TEXT")
    return conn


def raise_alert(instance, kind, agent, title, body, severity="warning", ref=None):
    with _conn(instance) as conn:
        if ref is None:
            row = conn.execute("""SELECT id FROM alerts WHERE kind=? AND agent=? AND ref IS NULL
                AND acknowledged=0 ORDER BY id DESC LIMIT 1""", (str(kind), str(agent))).fetchone()
        else:
            row = conn.execute("""SELECT id FROM alerts WHERE kind=? AND agent=? AND ref=?
                AND acknowledged=0 ORDER BY id DESC LIMIT 1""",
                (str(kind), str(agent), str(ref))).fetchone()
        if row:
            conn.execute("UPDATE alerts SET at=?,severity=?,title=?,body=?,ref=? WHERE id=?",
                         (now(), str(severity)[:20], str(title)[:200], str(body)[:2000],
                          str(ref)[:200] if ref is not None else None, row[0]))
            return row[0]
        cur = conn.execute("""INSERT INTO alerts(at,kind,agent,severity,title,body,ref)
            VALUES(?,?,?,?,?,?,?)""", (now(), str(kind)[:40], str(agent)[:100],
                                      str(severity)[:20], str(title)[:200], str(body)[:2000],
                                      str(ref)[:200] if ref is not None else None))
        return cur.lastrowid


def list_alerts(instance, include_acknowledged=False):
    with _conn(instance) as conn:
        sql = "SELECT id,at,kind,agent,severity,title,body,acknowledged,ref FROM alerts"
        if not include_acknowledged:
            sql += " WHERE acknowledged=0"
        sql += " ORDER BY id DESC LIMIT 100"
        return [dict(zip(("id", "at", "kind", "agent", "severity", "title", "body",
                          "acknowledged", "ref"), row))
                for row in conn.execute(sql)]


def acknowledge(instance, alert_id):
    with _conn(instance) as conn:
        cur = conn.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (int(alert_id),))
        return cur.rowcount == 1
