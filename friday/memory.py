"""Long-term memory in SQLite: activity log, learned facts, chat history, notes, reminders."""
from __future__ import annotations

import difflib
import re
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY, ts REAL, app TEXT, title TEXT, description TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS activity_fts USING fts5(app, title, description, content='activity', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS activity_ai AFTER INSERT ON activity BEGIN
  INSERT INTO activity_fts(rowid, app, title, description) VALUES (new.id, new.app, new.title, new.description);
END;

CREATE TABLE IF NOT EXISTS facts(id INTEGER PRIMARY KEY, ts REAL, updated REAL, category TEXT, text TEXT,
                                 source TEXT, strength INTEGER DEFAULT 1);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(text, category, content='facts', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
  INSERT INTO facts_fts(rowid, text, category) VALUES (new.id, new.text, new.category);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, text, category) VALUES ('delete', old.id, old.text, old.category);
END;

CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, ts REAL, role TEXT, content TEXT, learned INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, ts REAL, kind TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY, due REAL, text TEXT, done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""

STOP = set("the and for you your are was what with this that have from how can about into when where which who why "
           "does did please tell show give make need want just like get".split())


def fts_query(text: str) -> str:
    words = [w for w in re.findall(r"\w{3,}", text.lower()) if w not in STOP]
    return " OR ".join(f'"{w}"' for w in words[:12])


class Memory:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def _q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid

    # --- key/value ---
    def get(self, k, default=None):
        rows = self._q("SELECT v FROM kv WHERE k=?", (k,))
        return rows[0]["v"] if rows else default

    def set(self, k, v):
        self._x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

    # --- activity ---
    def add_activity(self, app, title, description):
        return self._x("INSERT INTO activity(ts,app,title,description) VALUES(?,?,?,?)",
                       (time.time(), app, title, description))

    def activity_since(self, ts, limit=300):
        return self._q("SELECT * FROM activity WHERE ts>? ORDER BY ts LIMIT ?", (ts, limit))

    def recent_activity(self, limit=10):
        return list(reversed(self._q("SELECT * FROM activity ORDER BY ts DESC LIMIT ?", (limit,))))

    def search_activity(self, query, limit=15):
        q = fts_query(query)
        if not q:
            return []
        return self._q("SELECT a.* FROM activity_fts f JOIN activity a ON a.id=f.rowid "
                       "WHERE activity_fts MATCH ? ORDER BY a.ts DESC LIMIT ?", (q, limit))

    # --- facts ---
    def add_fact(self, text, category="general", source="observed"):
        """Insert a fact, or strengthen an existing near-duplicate. Returns True if new."""
        text = text.strip()
        if len(text) < 4:
            return False
        for f in self._q("SELECT id, text FROM facts"):
            if difflib.SequenceMatcher(None, f["text"].lower(), text.lower()).ratio() > 0.82:
                self._x("UPDATE facts SET strength=strength+1, updated=? WHERE id=?", (time.time(), f["id"]))
                return False
        now = time.time()
        self._x("INSERT INTO facts(ts,updated,category,text,source) VALUES(?,?,?,?,?)",
                (now, now, category.lower().strip() or "general", text, source))
        return True

    def facts(self, limit=500):
        return self._q("SELECT * FROM facts ORDER BY strength DESC, updated DESC LIMIT ?", (limit,))

    def search_facts(self, query, limit=8):
        q = fts_query(query)
        if not q:
            return []
        return self._q("SELECT f.* FROM facts_fts s JOIN facts f ON f.id=s.rowid "
                       "WHERE facts_fts MATCH ? ORDER BY rank LIMIT ?", (q, limit))

    def delete_fact(self, fact_id):
        self._x("DELETE FROM facts WHERE id=?", (fact_id,))

    # --- conversation ---
    def add_message(self, role, content):
        return self._x("INSERT INTO messages(ts,role,content) VALUES(?,?,?)", (time.time(), role, content))

    def recent_messages(self, limit=12):
        return list(reversed(self._q("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,))))

    def unlearned_messages(self, limit=80):
        return self._q("SELECT * FROM messages WHERE learned=0 ORDER BY id LIMIT ?", (limit,))

    def mark_learned(self, ids):
        for i in ids:
            self._x("UPDATE messages SET learned=1 WHERE id=?", (i,))

    # --- notes (hourly summaries, daily journals) ---
    def add_note(self, kind, text):
        return self._x("INSERT INTO notes(ts,kind,text) VALUES(?,?,?)", (time.time(), kind, text))

    def notes_between(self, start, end, kind=None):
        if kind:
            return self._q("SELECT * FROM notes WHERE ts>=? AND ts<? AND kind=? ORDER BY ts", (start, end, kind))
        return self._q("SELECT * FROM notes WHERE ts>=? AND ts<? ORDER BY ts", (start, end))

    def activity_between(self, start, end, limit=1000):
        return self._q("SELECT * FROM activity WHERE ts>=? AND ts<? ORDER BY ts LIMIT ?", (start, end, limit))

    # --- reminders ---
    def add_reminder(self, due, text):
        return self._x("INSERT INTO reminders(due,text) VALUES(?,?)", (due, text))

    def due_reminders(self):
        return self._q("SELECT * FROM reminders WHERE done=0 AND due<=?", (time.time(),))

    def pending_reminders(self):
        return self._q("SELECT * FROM reminders WHERE done=0 ORDER BY due")

    def finish_reminder(self, rid):
        self._x("UPDATE reminders SET done=1 WHERE id=?", (rid,))

    def stats(self):
        one = lambda sql: self._q(sql)[0]["n"]  # noqa: E731
        return {
            "facts": one("SELECT COUNT(*) n FROM facts"),
            "activity": one("SELECT COUNT(*) n FROM activity"),
            "messages": one("SELECT COUNT(*) n FROM messages"),
            "days": one("SELECT COUNT(DISTINCT date(ts,'unixepoch','localtime')) n FROM activity"),
        }
