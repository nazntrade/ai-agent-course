"""Durable atomic admission and authoritative SQLite snapshots."""
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import secrets
import sqlite3
import threading
import time

TERMINAL = {"completed", "cancelled", "failed"}
ACTIVE = {"queued", "loading_model", "preparing_context", "running", "cancelling"}


class DomainError(Exception):
    def __init__(self, code, status=409, retry_after=None):
        self.code, self.status, self.retry_after = code, status, retry_after
        super().__init__(code)


class Store:
    def __init__(self, path: Path, settings, clock=time.time):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.settings, self.clock = settings, clock
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript("""
          PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL; PRAGMA busy_timeout=10000;
          CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY);
          INSERT OR IGNORE INTO schema_migrations VALUES(1);
          CREATE TABLE IF NOT EXISTS owners(id TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 1,
            bucket REAL NOT NULL, bucket_at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES owners(id),
            token_hash TEXT UNIQUE NOT NULL,label TEXT NOT NULL,revoked_at REAL,pairing_expires REAL);
          CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES owners(id),
            title TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,active_job_id TEXT,deleted_at REAL,
            created_at REAL NOT NULL,updated_at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES owners(id),device_id TEXT NOT NULL REFERENCES devices(id),
            conversation_id TEXT NOT NULL REFERENCES conversations(id),state TEXT NOT NULL,state_version INTEGER NOT NULL,
            text TEXT NOT NULL,partial_content TEXT NOT NULL DEFAULT '',error_code TEXT,finish_reason TEXT,
            history_truncated INTEGER NOT NULL DEFAULT 0,prompt_tokens INTEGER,prompt_budget INTEGER,
            created_at REAL NOT NULL,updated_at REAL NOT NULL,deadline REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,conversation_id TEXT NOT NULL REFERENCES conversations(id),
            role TEXT NOT NULL,text TEXT NOT NULL,job_id TEXT NOT NULL REFERENCES jobs(id),completion_status TEXT NOT NULL,created_at REAL NOT NULL,
            UNIQUE(job_id,role));
          CREATE TABLE IF NOT EXISTS idempotency(owner_id TEXT NOT NULL REFERENCES owners(id),key TEXT NOT NULL,payload_hash TEXT NOT NULL,
            job_id TEXT NOT NULL REFERENCES jobs(id),expires_at REAL NOT NULL,PRIMARY KEY(owner_id,key));
          CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL REFERENCES jobs(id),
            state_version INTEGER NOT NULL,event_type TEXT NOT NULL,payload TEXT NOT NULL,created_at REAL NOT NULL);
          CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state,created_at);
          CREATE INDEX IF NOT EXISTS events_job ON events(job_id,seq);
        """)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def close(self):
        self.db.close()

    def provision(self, owner_id, label, *, pairing=True):
        now, token, device_id = self.clock(), secrets.token_urlsafe(32), secrets.token_hex(16)
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO owners(id,bucket,bucket_at) VALUES(?,?,?)",
                       (owner_id, self.settings.bucket_capacity, now))
            db.execute("INSERT INTO devices VALUES(?,?,?,?,NULL,?)", (device_id, owner_id,
                       hashlib.sha256(token.encode()).hexdigest(), label, now + 900 if pairing else None))
        return device_id, token

    def revoke(self, device_id):
        with self.transaction() as db:
            db.execute("UPDATE devices SET revoked_at=? WHERE id=?", (self.clock(), device_id))

    def confirm_pairing(self, device_id):
        with self.transaction() as db:
            changed = db.execute("UPDATE devices SET pairing_expires=NULL WHERE id=? AND revoked_at IS NULL AND pairing_expires>?",
                                 (device_id, self.clock())).rowcount
            if not changed:
                raise DomainError("pairing_expired", 401)

    def expire_pairings(self):
        with self.transaction() as db:
            db.execute("UPDATE devices SET revoked_at=? WHERE revoked_at IS NULL AND pairing_expires<=?",
                       (self.clock(), self.clock()))

    def authenticate(self, token):
        now = self.clock()
        with self.lock:
            row = self.db.execute("SELECT d.id device_id,d.owner_id FROM devices d JOIN owners o ON o.id=d.owner_id "
                "WHERE d.token_hash=? AND d.revoked_at IS NULL AND o.enabled=1 AND (d.pairing_expires IS NULL OR d.pairing_expires>?)",
                (hashlib.sha256(token.encode()).hexdigest(), now)).fetchone()
        if row is None:
            raise DomainError("unauthorized", 401)
        return dict(row)

    def conversation(self, owner, conversation_id, db=None):
        row = (db or self.db).execute("SELECT * FROM conversations WHERE id=? AND owner_id=? AND deleted_at IS NULL",
                                     (conversation_id, owner)).fetchone()
        if row is None:
            raise DomainError("not_found", 404)
        return dict(row)

    @staticmethod
    def public_conversation(row):
        return {key: row[key] for key in ("id", "title", "revision", "active_job_id", "created_at", "updated_at")}

    def create_conversation(self, owner, title):
        now, identifier = self.clock(), secrets.token_hex(16)
        with self.transaction() as db:
            if db.execute("SELECT count(*) FROM conversations WHERE owner_id=? AND deleted_at IS NULL", (owner,)).fetchone()[0] >= 200:
                raise DomainError("conversation_limit", 429, 60)
            db.execute("INSERT INTO conversations(id,owner_id,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                       (identifier, owner, title, now, now))
            return self.public_conversation(self.conversation(owner, identifier, db))

    def conversations(self, owner, limit, offset):
        with self.lock:
            rows = self.db.execute("SELECT * FROM conversations WHERE owner_id=? AND deleted_at IS NULL ORDER BY updated_at DESC,id LIMIT ? OFFSET ?",
                                   (owner, limit + 1, offset)).fetchall()
        return {"items": [self.public_conversation(dict(row)) for row in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None}

    def rename(self, owner, identifier, title, revision):
        with self.transaction() as db:
            row = self.conversation(owner, identifier, db)
            if row["revision"] != revision:
                raise DomainError("revision_conflict")
            db.execute("UPDATE conversations SET title=?,revision=revision+1,updated_at=? WHERE id=?", (title, self.clock(), identifier))
            return self.public_conversation(self.conversation(owner, identifier, db))

    def delete(self, owner, identifier, revision):
        with self.transaction() as db:
            row = self.conversation(owner, identifier, db)
            if row["revision"] != revision:
                raise DomainError("revision_conflict")
            if row["active_job_id"]:
                self._finish(db, row["active_job_id"], "cancelled", "conversation_deleted")
            db.execute("UPDATE conversations SET deleted_at=?,revision=revision+1,active_job_id=NULL WHERE id=?", (self.clock(), identifier))
            return row["active_job_id"]

    def _snapshot(self, db, identifier):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise DomainError("not_found", 404)
        value = {key: row[key] for key in ("id", "conversation_id", "state", "state_version", "partial_content", "error_code",
                 "finish_reason", "history_truncated", "prompt_tokens", "prompt_budget", "created_at", "updated_at")}
        value["history_truncated"] = bool(value["history_truncated"])
        value["queue_position"] = None
        if row["state"] == "queued":
            value["queue_position"] = db.execute("SELECT count(*) FROM jobs WHERE state='queued' AND rowid<= (SELECT rowid FROM jobs WHERE id=?)", (identifier,)).fetchone()[0]
        return value

    def _event(self, db, identifier):
        snapshot = self._snapshot(db, identifier)
        db.execute("INSERT INTO events(job_id,state_version,event_type,payload,created_at) VALUES(?,?,?,?,?)",
                   (identifier, snapshot["state_version"], "terminal" if snapshot["state"] in TERMINAL else "snapshot",
                    json.dumps(snapshot), self.clock()))
        db.execute("DELETE FROM events WHERE job_id=? AND seq NOT IN (SELECT seq FROM events WHERE job_id=? ORDER BY seq DESC LIMIT ?)",
                   (identifier, identifier, self.settings.event_retention))

    def _refresh_queue(self,db):
        for queued in db.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY rowid").fetchall():
            db.execute("UPDATE jobs SET state_version=state_version+1,updated_at=? WHERE id=?",(self.clock(),queued[0]))
            self._event(db,queued[0])

    def _finish(self, db, identifier, state, error=None, content=None, finish_reason=None):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
        if not row or row["state"] in TERMINAL:
            return
        conversation = db.execute("SELECT * FROM conversations WHERE id=?", (row["conversation_id"],)).fetchone()
        if state == "completed" and (row["state"] == "cancelling" or conversation["deleted_at"] is not None
                                     or conversation["active_job_id"] != identifier):
            state, error, content = "cancelled", "cancelled", None
        if content is not None:
            db.execute("UPDATE jobs SET partial_content=? WHERE id=?", (content, identifier))
        db.execute("UPDATE jobs SET state=?,state_version=state_version+1,error_code=?,finish_reason=?,updated_at=? WHERE id=?",
                   (state, error, finish_reason, self.clock(), identifier))
        db.execute("UPDATE messages SET completion_status=? WHERE job_id=?", (state, identifier))
        if state == "completed":
            db.execute("INSERT INTO messages(conversation_id,role,text,job_id,completion_status,created_at) VALUES(?,'assistant',?,?,'completed',?)",
                       (row["conversation_id"], content, identifier, self.clock()))
        db.execute("UPDATE conversations SET active_job_id=NULL,revision=revision+1,updated_at=? WHERE id=? AND active_job_id=?",
                   (self.clock(), row["conversation_id"], identifier))
        self._event(db, identifier)
        if row["state"]=="queued":
            self._refresh_queue(db)

    def finish(self, identifier, state, error=None, content=None, finish_reason=None):
        with self.transaction() as db:
            self._finish(db, identifier, state, error, content, finish_reason)

    def admit(self, identity, conversation_id, key, text, revision):
        owner, now = identity["owner_id"], self.clock()
        digest = hashlib.sha256(json.dumps([conversation_id, text, revision], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        with self.transaction() as db:
            device=db.execute("SELECT d.id FROM devices d JOIN owners o ON o.id=d.owner_id WHERE d.id=? AND d.owner_id=? AND d.revoked_at IS NULL AND o.enabled=1 AND (d.pairing_expires IS NULL OR d.pairing_expires>?)",
                              (identity["device_id"],owner,now)).fetchone()
            if not device:
                raise DomainError("unauthorized",401)
            # Replay precedes mutable revision, quota, busy and queue checks.
            prior = db.execute("SELECT * FROM idempotency WHERE owner_id=? AND key=?", (owner, key)).fetchone()
            if prior:
                if prior["payload_hash"] != digest:
                    raise DomainError("idempotency_conflict")
                self.conversation(owner, conversation_id, db)
                return self._snapshot(db, prior["job_id"]), False
            row = self.conversation(owner, conversation_id, db)
            if row["revision"] != revision:
                raise DomainError("revision_conflict")
            if row["active_job_id"]:
                raise DomainError("conversation_busy")
            if db.execute("SELECT count(*) FROM jobs WHERE state='queued'").fetchone()[0] >= self.settings.queue_capacity:
                raise DomainError("queue_full", 429, 1)
            if db.execute("SELECT count(*) FROM jobs WHERE owner_id=? AND state='queued'", (owner,)).fetchone()[0] >= self.settings.owner_waiting:
                raise DomainError("owner_queue_full", 429, 1)
            bucket = db.execute("SELECT bucket,bucket_at FROM owners WHERE id=?", (owner,)).fetchone()
            available = min(self.settings.bucket_capacity, bucket["bucket"] + max(0, now-bucket["bucket_at"]) * self.settings.refill_per_minute / 60)
            if available < 1:
                raise DomainError("rate_limited", 429, max(1, math.ceil((1-available)*60/self.settings.refill_per_minute)))
            identifier = secrets.token_hex(16)
            db.execute("UPDATE owners SET bucket=?,bucket_at=? WHERE id=?", (available-1, max(now,bucket["bucket_at"]), owner))
            db.execute("INSERT INTO jobs(id,owner_id,device_id,conversation_id,state,state_version,text,created_at,updated_at,deadline) VALUES(?,?,?,?,'queued',1,?,?,?,?)",
                       (identifier, owner, identity["device_id"], conversation_id, text, now, now, now+self.settings.queue_seconds))
            db.execute("INSERT INTO messages(conversation_id,role,text,job_id,completion_status,created_at) VALUES(?,'user',?,?,'queued',?)", (conversation_id,text,identifier,now))
            db.execute("INSERT INTO idempotency VALUES(?,?,?,?,?)", (owner,key,digest,identifier,now+86400))
            db.execute("UPDATE conversations SET revision=revision+1,active_job_id=?,updated_at=? WHERE id=?", (identifier,now,conversation_id))
            self._event(db, identifier)
            return self._snapshot(db, identifier), True

    def job(self, owner, identifier):
        with self.lock:
            row = self.db.execute("SELECT conversation_id FROM jobs WHERE id=? AND owner_id=?", (identifier,owner)).fetchone()
            if not row:
                raise DomainError("not_found", 404)
            self.conversation(owner,row[0])
            return self._snapshot(self.db, identifier)

    def by_key(self, owner, key):
        with self.lock:
            row = self.db.execute("SELECT job_id FROM idempotency WHERE owner_id=? AND key=?",(owner,key)).fetchone()
            if not row:
                raise DomainError("not_found",404)
            return self.job(owner,row[0])

    def cancel(self, owner, identifier, partial_content=None):
        with self.transaction() as db:
            self.job(owner,identifier)
            self._finish(db,identifier,"cancelled","cancelled",content=partial_content)
            return self._snapshot(db,identifier)

    def claim(self):
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM jobs WHERE state IN ('loading_model','preparing_context','running','cancelling') LIMIT 1").fetchone():
                return None
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
            if not row:
                return None
            if row["deadline"] <= self.clock():
                self._finish(db,row["id"],"failed","queue_deadline_exceeded")
                return {"expired": True}
            db.execute("UPDATE jobs SET state='loading_model',state_version=state_version+1,updated_at=? WHERE id=?", (self.clock(),row["id"]))
            self._event(db,row["id"])
            self._refresh_queue(db)
            return dict(row)

    def update(self, identifier, **values):
        allowed = {"state","partial_content","history_truncated","prompt_tokens","prompt_budget"}
        if not values or not set(values) <= allowed:
            raise ValueError("invalid_job_update")
        with self.transaction() as db:
            row = db.execute("SELECT state FROM jobs WHERE id=?",(identifier,)).fetchone()
            if row[0] in TERMINAL:
                return False
            db.execute("UPDATE jobs SET " + ",".join(key+"=?" for key in values) + ",state_version=state_version+1,updated_at=? WHERE id=?",
                       (*values.values(),self.clock(),identifier))
            self._event(db,identifier)
            return True

    def history(self, conversation_id):
        with self.lock:
            rows = self.db.execute("SELECT j.text user_text,m.text assistant_text FROM jobs j JOIN messages m ON m.job_id=j.id AND m.role='assistant' WHERE j.conversation_id=? AND j.state='completed' ORDER BY j.rowid",(conversation_id,)).fetchall()
        return [({"role":"user","content":r[0]},{"role":"assistant","content":r[1]}) for r in rows]

    def messages(self, owner, identifier, limit, offset):
        with self.lock:
            self.conversation(owner,identifier)
            rows = self.db.execute("SELECT id,role,text,job_id,completion_status,created_at FROM messages WHERE conversation_id=? ORDER BY id LIMIT ? OFFSET ?",(identifier,limit+1,offset)).fetchall()
            return {"items":[dict(r) for r in rows[:limit]],"next_offset":offset+limit if len(rows)>limit else None}

    def events(self, identifier, cursor):
        with self.lock:
            rows = self.db.execute("SELECT * FROM events WHERE job_id=? ORDER BY seq",(identifier,)).fetchall()
            if cursor is None or not rows or cursor < rows[0]["seq"]-1 or cursor > rows[-1]["seq"]:
                return [(rows[-1]["seq"] if rows else 0,"reset",self._snapshot(self.db,identifier))]
            return [(r["seq"],r["event_type"],json.loads(r["payload"])) for r in rows if r["seq"]>cursor]

    def recover(self):
        with self.transaction() as db:
            for row in db.execute("SELECT id FROM jobs WHERE state NOT IN ('completed','cancelled','failed')").fetchall():
                self._finish(db,row[0],"failed","service_restarted")

    def stop_jobs(self, partial_contents=None):
        partial_contents=partial_contents or {}
        with self.transaction() as db:
            for row in db.execute("SELECT id FROM jobs WHERE state NOT IN ('completed','cancelled','failed')").fetchall():
                self._finish(db,row[0],"cancelled","service_stopping",content=partial_contents.get(row[0]))

    def counts(self):
        with self.lock:
            return {"waiting_jobs":self.db.execute("SELECT count(*) FROM jobs WHERE state='queued'").fetchone()[0],
                    "active_jobs":self.db.execute("SELECT count(*) FROM jobs WHERE state IN ('loading_model','preparing_context','running')").fetchone()[0]}
