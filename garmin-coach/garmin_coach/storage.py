"""SQLite-tallennus. Kaikki data pysyy paikallisesti käyttäjän koneella."""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from .models import Activity, DailyMetrics, Goal, Profile, Session

DEFAULT_DB = Path(os.environ.get("GARMIN_COACH_DB", Path.home() / ".garmin_coach" / "data.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_metrics (
    day TEXT PRIMARY KEY,
    resting_hr REAL, hrv_ms REAL, sleep_hours REAL, sleep_score REAL,
    deep_sleep_hours REAL, rem_sleep_hours REAL
);
CREATE TABLE IF NOT EXISTS activities (
    activity_id TEXT PRIMARY KEY,
    day TEXT NOT NULL, sport TEXT NOT NULL, duration_min REAL NOT NULL,
    distance_km REAL, avg_hr REAL, load REAL
);
CREATE INDEX IF NOT EXISTS idx_activities_day ON activities(day);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plan_sessions (
    day TEXT PRIMARY KEY,
    kind TEXT NOT NULL, distance_km REAL NOT NULL, title TEXT NOT NULL, details TEXT NOT NULL,
    phase TEXT NOT NULL, week_index INTEGER NOT NULL, adjusted TEXT
);
CREATE TABLE IF NOT EXISTS plan_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL, as_of TEXT NOT NULL, message TEXT NOT NULL
);
"""

_METRIC_FIELDS = ("resting_hr", "hrv_ms", "sleep_hours", "sleep_score", "deep_sleep_hours", "rem_sleep_hours")


def _d(s: str) -> date:
    return date.fromisoformat(s)


class Store:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- päivittäiset mittarit -------------------------------------------------
    def upsert_metrics(self, items: list[DailyMetrics]) -> int:
        """Lisää tai päivittää. None-arvot eivät ylikirjoita olemassa olevaa dataa."""
        n = 0
        for m in items:
            existing = self.conn.execute("SELECT * FROM daily_metrics WHERE day=?", (m.day.isoformat(),)).fetchone()
            values = {f: getattr(m, f) for f in _METRIC_FIELDS}
            if existing:
                for f in _METRIC_FIELDS:
                    if values[f] is None:
                        values[f] = existing[f]
            self.conn.execute(
                f"INSERT OR REPLACE INTO daily_metrics (day, {', '.join(_METRIC_FIELDS)}) VALUES (?{', ?' * len(_METRIC_FIELDS)})",
                (m.day.isoformat(), *[values[f] for f in _METRIC_FIELDS]),
            )
            n += 1
        self.conn.commit()
        return n

    def metrics(self, start: date | None = None, end: date | None = None) -> list[DailyMetrics]:
        rows = self.conn.execute(
            "SELECT * FROM daily_metrics WHERE day >= ? AND day <= ? ORDER BY day",
            ((start or date.min).isoformat(), (end or date.max).isoformat()),
        ).fetchall()
        return [DailyMetrics(day=_d(r["day"]), **{f: r[f] for f in _METRIC_FIELDS}) for r in rows]

    # --- harjoitukset ------------------------------------------------------------
    def upsert_activities(self, items: list[Activity]) -> int:
        for a in items:
            self.conn.execute(
                "INSERT OR REPLACE INTO activities VALUES (?,?,?,?,?,?,?)",
                (a.activity_id, a.day.isoformat(), a.sport, a.duration_min, a.distance_km, a.avg_hr, a.load),
            )
        self.conn.commit()
        return len(items)

    def activities(self, start: date | None = None, end: date | None = None) -> list[Activity]:
        rows = self.conn.execute(
            "SELECT * FROM activities WHERE day >= ? AND day <= ? ORDER BY day",
            ((start or date.min).isoformat(), (end or date.max).isoformat()),
        ).fetchall()
        ov = self.get_value("activity_overrides") or {}
        out = []
        for r in rows:
            a = Activity(
                activity_id=r["activity_id"], day=_d(r["day"]), sport=r["sport"], duration_min=r["duration_min"],
                distance_km=r["distance_km"], avg_hr=r["avg_hr"], load=r["load"],
            )
            o = ov.get(a.activity_id)
            if o:  # käyttäjän korjaus pysyy voimassa, vaikka Garmin-synkronointi päivittäisi alkuperäisen
                a.sport = o.get("sport", a.sport)
                if "load" in o:
                    a.load = o["load"]
            out.append(a)
        return out

    def set_activity_override(self, activity_id: str, sport: str | None = None, load: float | None = None) -> None:
        ov = self.get_value("activity_overrides") or {}
        cur = ov.get(activity_id, {})
        if sport:
            cur["sport"] = sport
        if load is not None:
            cur["load"] = load
        ov[activity_id] = cur
        self.set_value("activity_overrides", ov)

    # --- asetukset ---------------------------------------------------------------
    def _set(self, key: str, value: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value, default=str)))
        self.conn.commit()

    def _get(self, key: str) -> dict | None:
        r = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else None

    def save_profile(self, p: Profile) -> None:
        self._set("profile", asdict(p))

    def profile(self) -> Profile:
        d = self._get("profile")
        return Profile(**d) if d else Profile()

    def save_goal(self, g: Goal) -> None:
        self._set("goal", asdict(g))

    def goal(self) -> Goal | None:
        d = self._get("goal")
        if not d:
            return None
        d["start_date"] = _d(d["start_date"])
        d["event_date"] = _d(d["event_date"]) if d.get("event_date") else None
        return Goal(**d)

    def get_value(self, key: str):
        d = self._get(key)
        return d.get("v") if d else None

    def set_value(self, key: str, v) -> None:
        self._set(key, {"v": v})

    # --- harjoitusohjelma --------------------------------------------------------
    def plan(self, start: date | None = None, end: date | None = None) -> list[Session]:
        rows = self.conn.execute(
            "SELECT * FROM plan_sessions WHERE day >= ? AND day <= ? ORDER BY day",
            ((start or date.min).isoformat(), (end or date.max).isoformat()),
        ).fetchall()
        return [
            Session(
                day=_d(r["day"]), kind=r["kind"], distance_km=r["distance_km"], title=r["title"],
                details=r["details"], phase=r["phase"], week_index=r["week_index"], adjusted=r["adjusted"],
            )
            for r in rows
        ]

    def replace_plan_from(self, from_day: date, sessions: list[Session]) -> None:
        """Korvaa ohjelman päivästä `from_day` eteenpäin. Mennyt historia säilyy koskemattomana."""
        self.conn.execute("DELETE FROM plan_sessions WHERE day >= ?", (from_day.isoformat(),))
        for s in sessions:
            if s.day < from_day:
                continue
            self.conn.execute(
                "INSERT OR REPLACE INTO plan_sessions VALUES (?,?,?,?,?,?,?,?)",
                (s.day.isoformat(), s.kind, s.distance_km, s.title, s.details, s.phase, s.week_index, s.adjusted),
            )
        self.conn.commit()

    def clear_plan(self) -> None:
        self.conn.execute("DELETE FROM plan_sessions")
        self.conn.commit()

    def log(self, as_of: date, message: str) -> None:
        self.conn.execute(
            "INSERT INTO plan_log (created_at, as_of, message) VALUES (?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), as_of.isoformat(), message),
        )
        self.conn.commit()

    def log_entries(self, limit: int = 30) -> list[tuple[str, str]]:
        rows = self.conn.execute("SELECT as_of, message FROM plan_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [(r["as_of"], r["message"]) for r in rows]
