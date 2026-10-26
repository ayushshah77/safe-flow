import sqlite3, json, time, re

DB = "runs.db"
SECRET = re.compile(
    r"(sk-[A-Za-z0-9_\-]{10,})|((api[_-]?key|token|password)\"?\s*[:=]\s*\"?[^\s\",]+)",
    re.I,
)

def _conn():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY, run_id TEXT, ts REAL,
        step_id TEXT, event TEXT, data TEXT)""")
    return c

def log_event(run_id, step_id, event, data):
    payload = SECRET.sub("[REDACTED]", json.dumps(data, default=str))
    c = _conn()
    with c:
        c.execute("INSERT INTO events(run_id,ts,step_id,event,data) VALUES (?,?,?,?,?)",
                  (run_id, time.time(), step_id, event, payload))
    c.close()

def get_run(run_id):
    c = _conn()
    rows = c.execute("SELECT ts,step_id,event,data FROM events WHERE run_id=? ORDER BY id",
                     (run_id,)).fetchall()
    c.close()
    return rows
