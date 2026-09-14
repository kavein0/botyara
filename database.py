import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from profiles import STANDARD_PROFILE, validate_profile

DATABASE = os.environ.get("BOT_DATABASE", str(Path(__file__).with_name("server.db")))
PAYLOAD_UUID = "550e8400-e29b-41d4-a716-446655440000"


def connect():
    db = sqlite3.connect(DATABASE, timeout=5)
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db():
    with closing(connect()) as db, db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS payloads (
                id INTEGER PRIMARY KEY,
                uuid TEXT NOT NULL UNIQUE,
                profile TEXT NOT NULL
            )
        """)
        columns = [row[1] for row in db.execute("PRAGMA table_info(payloads)")]
        if "profile" not in columns:
            db.execute("ALTER TABLE payloads ADD COLUMN profile TEXT")
        db.execute("UPDATE payloads SET profile = ? WHERE profile IS NULL",
                   (json.dumps(STANDARD_PROFILE),))
        db.execute("""
            CREATE TABLE IF NOT EXISTS callbacks (
                id INTEGER PRIMARY KEY,
                uuid TEXT NOT NULL UNIQUE,
                payload_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen TEXT,
                stopped INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (payload_id) REFERENCES payloads(id)
            )
        """)
        columns = [row[1] for row in db.execute("PRAGMA table_info(callbacks)")]
        if "last_seen" not in columns:
            db.execute("ALTER TABLE callbacks ADD COLUMN last_seen TEXT")
        if "stopped" not in columns:
            db.execute("ALTER TABLE callbacks ADD COLUMN stopped INTEGER NOT NULL DEFAULT 0")
        db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY,
                callback_id INTEGER NOT NULL,
                command TEXT NOT NULL
                    CHECK (command IN ('whoami', 'ip', 'exit')),
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'sent', 'completed')),
                result TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (callback_id) REFERENCES callbacks(id)
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(callback_id, status, id)")
        db.execute(
            "INSERT OR IGNORE INTO payloads (uuid, profile) VALUES (?, ?)",
            (PAYLOAD_UUID, json.dumps(STANDARD_PROFILE))
        )


def get_payload_id(payload_uuid):
    with closing(connect()) as db, db:
        row = db.execute(
            "SELECT id FROM payloads WHERE uuid = ?",
            (payload_uuid,)
        ).fetchone()
    if row is None:
        return None
    return row[0]


def create_payload(payload_uuid, profile=STANDARD_PROFILE):
    validate_profile(profile)
    with closing(connect()) as db, db:
        db.execute("INSERT INTO payloads (uuid, profile) VALUES (?, ?)",
                   (payload_uuid, json.dumps(profile, ensure_ascii=False)))


def get_payload(payload_uuid):
    with closing(connect()) as db:
        row = db.execute("SELECT id, profile FROM payloads WHERE uuid = ?",
                         (payload_uuid,)).fetchone()
    if row is None:
        return None
    return {"id": row[0], "profile": json.loads(row[1])}


def get_callback_payload_id(callback_uuid):
    with closing(connect()) as db:
        row = db.execute("SELECT payload_id FROM callbacks WHERE uuid = ?",
                         (callback_uuid,)).fetchone()
    return row[0] if row else None


def create_callback(callback_uuid, payload_id):
    with closing(connect()) as db, db:
        db.execute(
            "INSERT INTO callbacks (uuid, payload_id, last_seen) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (callback_uuid, payload_id)
        )


def create_task(callback_uuid, command):
    with closing(connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""
            SELECT id, stopped,
                (julianday('now') - julianday(last_seen)) * 86400 < 15
            FROM callbacks WHERE uuid = ?
        """, (callback_uuid,)).fetchone()
        if row is None:
            return None
        if row[1]:
            raise ValueError("Колбэк уже вышел")
        if not row[2]:
            raise ValueError("Колбэк не на связи. Обнови страницу и выбери активный")
        cursor = db.execute(
            "INSERT INTO tasks (callback_id, command) VALUES (?, ?)",
            (row[0], command)
        )
        return cursor.lastrowid


def get_callback_id(callback_uuid):
    with closing(connect()) as db, db:
        row = db.execute(
            "SELECT id FROM callbacks WHERE uuid = ?",
            (callback_uuid,)
        ).fetchone()
    if row is None:
        return None
    return row[0]


def get_next_task(callback_id):
    with closing(connect()) as db, db:
        row = db.execute("""
            SELECT id, command
            FROM tasks
            WHERE callback_id = ? AND status IN ('pending', 'sent')
            ORDER BY id
            LIMIT 1
        """, (callback_id,)).fetchone()
    return row


def poll_task(callback_uuid):
    with closing(connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        callback = db.execute(
            "SELECT id, stopped FROM callbacks WHERE uuid = ?",
            (callback_uuid,)
        ).fetchone()
        if callback is None:
            raise LookupError("Колбэка такого нету")
        if callback[1]:
            raise ValueError("Колбэк уже вышел")
        db.execute(
            "UPDATE callbacks SET last_seen = CURRENT_TIMESTAMP WHERE id = ?",
            (callback[0],)
        )
        task = db.execute("""
            SELECT id, command FROM tasks
            WHERE callback_id = ? AND status IN ('pending', 'sent')
            ORDER BY id LIMIT 1
        """, (callback[0],)).fetchone()
        if task is not None:
            db.execute("UPDATE tasks SET status = 'sent' WHERE id = ?", (task[0],))
    return task


def save_result(callback_uuid, task_id, result):
    with closing(connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        task = db.execute("""
            SELECT t.status, t.result, t.command, t.callback_id
            FROM tasks t JOIN callbacks c ON c.id = t.callback_id
            WHERE t.id = ? AND c.uuid = ?
        """, (task_id, callback_uuid)).fetchone()
        if task is None:
            raise LookupError("Это не твоя задача или её нет")
        if task[0] == "pending":
            raise ValueError("Сначала забери задачу через poll")
        if task[0] == "completed":
            if task[1] != result:
                raise ValueError("У задачи уже другой результат")
            return
        db.execute(
            "UPDATE tasks SET result = ?, status = 'completed' WHERE id = ?",
            (result, task_id)
        )
        db.execute(
            "UPDATE callbacks SET last_seen = CURRENT_TIMESTAMP, stopped = ? WHERE id = ?",
            (int(task[2] == "exit"), task[3])
        )


def get_state():
    with closing(connect()) as db, db:
        db.row_factory = sqlite3.Row
        payloads = db.execute("SELECT id, uuid, profile FROM payloads ORDER BY id").fetchall()
        callbacks = db.execute("""
            SELECT c.*, p.uuid AS payload_uuid,
                CASE
                    WHEN c.stopped = 1 THEN 'вышел'
                    WHEN (julianday('now') - julianday(c.last_seen)) * 86400 < 15
                        THEN 'на связи'
                    ELSE 'нет связи'
                END AS state
            FROM callbacks c JOIN payloads p ON p.id = c.payload_id
            ORDER BY c.id DESC
        """).fetchall()
        tasks = db.execute("""
            SELECT t.*, c.uuid AS callback_uuid
            FROM tasks t JOIN callbacks c ON c.id = t.callback_id
            ORDER BY t.id DESC
        """).fetchall()
    return {
        "payloads": [{"id": row["id"], "uuid": row["uuid"],
                      "profile": json.loads(row["profile"])} for row in payloads],
        "callbacks": [dict(row) for row in callbacks],
        "tasks": [dict(row) for row in tasks]
    }


if __name__ == "__main__":
    init_db()
    print("ДБ готова")
