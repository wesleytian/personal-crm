import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

APP_DIR = Path(__file__).parent
# CRM_DEMO=1 runs against the fake data from seed_demo.py: demo.db plus fake Messages/call-history
# DBs, and never touches the real macOS Contacts, Messages or call history.
DEMO_MODE = os.environ.get("CRM_DEMO") == "1"
DB_PATH = Path(os.environ.get("CRM_DB") or APP_DIR / ("demo.db" if DEMO_MODE else "crm.db"))
if DEMO_MODE:
    MESSAGES_DB = APP_DIR / "demo_messages.db"
    CALLS_DB = APP_DIR / "demo_calls.db"
else:
    MESSAGES_DB = Path.home() / "Library" / "Messages" / "chat.db"
    CALLS_DB = Path.home() / "Library" / "Application Support" / "CallHistoryDB" / "CallHistory.storedata"
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

app = Flask(__name__, static_folder="static", static_url_path="/static")


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT,
                email TEXT,
                birthday TEXT,
                notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)").fetchall()}
        if "tier" not in cols:
            conn.execute("ALTER TABLE contacts ADD COLUMN tier INTEGER")
        if "location" not in cols:
            conn.execute("ALTER TABLE contacts ADD COLUMN location TEXT")
        if "role" not in cols:
            conn.execute("ALTER TABLE contacts ADD COLUMN role TEXT")
        if "is_service" not in cols:
            conn.execute("ALTER TABLE contacts ADD COLUMN is_service INTEGER DEFAULT 0")
            conn.execute("UPDATE contacts SET is_service = 1 WHERE role IS NOT NULL AND role != ''")
        if "archived" not in cols:
            conn.execute("ALTER TABLE contacts ADD COLUMN archived INTEGER DEFAULT 0")
        if "starred" not in cols:
            conn.execute("ALTER TABLE contacts ADD COLUMN starred INTEGER DEFAULT 0")
        if "trajectory" not in cols:
            conn.execute("ALTER TABLE contacts ADD COLUMN trajectory INTEGER")
        if "chemistry" not in cols:
            conn.execute("ALTER TABLE contacts ADD COLUMN chemistry INTEGER")
        if "occupations" not in cols:
            conn.execute("ALTER TABLE contacts ADD COLUMN occupations TEXT")
        # Idempotent rename: "Operator (sales/design/PM)" → "Operator" in any occupations array.
        for row in conn.execute(
            "SELECT id, occupations FROM contacts WHERE occupations LIKE '%Operator (sales/design/PM)%'"
        ).fetchall():
            try:
                arr = json.loads(row[1])
                new_arr, seen = [], set()
                for x in arr:
                    v = "Operator" if x == "Operator (sales/design/PM)" else x
                    if v not in seen:
                        new_arr.append(v); seen.add(v)
                conn.execute("UPDATE contacts SET occupations=? WHERE id=?", (json.dumps(new_arr), row[0]))
            except Exception:
                pass
        if "cadence_override" not in cols:
            conn.execute("ALTER TABLE contacts ADD COLUMN cadence_override TEXT")
        if "cadence_override_at" not in cols:
            conn.execute("ALTER TABLE contacts ADD COLUMN cadence_override_at TEXT")
        if "cadence_computed_at_override" not in cols:
            conn.execute("ALTER TABLE contacts ADD COLUMN cadence_computed_at_override TEXT")
        # Deals + investor network. List-valued fields are JSON arrays in TEXT, like occupations.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS investor_profiles (
                contact_id INTEGER PRIMARY KEY,
                investor_types TEXT,
                firm TEXT,
                closeness TEXT,
                share_bar TEXT DEFAULT 'good_or_better',
                active TEXT,
                check_min INTEGER,
                check_max INTEGER,
                stages TEXT,
                sectors TEXT,
                knows_well TEXT,
                investor_notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT,
                asks TEXT,
                status TEXT,
                conviction TEXT DEFAULT 'unrated',
                my_check INTEGER,
                invested_on TEXT,
                stage TEXT,
                sectors TEXT,
                round_size INTEGER,
                round_terms TEXT,
                deck_url TEXT,
                one_liner TEXT,
                forwardable_blurb TEXT,
                my_take TEXT,
                notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # role: 'founder' or 'shared' (shared_with = who has seen this deal; no statuses by design)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deal_contacts (
                deal_id INTEGER NOT NULL,
                contact_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (deal_id, contact_id, role)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deal_contacts_contact ON deal_contacts(contact_id)")


def normalize_handle(handle: str | None) -> str | None:
    if not handle:
        return None
    handle = handle.strip()
    if "@" in handle:
        return handle.lower()
    digits = "".join(c for c in handle if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits or None


_MSG_CACHE: dict = {"ts": 0.0, "data": None}
_MSG_CACHE_TTL = 30  # seconds


def _cadence_label(median_days: float) -> str:
    if median_days <= 2:
        return "daily"
    if median_days <= 10:
        return "weekly"
    if median_days <= 45:
        return "monthly"
    if median_days <= 150:
        return "quarterly"
    return "yearly"


def _snapshot_apple_db(src_db: Path, tmp_db: Path) -> bool:
    """Copy a live Apple SQLite db (plus its -wal/-shm sidecars) to a temp path.

    Returns False when macOS refuses the read. `.exists()` is not enough of a
    guard: under TCC a stat() succeeds while open() raises EPERM, so a missing
    Full Disk Access grant would otherwise blow up every caller.
    """
    try:
        shutil.copy2(src_db, tmp_db)
        for ext in ("-wal", "-shm"):
            side = Path(str(src_db) + ext)
            if side.exists():
                shutil.copy2(side, str(tmp_db) + ext)
    except PermissionError:
        app.logger.warning(
            "No permission to read %s - grant Full Disk Access to %s",
            src_db, sys.executable,
        )
        return False
    return True


def get_messages_stats() -> dict:
    """Return {normalized_handle: {count, last_date, last_text, last_from_me,
    cadence_label, cadence_days, overdue}}. Cached for 30s."""
    if _MSG_CACHE["data"] and time.time() - _MSG_CACHE["ts"] < _MSG_CACHE_TTL:
        return _MSG_CACHE["data"]

    if not MESSAGES_DB.exists():
        return {}

    tmp_dir = Path(tempfile.mkdtemp(prefix="crm_msgs_"))
    tmp_db = tmp_dir / "chat.db"
    try:
        if not _snapshot_apple_db(MESSAGES_DB, tmp_db):
            return {}

        with sqlite3.connect(tmp_db) as conn:
            # All 1:1 message dates per handle (used for count, last_date, cadence, outbound count)
            date_rows = conn.execute(
                """
                SELECT h.id, m.date, m.is_from_me
                FROM message m
                JOIN handle h ON m.handle_id = h.ROWID
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE c.style = 45 AND h.id IS NOT NULL
                """
            ).fetchall()
            last_rows = conn.execute(
                """
                SELECT h.id, m.date, m.text, m.is_from_me, m.attributedBody
                FROM message m
                JOIN handle h ON m.handle_id = h.ROWID
                JOIN (
                    SELECT m2.handle_id, MAX(m2.ROWID) AS max_rowid
                    FROM message m2
                    JOIN chat_message_join cmj2 ON cmj2.message_id = m2.ROWID
                    JOIN chat c2 ON c2.ROWID = cmj2.chat_id
                    WHERE m2.handle_id IS NOT NULL AND c2.style = 45
                    GROUP BY m2.handle_id
                ) latest ON m.ROWID = latest.max_rowid
                """
            ).fetchall()
    except Exception as e:
        print(f"[messages] error: {e}")
        return {}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Group dates by normalized key (collapses phone+email aliases for the same person)
    dates_by_key: dict[str, list[int]] = defaultdict(list)
    outbound_by_key: dict[str, int] = defaultdict(int)
    for handle, date, is_from_me in date_rows:
        key = normalize_handle(handle)
        if key and date is not None:
            dates_by_key[key].append(date)
            if is_from_me:
                outbound_by_key[key] += 1

    # Pick the LATEST message across all raw handles that map to the same person.
    # (Messages sometimes has duplicate handle rows for the same phone/email.)
    last_by_key: dict = {}  # key -> (date_ns, text, is_from_me)
    for handle, date_ns, text, is_from_me, attr_body in last_rows:
        key = normalize_handle(handle)
        if not key or date_ns is None:
            continue
        body_text = text or decode_attributed_body(attr_body) or ""
        prev = last_by_key.get(key)
        if prev is None or date_ns > prev[0]:
            last_by_key[key] = (date_ns, body_text, bool(is_from_me))
    last_by_key = {k: (v[1], v[2]) for k, v in last_by_key.items()}

    now_apple_ns = int((datetime.now(timezone.utc) - APPLE_EPOCH).total_seconds() * 1_000_000_000)
    stats: dict = {}
    for key, dates in dates_by_key.items():
        dates.sort()
        count = len(dates)
        last_date_ns = dates[-1]
        last_iso = (APPLE_EPOCH + timedelta(seconds=last_date_ns / 1_000_000_000)).isoformat()
        last_text, last_from_me = last_by_key.get(key, ("", False))

        cadence_label = None
        cadence_days = None
        overdue = False
        # Frequency-based cadence: conversation days / observation period in the last year.
        # Plus minimum sample size guard so a 3-day burst can't be called "daily".
        NS_PER_DAY = 86_400 * 1_000_000_000
        WINDOW_DAYS = 365
        today_day = now_apple_ns // NS_PER_DAY
        window_start = today_day - WINDOW_DAYS
        unique_days = sorted({d // NS_PER_DAY for d in dates})
        recent_days = [d for d in unique_days if d >= window_start]

        if len(recent_days) >= 2:
            first = recent_days[0]
            # Minimum observation window of 90 days so short bursts of activity
            # don't get labeled "daily" — a real daily cadence has to sustain over months.
            period = max(90, today_day - first + 1)
            if period >= 90:
                freq = len(recent_days) / period
                if freq >= 0.5:
                    label = "daily"
                elif freq >= 0.143:
                    label = "weekly"   # ≥ 1 conversation every ~7 days
                elif freq >= 0.025:
                    label = "monthly"
                elif freq >= 0.008:
                    label = "quarterly"
                else:
                    label = "yearly"

                # Downgrade if not enough distinct days to credibly support the label.
                order = ["daily", "weekly", "monthly", "quarterly", "yearly"]
                min_days = {"daily": 45, "weekly": 9, "monthly": 3, "quarterly": 2, "yearly": 1}
                while label and len(recent_days) < min_days[label]:
                    i = order.index(label)
                    label = order[i + 1] if i + 1 < len(order) else None

                cadence_label = label
                if label:
                    cadence_days = max(1, int(round(1 / freq)))
                    current_gap_days = (now_apple_ns - last_date_ns) / NS_PER_DAY
                    expected_days = 1 / freq
                    overdue = current_gap_days > 2 * expected_days and current_gap_days > 3

        first_date_ns = dates[0]
        first_iso = (APPLE_EPOCH + timedelta(seconds=first_date_ns / 1_000_000_000)).isoformat()
        stats[key] = {
            "count": count,
            "outbound_count": outbound_by_key.get(key, 0),
            "last_date": last_iso,
            "first_date": first_iso,
            "last_text": last_text,
            "last_from_me": last_from_me,
            "cadence_label": cadence_label,
            "cadence_days": cadence_days,
            "overdue": overdue,
        }

    _MSG_CACHE["ts"] = time.time()
    _MSG_CACHE["data"] = stats
    return stats


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Extract the message text from macOS Messages' attributedBody (typedstream) blob.
    Pattern: ... NSString ... + <length-byte(s)> <utf8-text> 0x86 ..."""
    if not blob:
        return None
    idx = blob.find(b"NSString")
    if idx < 0:
        return None
    plus = blob.find(b"+", idx, idx + 32)
    if plus < 0 or plus + 1 >= len(blob):
        return None
    p = plus + 1
    first = blob[p]
    if first == 0x81 and p + 2 < len(blob):
        length = int.from_bytes(blob[p + 1:p + 3], "little")
        start = p + 3
    elif first == 0x82 and p + 4 < len(blob):
        length = int.from_bytes(blob[p + 1:p + 5], "little")
        start = p + 5
    else:
        length = first
        start = p + 1
    raw = blob[start:start + length]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def get_recent_message_handles(limit: int | None = None, days: int | None = None) -> list[dict]:
    """Return [{handle, count, last_date}] for 1:1 message handles.
    Filter by `days` (only handles with at least one message in last N days)
    and/or `limit` (top N by most recent message)."""
    if not MESSAGES_DB.exists():
        return []

    where = ["c.style = 45", "h.id IS NOT NULL"]
    params: list = []
    if days:
        cutoff_ns = int(
            (datetime.now(timezone.utc) - timedelta(days=days) - APPLE_EPOCH).total_seconds()
            * 1_000_000_000
        )
        where.append("m.date > ?")
        params.append(cutoff_ns)

    sql = f"""
        SELECT h.id, COUNT(m.ROWID) AS cnt, MAX(m.date) AS last_date
        FROM message m
        JOIN handle h ON m.handle_id = h.ROWID
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        JOIN chat c ON c.ROWID = cmj.chat_id
        WHERE {' AND '.join(where)}
        GROUP BY h.id
        ORDER BY last_date DESC
    """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)

    tmp_dir = Path(tempfile.mkdtemp(prefix="crm_msgs_"))
    tmp_db = tmp_dir / "chat.db"
    try:
        if not _snapshot_apple_db(MESSAGES_DB, tmp_db):
            return []
        with sqlite3.connect(tmp_db) as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception as e:
        print(f"[recent-messages] error: {e}")
        return []
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    out = []
    for handle, count, last_date in rows:
        last_iso = None
        if last_date:
            last_iso = (APPLE_EPOCH + timedelta(seconds=last_date / 1_000_000_000)).isoformat()
        out.append({"handle": handle, "count": count, "last_date": last_iso})
    return out


def get_macos_contacts() -> list[dict]:
    """Read macOS Contacts directly from the AddressBook SQLite DB."""
    if DEMO_MODE:
        return []
    sources = list((Path.home() / "Library/Application Support/AddressBook/Sources").glob("*/AddressBook-v22.abcddb"))
    if not sources:
        top = Path.home() / "Library/Application Support/AddressBook/AddressBook-v22.abcddb"
        if top.exists():
            sources = [top]
    if not sources:
        print("[contacts] No AddressBook DB found")
        return []

    out: list[dict] = []
    for db in sources:
        tmp_dir = Path(tempfile.mkdtemp(prefix="crm_ab_"))
        tmp_db = tmp_dir / "ab.db"
        try:
            shutil.copy2(db, tmp_db)
            for ext in ("-wal", "-shm"):
                src = Path(str(db) + ext)
                if src.exists():
                    shutil.copy2(src, str(tmp_db) + ext)

            with sqlite3.connect(tmp_db) as conn:
                people: dict[int, dict] = {}
                for pk, fn, ln, org, nick, bday in conn.execute(
                    "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION, ZNICKNAME, ZBIRTHDAY FROM ZABCDRECORD"
                ):
                    name = " ".join(p for p in (fn, ln) if p).strip() or nick or org
                    if not name:
                        continue
                    bday_iso = None
                    if bday is not None:
                        try:
                            bday_iso = (APPLE_EPOCH + timedelta(seconds=bday)).date().isoformat()
                        except Exception:
                            pass
                    people[pk] = {"name": name, "phones": [], "emails": [], "birthday": bday_iso}

                for owner, phone in conn.execute(
                    "SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER WHERE ZFULLNUMBER IS NOT NULL"
                ):
                    if owner in people:
                        people[owner]["phones"].append(phone)

                for owner, email in conn.execute(
                    "SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS WHERE ZADDRESS IS NOT NULL"
                ):
                    if owner in people:
                        people[owner]["emails"].append(email)

                out.extend(people.values())
        except Exception as e:
            print(f"[contacts] error reading {db}: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return out


def relative_time(iso_str: str | None) -> str:
    if not iso_str:
        return "Never"
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    days = delta.days
    if days < 0:
        return "Just now"
    if days == 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if days < 7:
        return f"{days} days ago"
    if days < 30:
        w = days // 7
        return f"{w} week{'s' if w > 1 else ''} ago"
    if days < 365:
        m = days // 30
        return f"{m} month{'s' if m > 1 else ''} ago"
    y = days // 365
    return f"{y} year{'s' if y > 1 else ''} ago"


def merge_stats(contact: dict, msg_stats: dict) -> dict:
    count = 0
    outbound = 0
    last_date = None
    first_date = None
    last_text = ""
    last_from_me = False
    cadence_label = None
    cadence_days = None
    overdue = False
    best_count = -1
    for raw in (contact.get("phone"), contact.get("email")):
        key = normalize_handle(raw)
        if not key or key not in msg_stats:
            continue
        s = msg_stats[key]
        count += s["count"]
        outbound += s.get("outbound_count", 0)
        if s["last_date"] and (last_date is None or s["last_date"] > last_date):
            last_date = s["last_date"]
            last_text = s.get("last_text", "")
            last_from_me = s.get("last_from_me", False)
        if s.get("first_date") and (first_date is None or s["first_date"] < first_date):
            first_date = s["first_date"]
        if s["count"] > best_count:
            best_count = s["count"]
            cadence_label = s.get("cadence_label")
            cadence_days = s.get("cadence_days")
            overdue = s.get("overdue", False)
    contact["message_count"] = count
    contact["outbound_count"] = outbound
    contact["last_contact"] = relative_time(last_date)
    contact["last_contact_raw"] = last_date
    contact["first_contact"] = relative_time(first_date)
    contact["first_contact_raw"] = first_date
    contact["last_message"] = last_text
    contact["last_from_me"] = last_from_me
    contact["cadence_days"] = cadence_days
    contact["overdue"] = overdue
    # Apply user override of cadence label (track computed for later pattern analysis)
    override = contact.get("cadence_override")
    contact["cadence_computed"] = cadence_label
    if override:
        contact["cadence_label"] = override
        contact["cadence_overridden"] = True
    else:
        contact["cadence_label"] = cadence_label
        contact["cadence_overridden"] = False
    # Unnamed = the import used the phone/email as a fallback name
    contact["unnamed"] = (
        contact.get("name") and (
            contact["name"] == contact.get("phone") or
            contact["name"] == contact.get("email")
        )
    )
    return contact


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/calendar")
def calendar_page():
    return send_from_directory("static", "calendar.html")


def _get_call_stats(days: int = 365) -> dict[str, dict]:
    """Return {normalized_handle: {count, last_date_iso, duration_seconds}} for FaceTime + phone calls."""
    if not CALLS_DB.exists():
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days) - APPLE_EPOCH).total_seconds()
    tmp_dir = Path(tempfile.mkdtemp(prefix="crm_calls_"))
    tmp_db = tmp_dir / "calls.db"
    try:
        if not _snapshot_apple_db(CALLS_DB, tmp_db):
            return {}
        with sqlite3.connect(tmp_db) as conn:
            rows = conn.execute(
                "SELECT ZADDRESS, COUNT(*), MAX(ZDATE), SUM(ZDURATION) FROM ZCALLRECORD WHERE ZDATE > ? AND ZADDRESS IS NOT NULL GROUP BY ZADDRESS",
                (cutoff,),
            ).fetchall()
    except Exception as e:
        print(f"[calls] error: {e}")
        return {}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    out: dict[str, dict] = {}
    for addr, count, last_date_sec, dur in rows:
        k = normalize_handle(addr)
        if not k:
            continue
        last_iso = (APPLE_EPOCH + timedelta(seconds=last_date_sec)).isoformat() if last_date_sec else None
        if k not in out:
            out[k] = {"count": 0, "last_date": None, "duration_seconds": 0}
        s = out[k]
        s["count"] += count
        s["duration_seconds"] += float(dur or 0)
        if last_iso and (s["last_date"] is None or last_iso > s["last_date"]):
            s["last_date"] = last_iso
    return out


_CALL_CACHE = {"data": None, "ts": 0.0}


def _cached_calls() -> dict[str, dict]:
    if _CALL_CACHE["data"] is not None and time.time() - _CALL_CACHE["ts"] < 300:
        return _CALL_CACHE["data"]
    _CALL_CACHE["data"] = _get_call_stats(365)
    _CALL_CACHE["ts"] = time.time()
    return _CALL_CACHE["data"]


_VOL_CACHE = {"data": None, "ts": 0.0}


def _cached_volumes() -> dict[str, int]:
    if _VOL_CACHE["data"] is not None and time.time() - _VOL_CACHE["ts"] < 300:
        return _VOL_CACHE["data"]
    _VOL_CACHE["data"] = _compute_volumes(365)
    _VOL_CACHE["ts"] = time.time()
    return _VOL_CACHE["data"]


@app.route("/api/contacts", methods=["GET"])
def list_contacts():
    if request.args.get("fresh") == "1":
        _MSG_CACHE["data"] = None
        _VOL_CACHE["data"] = None
    msg_stats = get_messages_stats()
    volumes = _cached_volumes()
    calls = _cached_calls()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM contacts ORDER BY name COLLATE NOCASE").fetchall()]
    for c in rows:
        c["locations"] = parse_locations(c.pop("location", None))
        c["occupations"] = parse_locations(c.pop("occupations", None))
    contacts = [merge_stats(c, msg_stats) for c in rows]
    for c in contacts:
        v = 0
        call_n = 0
        call_dur = 0.0
        for raw in (c.get("phone"), c.get("email")):
            k = normalize_handle(raw)
            if not k:
                continue
            if k in volumes:
                v += volumes[k]
            if k in calls:
                call_n += calls[k]["count"]
                call_dur += calls[k]["duration_seconds"]
        c["msg_count_365"] = v          # raw messages incl. group chat
        c["call_count_365"] = call_n
        c["call_duration_seconds_365"] = int(call_dur)
        # Pure message count for "most messages" sort. Calls displayed separately.
        c["volume_365"] = v
        # "Total time" score: msgs + 3 × call minutes — combined relationship-time signal.
        c["interaction_score_365"] = v + int(call_dur / 60 * 3)
    return jsonify(contacts)


def _locations_from_payload(data: dict) -> str | None:
    """Accept `locations: [...]` (preferred) or legacy `location: 'str'`."""
    if "locations" in data:
        return serialize_locations(data.get("locations"))
    if "location" in data:
        return serialize_locations(data.get("location"))
    return None


def _occupations_from_payload(data: dict) -> str | None:
    if "occupations" in data:
        return serialize_locations(data.get("occupations"))
    return None


def _is_service_from_payload(data: dict) -> int:
    return 1 if data.get("is_service") else 0


@app.route("/api/contacts", methods=["POST"])
def add_contact():
    data = request.get_json(force=True)
    if not data.get("name"):
        return jsonify({"error": "name required"}), 400
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO contacts (name, phone, email, birthday, notes, location, occupations, is_service) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (data.get("name"), data.get("phone"), data.get("email"), data.get("birthday"), data.get("notes"), _locations_from_payload(data), _occupations_from_payload(data), _is_service_from_payload(data)),
        )
    return jsonify({"id": cur.lastrowid}), 201


@app.route("/api/contacts/<int:cid>", methods=["PUT"])
def update_contact(cid: int):
    """Partial update — only touches fields explicitly present in the payload, so
    a form that forgets `locations` (or anything else) can't silently wipe it."""
    data = request.get_json(force=True)
    fields = []
    values = []
    if "name" in data:
        fields.append("name=?"); values.append(data.get("name"))
    if "phone" in data:
        fields.append("phone=?"); values.append(data.get("phone"))
    if "email" in data:
        fields.append("email=?"); values.append(data.get("email"))
    if "birthday" in data:
        fields.append("birthday=?"); values.append(data.get("birthday"))
    if "notes" in data:
        fields.append("notes=?"); values.append(data.get("notes"))
    if "locations" in data or "location" in data:
        fields.append("location=?"); values.append(_locations_from_payload(data))
    if "occupations" in data:
        fields.append("occupations=?"); values.append(_occupations_from_payload(data))
    if "is_service" in data:
        fields.append("is_service=?"); values.append(_is_service_from_payload(data))
    if "archived" in data:
        fields.append("archived=?"); values.append(1 if data.get("archived") else 0)
    if "starred" in data:
        fields.append("starred=?"); values.append(1 if data.get("starred") else 0)
    for f in ("trajectory", "chemistry"):
        if f in data:
            v = data.get(f)
            if v is not None:
                try:
                    v = int(v)
                    if v < 1 or v > 3:
                        v = None
                except (TypeError, ValueError):
                    v = None
            fields.append(f"{f}=?"); values.append(v)
    if "cadence_override" in data:
        fields.append("cadence_override=?"); values.append(data.get("cadence_override") or None)
    if not fields:
        return jsonify({"ok": True, "updated": []})
    values.append(cid)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f"UPDATE contacts SET {', '.join(fields)} WHERE id=?", values)
    return jsonify({"ok": True, "updated": [f.split("=")[0] for f in fields]})


@app.route("/api/contacts/<int:cid>/service", methods=["PUT"])
def set_service(cid: int):
    data = request.get_json(force=True)
    val = _is_service_from_payload(data)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE contacts SET is_service=? WHERE id=?", (val, cid))
    return jsonify({"ok": True, "is_service": bool(val)})


@app.route("/api/contacts/<int:cid>/archive", methods=["PUT"])
def set_archived(cid: int):
    data = request.get_json(force=True)
    val = 1 if data.get("archived") else 0
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE contacts SET archived=? WHERE id=?", (val, cid))
    return jsonify({"ok": True, "archived": bool(val)})


@app.route("/api/contacts/<int:cid>/star", methods=["PUT"])
def set_starred(cid: int):
    data = request.get_json(force=True)
    val = 1 if data.get("starred") else 0
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE contacts SET starred=? WHERE id=?", (val, cid))
    return jsonify({"ok": True, "starred": bool(val)})


@app.route("/api/contacts/<int:cid>/<field>", methods=["PUT"])
def set_score(cid: int, field: str):
    if field not in ("trajectory", "chemistry"):
        return jsonify({"error": "unknown field"}), 404
    data = request.get_json(force=True)
    v = data.get(field)
    if v is not None:
        try:
            v = int(v)
            if v < 1 or v > 3:
                v = None
        except (TypeError, ValueError):
            v = None
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f"UPDATE contacts SET {field}=? WHERE id=?", (v, cid))
    return jsonify({"ok": True, field: v})


VALID_CADENCES = {"daily", "weekly", "monthly", "quarterly", "yearly"}


@app.route("/api/contacts/<int:cid>/cadence-override", methods=["PUT"])
def set_cadence_override(cid: int):
    """Override the computed cadence. Body: {cadence_override: 'weekly'|null, cadence_computed: 'monthly'|null}.
    `cadence_computed` is what the algo said at override time — stored alongside so we can later
    spot patterns in where the algo is consistently off."""
    data = request.get_json(force=True)
    override = data.get("cadence_override")
    if override == "" or override is None:
        override = None
    elif override not in VALID_CADENCES:
        return jsonify({"error": f"cadence must be one of {sorted(VALID_CADENCES)}"}), 400
    computed = data.get("cadence_computed") if override else None
    now_iso = datetime.now(timezone.utc).isoformat() if override else None
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE contacts SET cadence_override=?, cadence_override_at=?, cadence_computed_at_override=? WHERE id=?",
            (override, now_iso, computed, cid),
        )
    return jsonify({"ok": True, "cadence_override": override})


@app.route("/api/contacts/<int:cid>", methods=["DELETE"])
def delete_contact(cid: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM contacts WHERE id=?", (cid,))
        conn.execute("DELETE FROM investor_profiles WHERE contact_id=?", (cid,))
        conn.execute("DELETE FROM deal_contacts WHERE contact_id=?", (cid,))
    return jsonify({"ok": True})


@app.route("/api/contacts/<int:cid>/locations", methods=["PUT"])
def set_locations(cid: int):
    """Replace the whole locations list. Body: {locations: [...]} or {location: '...'} (legacy)."""
    data = request.get_json(force=True)
    serialized = _locations_from_payload(data)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE contacts SET location=? WHERE id=?", (serialized, cid))
    return jsonify({"ok": True, "locations": parse_locations(serialized)})


@app.route("/api/contacts/<int:cid>/occupations", methods=["PUT"])
def set_occupations(cid: int):
    data = request.get_json(force=True)
    serialized = _occupations_from_payload(data)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE contacts SET occupations=? WHERE id=?", (serialized, cid))
    return jsonify({"ok": True, "occupations": parse_locations(serialized)})


@app.route("/api/contacts/<int:cid>/tier", methods=["PUT"])
def set_tier(cid: int):
    data = request.get_json(force=True)
    tier = data.get("tier")
    if tier is not None:
        try:
            tier = int(tier)
            if tier < 1 or tier > 4:
                tier = None
        except (TypeError, ValueError):
            tier = None
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE contacts SET tier=? WHERE id=?", (tier, cid))
    return jsonify({"ok": True, "tier": tier})


@app.route("/api/import-contacts", methods=["POST"])
def import_contacts():
    """Pull contacts from macOS Contacts, skip duplicates by name."""
    mac_contacts = get_macos_contacts()
    added = 0
    with sqlite3.connect(DB_PATH) as conn:
        existing = {r[0].lower() for r in conn.execute("SELECT name FROM contacts").fetchall() if r[0]}
        for c in mac_contacts:
            name = (c.get("name") or "").strip()
            if not name or name.lower() in existing:
                continue
            phone = c["phones"][0] if c.get("phones") else None
            email = c["emails"][0] if c.get("emails") else None
            conn.execute(
                "INSERT INTO contacts (name, phone, email, birthday) VALUES (?, ?, ?, ?)",
                (name, phone, email, c.get("birthday")),
            )
            added += 1
            existing.add(name.lower())
    return jsonify({"added": added, "total_in_contacts": len(mac_contacts)})


@app.route("/api/import-recent-messages", methods=["POST"])
def import_recent_messages():
    """Pull message handles. Use macOS Contacts to fill in names; dedupe by phone/email.
    Query params: `days=N` (everyone with a 1:1 message in last N days) — preferred.
    Or `limit=N` for top N by recency. Default: days=365."""
    days_arg = request.args.get("days")
    limit_arg = request.args.get("limit")
    if days_arg:
        try:
            handles = get_recent_message_handles(days=max(1, int(days_arg)))
        except ValueError:
            handles = get_recent_message_handles(days=365)
    elif limit_arg:
        try:
            handles = get_recent_message_handles(limit=max(1, min(int(limit_arg), 2000)))
        except ValueError:
            handles = get_recent_message_handles(days=365)
    else:
        handles = get_recent_message_handles(days=365)

    name_lookup: dict[str, str] = {}
    for c in get_macos_contacts():
        nm = (c.get("name") or "").strip()
        if not nm:
            continue
        for ph in c.get("phones", []) or []:
            k = normalize_handle(ph)
            if k:
                name_lookup.setdefault(k, nm)
        for em in c.get("emails", []) or []:
            k = normalize_handle(em)
            if k:
                name_lookup.setdefault(k, nm)

    added = 0
    relinked = 0
    with sqlite3.connect(DB_PATH) as conn:
        existing_phones, existing_emails = set(), set()
        for cid, cname, phone, email in conn.execute(
            "SELECT id, name, phone, email FROM contacts"
        ).fetchall():
            kp = normalize_handle(phone)
            ke = normalize_handle(email)
            if kp:
                existing_phones.add(kp)
            if ke:
                existing_emails.add(ke)
            # Re-link: if name currently == phone or email (fallback case), look up real name
            if cname and (cname == phone or cname == email):
                real = name_lookup.get(kp) or name_lookup.get(ke)
                if real and real != cname:
                    conn.execute("UPDATE contacts SET name=? WHERE id=?", (real, cid))
                    relinked += 1

        for h in handles:
            handle = h["handle"]
            key = normalize_handle(handle)
            if not key:
                continue
            is_email = "@" in handle
            if is_email and key in existing_emails:
                continue
            if not is_email and key in existing_phones:
                continue

            name = name_lookup.get(key)
            phone = None if is_email else handle
            email = handle if is_email else None
            # Skip unnamed shortcodes outright — almost always automated senders
            if not name and not is_email and _phone_digit_count(handle) < 7:
                continue
            conn.execute(
                "INSERT INTO contacts (name, phone, email) VALUES (?, ?, ?)",
                (name or handle, phone, email),
            )
            added += 1
            (existing_emails if is_email else existing_phones).add(key)

    return jsonify({"added": added, "relinked": relinked, "checked": len(handles)})


def parse_locations(val: str | None) -> list[str]:
    """Read the `location` DB cell. Stores a JSON array; falls back to legacy
    single-string format for old rows."""
    if not val:
        return []
    s = val.strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            result = json.loads(s)
            if isinstance(result, list):
                return [str(x).strip() for x in result if str(x).strip()]
        except (json.JSONDecodeError, TypeError):
            pass
    return [s]


def serialize_locations(locs) -> str | None:
    if locs is None:
        return None
    if isinstance(locs, str):
        locs = [locs]
    cleaned = []
    seen = set()
    for x in locs:
        if not x:
            continue
        t = str(x).strip()
        if t and t not in seen:
            cleaned.append(t)
            seen.add(t)
    return json.dumps(cleaned) if cleaned else None


def _phone_digit_count(phone: str | None) -> int:
    if not phone:
        return 0
    return sum(1 for c in phone if c.isdigit())


@app.route("/api/cleanup", methods=["POST"])
def cleanup_unnamed():
    """Delete unnamed contacts that look automated. Flag the rest for manual review.
    Heuristics for 'definitely automated':
      - Phone is a short code (< 7 digits), OR
      - You've never sent them a message (one-way notification stream)."""
    msg_stats = get_messages_stats()
    deleted_ids: list[int] = []
    flagged: list[dict] = []
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM contacts").fetchall()]
        for c in rows:
            name = c.get("name") or ""
            phone = c.get("phone") or ""
            email = c.get("email") or ""
            if not (name and (name == phone or name == email)):
                continue  # has a real name, leave alone

            outbound = 0
            for raw in (phone, email):
                key = normalize_handle(raw)
                if key and key in msg_stats:
                    outbound += msg_stats[key].get("outbound_count", 0)

            digits = _phone_digit_count(phone)
            is_shortcode = phone and digits and digits < 7
            never_replied = outbound == 0

            if is_shortcode or never_replied:
                conn.execute("DELETE FROM contacts WHERE id=?", (c["id"],))
                deleted_ids.append(c["id"])
            else:
                flagged.append({
                    "id": c["id"],
                    "name": name,
                    "outbound_count": outbound,
                })
    return jsonify({"deleted": len(deleted_ids), "flagged": flagged})


def _real_keys_from_stats(msg_stats: dict) -> set[str]:
    """Set of normalized handle keys that represent real people:
    you've sent them at least one message, and the handle isn't a shortcode."""
    out = set()
    for key, s in msg_stats.items():
        if s.get("outbound_count", 0) < 1:
            continue
        if "@" in key or len(key) >= 7:
            out.add(key)
    return out


@app.route("/api/activity")
def activity():
    """For each day in the last N days, count distinct REAL people you 1:1 messaged.
    'Real' = not a shortcode AND you've sent them at least one message (filters out
    Doordash, Ticketmaster, banks, etc.)."""
    try:
        days = max(7, min(int(request.args.get("days", 365)), 1095))
    except (TypeError, ValueError):
        days = 365
    if not MESSAGES_DB.exists():
        return jsonify([])

    NS_PER_DAY = 86_400 * 1_000_000_000
    cutoff_ns = int(
        (datetime.now(timezone.utc) - timedelta(days=days) - APPLE_EPOCH).total_seconds()
        * 1_000_000_000
    )

    real_keys = _real_keys_from_stats(get_messages_stats())

    tmp_dir = Path(tempfile.mkdtemp(prefix="crm_act_"))
    tmp_db = tmp_dir / "chat.db"
    try:
        if not _snapshot_apple_db(MESSAGES_DB, tmp_db):
            return jsonify([])
        with sqlite3.connect(tmp_db) as conn:
            rows = conn.execute(
                """
                SELECT m.date, h.id
                FROM message m
                JOIN handle h ON m.handle_id = h.ROWID
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE c.style = 45 AND m.date > ? AND h.id IS NOT NULL
                """,
                (cutoff_ns,),
            ).fetchall()
    except Exception as e:
        print(f"[activity] error: {e}")
        return jsonify([])
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    day_people: dict[int, set[str]] = defaultdict(set)
    for date_ns, handle in rows:
        key = normalize_handle(handle)
        if key and key in real_keys:
            day_people[date_ns // NS_PER_DAY].add(key)

    today_day = int((datetime.now(timezone.utc) - APPLE_EPOCH).total_seconds() // 86_400)
    start_day = today_day - days + 1
    result = []
    for d in range(start_day, today_day + 1):
        dt = (APPLE_EPOCH + timedelta(days=d)).date()
        result.append({"date": dt.isoformat(), "count": len(day_people.get(d, set()))})
    return jsonify(result)


@app.route("/api/activity/day/<date_str>")
def activity_day(date_str: str):
    """Who you 1:1 messaged on a specific day. Real people only.
    Returns {date, people: [{name, count, last_text, last_from_me}]}."""
    try:
        dt = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    NS_PER_DAY = 86_400 * 1_000_000_000
    day_start_ns = int(
        (dt.replace(hour=0, minute=0, second=0, microsecond=0) - APPLE_EPOCH).total_seconds()
        * 1_000_000_000
    )
    day_end_ns = day_start_ns + NS_PER_DAY

    msg_stats = get_messages_stats()
    real_keys = _real_keys_from_stats(msg_stats)

    if not MESSAGES_DB.exists():
        return jsonify({"date": date_str, "people": [], "count": 0})

    tmp_dir = Path(tempfile.mkdtemp(prefix="crm_day_"))
    tmp_db = tmp_dir / "chat.db"
    try:
        if not _snapshot_apple_db(MESSAGES_DB, tmp_db):
            return jsonify({"date": date_str, "people": [], "count": 0})
        with sqlite3.connect(tmp_db) as conn:
            rows = conn.execute(
                """
                SELECT m.date, h.id, m.text, m.attributedBody, m.is_from_me
                FROM message m
                JOIN handle h ON m.handle_id = h.ROWID
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE c.style = 45 AND m.date >= ? AND m.date < ? AND h.id IS NOT NULL
                ORDER BY m.date
                """,
                (day_start_ns, day_end_ns),
            ).fetchall()
    except Exception as e:
        print(f"[activity-day] error: {e}")
        return jsonify({"date": date_str, "people": [], "count": 0})
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    crm_names: dict[str, str] = {}
    with sqlite3.connect(DB_PATH) as conn:
        for name, phone, email in conn.execute("SELECT name, phone, email FROM contacts").fetchall():
            for raw in (phone, email):
                k = normalize_handle(raw)
                if k and name and name != phone and name != email:
                    crm_names[k] = name

    by_key: dict = {}
    for date_ns, handle, text, ab, from_me in rows:
        key = normalize_handle(handle)
        if not key or key not in real_keys:
            continue
        info = by_key.setdefault(key, {"count": 0, "last_text": "", "last_from_me": False, "last_ns": 0})
        info["count"] += 1
        if date_ns > info["last_ns"]:
            info["last_ns"] = date_ns
            info["last_text"] = text or decode_attributed_body(ab) or ""
            info["last_from_me"] = bool(from_me)

    people = []
    for key, info in by_key.items():
        people.append({
            "key": key,
            "name": crm_names.get(key, key),
            "count": info["count"],
            "last_text": info["last_text"],
            "last_from_me": info["last_from_me"],
            "named": key in crm_names,
        })
    people.sort(key=lambda p: (-p["count"], p["name"]))
    return jsonify({"date": date_str, "people": people, "count": len(people)})


@app.route("/api/thread/<key>")
def thread(key: str):
    """Return 1:1 messages from a person (by normalized key) anchored on a date.
    Query params:
      around=YYYY-MM-DD  (optional) anchor; defaults to most recent message
      before=N           messages before the anchor (default 25, max 500)
      after=N            messages on/after the anchor (default 25, max 500)
    Counts are by EXTRACTABLE message (text or decodable attributedBody), so
    they reflect what the user actually sees in the thread."""
    around = request.args.get("around")
    try:
        before_n = max(0, min(int(request.args.get("before", 25)), 500))
    except (TypeError, ValueError):
        before_n = 25
    try:
        after_n = max(0, min(int(request.args.get("after", 25)), 500))
    except (TypeError, ValueError):
        after_n = 25

    anchor_ns = None
    if around:
        try:
            dt = datetime.fromisoformat(around).replace(
                tzinfo=timezone.utc, hour=23, minute=59, second=59, microsecond=999_999
            )
            anchor_ns = int((dt - APPLE_EPOCH).total_seconds() * 1_000_000_000)
        except ValueError:
            return jsonify({"error": "invalid date"}), 400

    if not MESSAGES_DB.exists():
        return jsonify({"messages": [], "around": around, "before": before_n, "after": after_n})

    tmp_dir = Path(tempfile.mkdtemp(prefix="crm_thread_"))
    tmp_db = tmp_dir / "chat.db"
    try:
        if not _snapshot_apple_db(MESSAGES_DB, tmp_db):
            return jsonify({"messages": [], "around": around, "before": before_n, "after": after_n})
        with sqlite3.connect(tmp_db) as conn:
            handle_rowids = [
                rowid for rowid, h_id in conn.execute("SELECT ROWID, id FROM handle").fetchall()
                if normalize_handle(h_id) == key
            ]
            if not handle_rowids:
                return jsonify({"messages": [], "around": around, "before": before_n, "after": after_n})
            placeholders = ",".join("?" * len(handle_rowids))
            rows = conn.execute(
                f"""
                SELECT m.date, m.text, m.is_from_me, m.attributedBody
                FROM message m
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE m.handle_id IN ({placeholders})
                  AND c.style = 45
                ORDER BY m.date
                """,
                handle_rowids,
            ).fetchall()
    except Exception as e:
        print(f"[thread] error: {e}")
        return jsonify({"messages": [], "around": around, "before": before_n, "after": after_n})
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Decode and drop empty-text messages first, so the count is over visible messages.
    visible = []
    for date_ns, text, from_me, ab in rows:
        body = text or decode_attributed_body(ab) or ""
        if not body:
            continue
        dt = APPLE_EPOCH + timedelta(seconds=date_ns / 1_000_000_000)
        visible.append({"date_ns": date_ns, "date": dt.isoformat(), "text": body, "from_me": bool(from_me)})

    if not visible:
        return jsonify({"messages": [], "around": around, "before": before_n, "after": after_n, "total": 0})

    # Find the anchor: latest message at/before `anchor_ns`, or last message if no anchor.
    if anchor_ns is None:
        anchor_idx = len(visible) - 1
    else:
        anchor_idx = None
        for i in range(len(visible) - 1, -1, -1):
            if visible[i]["date_ns"] <= anchor_ns:
                anchor_idx = i
                break
        if anchor_idx is None:
            anchor_idx = 0  # all messages are after the anchor date — start from beginning

    start = max(0, anchor_idx - before_n)
    end = min(len(visible), anchor_idx + after_n + 1)
    sliced = [{"date": m["date"], "text": m["text"], "from_me": m["from_me"]} for m in visible[start:end]]
    return jsonify({
        "messages": sliced,
        "around": around,
        "before": before_n,
        "after": after_n,
        "total": len(visible),
        "anchor_index": anchor_idx,
        "returned_start": start,
        "returned_end": end,
    })


@app.route("/api/contacts/<int:cid>/push-to-apple", methods=["POST"])
def push_to_apple(cid: int):
    """Write the CRM's name into macOS Contacts for this person.
    Looks them up by phone/email and updates the contact's name.
    First call may trigger a macOS permission prompt for Contacts."""
    if DEMO_MODE:
        return jsonify({"ok": False, "error": "Apple Contacts is disabled in demo mode"}), 400
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT name, phone, email FROM contacts WHERE id=?", (cid,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    name, phone, email = row
    if not name:
        return jsonify({"error": "no name set"}), 400

    target = phone or email
    if not target:
        return jsonify({"error": "no phone or email to look up"}), 400

    # Bulk JXA: fetch all names/phones/emails in 3 Apple Events calls (instead of N calls per person).
    parts = name.strip().split(maxsplit=1)
    first = parts[0]
    last = parts[1] if len(parts) > 1 else ""
    is_email = "@" in target
    # Find the matching person via bulk property reads (fast), then re-fetch by id
    # and update first AND last name with explicit saves between (works around a JXA
    # quirk where only the first property change sometimes persists).
    script = r"""
    function run(argv) {
      const target = argv[0];
      const isEmail = argv[1] === "1";
      const first = argv[2];
      const last = argv[3];
      const Contacts = Application('Contacts');
      const names = Contacts.people.name();
      const ids = Contacts.people.id();
      const phonesArr = Contacts.people.phones.value();
      const emailsArr = Contacts.people.emails.value();
      const norm = s => (s || '').replace(/\D/g, '').slice(-10);
      const wantKey = isEmail ? target.toLowerCase() : norm(target);
      for (let i = 0; i < names.length; i++) {
        let hit = false;
        if (isEmail) {
          const emails = (emailsArr[i] || []).map(e => (e || '').toLowerCase());
          hit = emails.includes(wantKey);
        } else {
          const phones = (phonesArr[i] || []).map(norm);
          hit = phones.includes(wantKey);
        }
        if (hit) {
          const matches = Contacts.people.whose({id: ids[i]})();
          if (matches.length === 0) continue;
          const p = matches[0];
          p.firstName = first;
          Contacts.save();
          p.lastName = last;
          Contacts.save();
          return JSON.stringify({ ok: true, matched: names[i], first: first, last: last });
        }
      }
      return JSON.stringify({ ok: false, error: 'no matching contact found' });
    }
    """
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script, target, "1" if is_email else "0", first, last],
            capture_output=True, text=True, timeout=90,
        )
        if result.returncode != 0:
            return jsonify({"ok": False, "error": result.stderr.strip() or "osascript failed"}), 500
        data = json.loads(result.stdout.strip() or "{}")
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


_AI_CACHE = {"data": None, "ts": 0.0}
_AI_TTL = 7 * 24 * 60 * 60  # cache AI insights for 7 days (manual ↻ regenerate to refresh)


def _build_ai_summary(exclude_ids: set | None = None) -> str:
    exclude_ids = exclude_ids or set()
    msg_stats = get_messages_stats()
    volumes = _cached_volumes()
    calls = _cached_calls()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM contacts WHERE archived IS NULL OR archived=0").fetchall()]
    for c in rows:
        c["locations"] = parse_locations(c.pop("location", None))
        c["occupations"] = parse_locations(c.pop("occupations", None))
    contacts = [merge_stats(c, msg_stats) for c in rows]
    for c in contacts:
        v = 0; call_n = 0
        for raw in (c.get("phone"), c.get("email")):
            k = normalize_handle(raw)
            if k and k in volumes: v += volumes[k]
            if k and k in calls: call_n += calls[k]["count"]
        c["msg_count_365"] = v
        c["call_count_365"] = call_n
    people = [c for c in contacts if not c.get("is_service") and not c.get("unnamed") and c.get("id") not in exclude_ids]

    occ, loc, tier_b, cad = {}, {}, {}, {}
    for c in people:
        for o in c.get("occupations") or []:
            occ[o] = occ.get(o, 0) + 1
        for l in c.get("locations") or []:
            loc[l] = loc.get(l, 0) + 1
        if c.get("tier"):
            tier_b[c["tier"]] = tier_b.get(c["tier"], 0) + 1
        if c.get("cadence_label"):
            cad[c["cadence_label"]] = cad.get(c["cadence_label"], 0) + 1

    def fmt_c(c):
        bits = [c["name"]]
        if c.get("tier"): bits.append(f"T{c['tier']}")
        if c.get("cadence_label"): bits.append(c["cadence_label"])
        if c.get("last_contact"): bits.append(f"last {c['last_contact']}")
        if c.get("locations"): bits.append("/".join(c["locations"]))
        if c.get("occupations"): bits.append("/".join(c["occupations"]))
        return " · ".join(bits)

    overdue = [c for c in people if c.get("overdue")]
    top_vol = sorted([c for c in people if c.get("msg_count_365") or c.get("call_count_365")],
                     key=lambda c: -((c.get("msg_count_365") or 0) + (c.get("call_count_365") or 0) * 5))[:20]

    def fmt_v(c):
        bits = [c["name"]]
        bits.append(f"{c.get('msg_count_365') or 0}msgs")
        if c.get("call_count_365"): bits.append(f"{c['call_count_365']}calls")
        if c.get("cadence_label"): bits.append(c["cadence_label"])
        if c.get("last_contact"): bits.append(f"last {c['last_contact']}")
        if c.get("locations"): bits.append("/".join(c["locations"]))
        if c.get("occupations"): bits.append("/".join(c["occupations"]))
        return " · ".join(bits)

    parts = [
        f"Total real people: {len(people)}",
        f"Tiers: {tier_b}  |  Cadences: {cad}",
        f"Top locations: {sorted(loc.items(), key=lambda x: -x[1])[:8]}",
        f"Top occupations: {sorted(occ.items(), key=lambda x: -x[1])[:8]}",
        f"TOP 20 BY ACTUAL VOLUME (last 365d, msgs incl. group chats + calls/FaceTime): " + "; ".join(fmt_v(c) for c in top_vol),
        f"Overdue ({len(overdue)}): " + "; ".join(fmt_c(c) for c in overdue[:25]),
    ]
    return "\n".join(parts)


@app.route("/api/ai-insights", methods=["GET"])
def ai_insights():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return jsonify({"error": f"ANTHROPIC_API_KEY not set on server. Edit {APP_DIR / '.env'} and restart."}), 503
    fresh = request.args.get("fresh") == "1"
    excludes: set[int] = set()
    if request.args.get("exclude"):
        for x in request.args["exclude"].split(","):
            x = x.strip()
            if x.isdigit():
                excludes.add(int(x))
    # Cache key incorporates excludes so different exclude sets don't collide
    cache_key = ",".join(str(i) for i in sorted(excludes))
    cached = _AI_CACHE["data"]
    if not fresh and isinstance(cached, dict) and cache_key in cached and time.time() - _AI_CACHE["ts"] < _AI_TTL:
        return jsonify({"insights": cached[cache_key], "cached": True, "age_seconds": int(time.time() - _AI_CACHE["ts"])})

    try:
        from anthropic import Anthropic
    except ImportError:
        return jsonify({"error": "anthropic SDK not installed. Run: ./venv/bin/pip install anthropic"}), 500

    summary = _build_ai_summary(excludes)
    prompt = (
        "Analyze my personal CRM. Generate 5 SHORT, SHARP insights.\n\n"
        "RULES:\n"
        "- ONE sentence each. Max ~20 words.\n"
        "- The 'TOP 20 BY ACTUAL VOLUME' list is the most reliable signal — use it more than tiers or stars (which aren't curated).\n"
        "- Be specific: name names from the data, cite cadence/gap.\n"
        "- Prefer insights that suggest action or surface a non-obvious pattern.\n"
        "- Skip generic observations and skip insights based purely on tier/starred fields.\n\n"
        f"DATA:\n{summary}\n\n"
        "Return ONLY a JSON array of 5 strings."
    )

    client = Anthropic(api_key=key)
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        return jsonify({"error": f"Claude API error: {e}"}), 502

    text = resp.content[0].text.strip()
    insights = None
    try:
        # Strip ```json fences if present
        cleaned = text
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()
        insights = json.loads(cleaned)
    except Exception:
        # Fallback: bullet lines
        insights = [
            line.lstrip("-•* 0123456789.").strip().strip('"').strip(",")
            for line in text.split("\n")
            if line.strip() and not line.strip().startswith("```")
        ]
        insights = [i for i in insights if 12 <= len(i) <= 400][:8]

    if not isinstance(_AI_CACHE["data"], dict):
        _AI_CACHE["data"] = {}
    _AI_CACHE["data"][cache_key] = insights
    _AI_CACHE["ts"] = time.time()
    return jsonify({"insights": insights, "cached": False})


_AI_CAL_CACHE = {"data": None, "ts": 0.0}


def _compute_volumes(days: int = 365) -> dict[str, int]:
    """Total message count per normalized handle, last N days, INCLUDING group chats."""
    if not MESSAGES_DB.exists():
        return {}
    cutoff_ns = int((datetime.now(timezone.utc) - timedelta(days=days) - APPLE_EPOCH).total_seconds() * 1_000_000_000)
    tmp_dir = Path(tempfile.mkdtemp(prefix="crm_vol_"))
    tmp_db = tmp_dir / "chat.db"
    try:
        if not _snapshot_apple_db(MESSAGES_DB, tmp_db):
            return {}
        with sqlite3.connect(tmp_db) as conn:
            rows = conn.execute(
                """
                SELECT h.id, COUNT(m.ROWID)
                FROM message m
                JOIN handle h ON m.handle_id = h.ROWID
                WHERE m.date > ? AND h.id IS NOT NULL
                GROUP BY h.id
                """,
                (cutoff_ns,),
            ).fetchall()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    out: dict[str, int] = defaultdict(int)
    for handle, count in rows:
        k = normalize_handle(handle)
        if k:
            out[k] += count
    return dict(out)


def _tier_for_volume(v: int) -> int | None:
    if v >= 2000: return 1
    if v >= 500: return 2
    if v >= 100: return 3
    if v >= 25: return 4
    return None


@app.route("/api/suggest-tiers", methods=["GET"])
def suggest_tiers():
    """Suggest tier 1-4 per contact based on total message volume (incl. group chats) over last 365d."""
    overwrite = request.args.get("overwrite") == "1"
    volumes = _compute_volumes(365)
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, name, phone, email, tier, is_service, archived FROM contacts"
        ).fetchall()
    out = []
    for cid, name, phone, email, cur_tier, is_service, archived in rows:
        if archived or is_service:
            continue
        vol = 0
        for raw in (phone, email):
            k = normalize_handle(raw)
            if k and k in volumes:
                vol += volumes[k]
        suggested = _tier_for_volume(vol)
        if suggested is None:
            continue
        if cur_tier and not overwrite:
            continue
        out.append({"id": cid, "name": name, "volume": vol, "suggested_tier": suggested, "current_tier": cur_tier})
    out.sort(key=lambda x: -x["volume"])
    return jsonify({"suggestions": out, "counts": {
        "tier1": sum(1 for s in out if s["suggested_tier"] == 1),
        "tier2": sum(1 for s in out if s["suggested_tier"] == 2),
        "tier3": sum(1 for s in out if s["suggested_tier"] == 3),
        "tier4": sum(1 for s in out if s["suggested_tier"] == 4),
    }})


@app.route("/api/apply-tiers", methods=["POST"])
def apply_tiers():
    overwrite = request.args.get("overwrite") == "1"
    volumes = _compute_volumes(365)
    applied = 0
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, phone, email, tier, is_service, archived FROM contacts"
        ).fetchall()
        for cid, phone, email, cur_tier, is_service, archived in rows:
            if archived or is_service: continue
            if cur_tier and not overwrite: continue
            vol = 0
            for raw in (phone, email):
                k = normalize_handle(raw)
                if k and k in volumes:
                    vol += volumes[k]
            suggested = _tier_for_volume(vol)
            if suggested is None: continue
            conn.execute("UPDATE contacts SET tier=? WHERE id=?", (suggested, cid))
            applied += 1
    return jsonify({"ok": True, "applied": applied})


@app.route("/api/ai-insights/calendar", methods=["GET"])
def ai_insights_calendar():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set on server."}), 503
    fresh = request.args.get("fresh") == "1"
    if not fresh and _AI_CAL_CACHE["data"] and time.time() - _AI_CAL_CACHE["ts"] < _AI_TTL:
        return jsonify({"insights": _AI_CAL_CACHE["data"], "cached": True, "age_seconds": int(time.time() - _AI_CAL_CACHE["ts"])})

    try:
        from anthropic import Anthropic
    except ImportError:
        return jsonify({"error": "anthropic SDK not installed."}), 500

    # Build activity summary
    days_param = max(7, min(int(request.args.get("days", 365)), 730))
    NS_PER_DAY = 86_400 * 1_000_000_000
    cutoff_ns = int((datetime.now(timezone.utc) - timedelta(days=days_param) - APPLE_EPOCH).total_seconds() * 1_000_000_000)

    if not MESSAGES_DB.exists():
        return jsonify({"error": "Messages DB not accessible."}), 500

    msg_stats = get_messages_stats()
    real_keys = _real_keys_from_stats(msg_stats)

    tmp_dir = Path(tempfile.mkdtemp(prefix="crm_aical_"))
    tmp_db = tmp_dir / "chat.db"
    try:
        if not _snapshot_apple_db(MESSAGES_DB, tmp_db):
            return jsonify({"error": "Messages DB not accessible."}), 500
        with sqlite3.connect(tmp_db) as conn:
            rows = conn.execute(
                """
                SELECT m.date, h.id
                FROM message m
                JOIN handle h ON m.handle_id = h.ROWID
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE c.style = 45 AND m.date > ? AND h.id IS NOT NULL
                """,
                (cutoff_ns,),
            ).fetchall()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Per-day distinct people (real only)
    day_people: dict[int, set[str]] = defaultdict(set)
    for date_ns, handle in rows:
        k = normalize_handle(handle)
        if k and k in real_keys:
            day_people[date_ns // NS_PER_DAY].add(k)

    today_day = int((datetime.now(timezone.utc) - APPLE_EPOCH).total_seconds() // 86_400)
    counts_per_day = {}
    for d in range(today_day - days_param + 1, today_day + 1):
        dt = (APPLE_EPOCH + timedelta(days=d)).date()
        counts_per_day[dt.isoformat()] = len(day_people.get(d, set()))

    # Aggregates
    dow_sum = [0]*7; dow_n = [0]*7
    by_month = defaultdict(int); by_month_days = defaultdict(int)
    by_week = defaultdict(int); by_week_days = defaultdict(int)
    total = 0; active = 0
    DOW_NAMES = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"]
    busiest = []
    for ds, cnt in counts_per_day.items():
        dt = datetime.fromisoformat(ds).date()
        wd = (dt.weekday() + 1) % 7  # JS-style Sun=0
        dow_sum[wd] += cnt; dow_n[wd] += 1
        mk = ds[:7]
        by_month[mk] += cnt; by_month_days[mk] += 1
        # ISO week (Monday-based)
        iso = dt.isocalendar()
        wk = f"{iso.year}-W{iso.week:02d}"
        by_week[wk] += cnt; by_week_days[wk] += 1
        total += cnt
        if cnt > 0: active += 1
        busiest.append((ds, cnt))
    busiest.sort(key=lambda x: -x[1])
    dow_avg = {DOW_NAMES[i]: (dow_sum[i] / dow_n[i] if dow_n[i] else 0) for i in range(7)}
    monthly_avg = sorted(((m, by_month[m] / by_month_days[m]) for m in by_month), key=lambda x: x[0])

    summary = "\n".join([
        f"Range: last {days_param} days. Total people-days: {total}. Active days: {active}/{len(counts_per_day)}.",
        f"Avg distinct people/day overall: {total / max(1, len(counts_per_day)):.2f}",
        f"Day-of-week avg: " + ", ".join(f"{k}={v:.1f}" for k, v in dow_avg.items()),
        f"Monthly avg/day (sorted by month): " + ", ".join(f"{m}={v:.1f}" for m, v in monthly_avg),
        f"Top 10 busiest days: " + ", ".join(f"{d}={c}" for d, c in busiest[:10]),
        f"Bottom 5 active-day counts: " + ", ".join(f"{d}={c}" for d, c in sorted(((d,c) for d,c in busiest if c > 0), key=lambda x: x[1])[:5]),
    ])

    prompt = (
        "Analyze my 1:1 messaging activity over time. Generate 5 SHORT insights.\n\n"
        "RULES:\n"
        "- ONE sentence each. Max ~20 words.\n"
        "- Find drift, anomalies, day-of-week or seasonal patterns. Cite specific dates/weeks/percentages.\n"
        "- Each insight should make me reflect or change behavior.\n"
        "- Skip generic rankings (e.g. 'Jan was your busiest month').\n\n"
        f"DATA:\n{summary}\n\n"
        "Return ONLY a JSON array of 5 strings."
    )
    client = Anthropic(api_key=key)
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        return jsonify({"error": f"Claude API error: {e}"}), 502

    text = resp.content[0].text.strip()
    insights = None
    try:
        cleaned = text
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()
        insights = json.loads(cleaned)
    except Exception:
        insights = [line.lstrip("-•* 0123456789.").strip().strip('"').strip(",") for line in text.split("\n") if line.strip() and not line.strip().startswith("```")]
        insights = [i for i in insights if 12 <= len(i) <= 400][:8]

    _AI_CAL_CACHE["data"] = insights
    _AI_CAL_CACHE["ts"] = time.time()
    return jsonify({"insights": insights, "cached": False})


# ---------------------------------------------------------------------------
# Deals + investor network
# ---------------------------------------------------------------------------

STAGES = ["pre_seed", "seed", "series_a", "later"]
DEAL_ASKS = ["my_investment", "angel_intros", "vc_intros", "advice"]
DEAL_STATUSES = ["advising", "watching", "evaluating", "committed", "invested", "passed"]
CONVICTIONS = ["A", "B", "C", "unrated"]
INVESTOR_TYPES = ["angel", "vc"]
CLOSENESS = ["close_friend", "friend", "friendly", "acquaintance"]
SHARE_BARS = ["only_best", "good_or_better", "anything_relevant"]
ACTIVITY = ["very_active", "active", "occasional", "unknown"]

# field -> (kind, allowed). kinds: text, int, date, enum, multi (enum list), tags (free list)
DEAL_FIELDS = {
    "company_name": ("text", None), "asks": ("multi", DEAL_ASKS), "status": ("enum", DEAL_STATUSES),
    "conviction": ("enum", CONVICTIONS), "my_check": ("int", None), "invested_on": ("date", None),
    "stage": ("enum", STAGES), "sectors": ("tags", None), "round_size": ("int", None),
    "round_terms": ("text", None), "deck_url": ("text", None), "one_liner": ("text", None),
    "forwardable_blurb": ("text", None), "my_take": ("text", None), "notes": ("text", None),
}
INVESTOR_FIELDS = {
    "investor_types": ("multi", INVESTOR_TYPES), "firm": ("text", None), "closeness": ("enum", CLOSENESS),
    "share_bar": ("enum", SHARE_BARS), "active": ("enum", ACTIVITY), "check_min": ("int", None),
    "check_max": ("int", None), "stages": ("multi", STAGES), "sectors": ("tags", None),
    "knows_well": ("text", None), "investor_notes": ("text", None),
}


def _clean_field(kind: str, allowed, v):
    """Coerce one incoming value; raises ValueError with a readable message on bad input.
    Blank values become None so every field stays optional."""
    if kind in ("multi", "tags"):
        if v is None or v == "":
            return None
        if isinstance(v, str):
            v = [s for s in v.split(",")]
        out, seen = [], set()
        for x in v:
            x = str(x).strip()
            if not x or x.lower() in seen:
                continue
            if kind == "multi" and x not in allowed:
                raise ValueError(f"'{x}' must be one of {allowed}")
            out.append(x); seen.add(x.lower())
        return json.dumps(out) if out else None
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if kind == "enum":
        if v not in allowed:
            raise ValueError(f"'{v}' must be one of {allowed}")
        return v
    if kind == "int":
        try:
            return int(float(str(v).replace(",", "").replace("$", "").strip()))
        except ValueError:
            raise ValueError(f"'{v}' is not a number")
    if kind == "date":
        datetime.fromisoformat(str(v))
        return str(v)[:10]
    return str(v).strip()


def _apply_fields(spec: dict, data: dict) -> tuple[list[str], list]:
    cols, vals = [], []
    for f, (kind, allowed) in spec.items():
        if f in data:
            try:
                vals.append(_clean_field(kind, allowed, data[f]))
            except ValueError as e:
                raise ValueError(f"{f}: {e}")
            cols.append(f)
    return cols, vals


def _decode_lists(row: dict, spec: dict) -> dict:
    for f, (kind, _) in spec.items():
        if kind in ("multi", "tags"):
            row[f] = parse_locations(row.get(f))
    return row


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _deal_people(conn, deal_ids: list[int]) -> dict[int, dict[str, list]]:
    out: dict[int, dict[str, list]] = {i: {"founders": [], "shared_with": []} for i in deal_ids}
    if not deal_ids:
        return out
    ph = ",".join("?" * len(deal_ids))
    for r in conn.execute(
        f"""SELECT dc.deal_id, dc.role, c.id, c.name, c.email, c.phone
            FROM deal_contacts dc JOIN contacts c ON c.id = dc.contact_id
            WHERE dc.deal_id IN ({ph}) ORDER BY dc.created_at, c.name""",
        deal_ids,
    ):
        key = "founders" if r["role"] == "founder" else "shared_with"
        out[r["deal_id"]][key].append({"id": r["id"], "name": r["name"], "email": r["email"], "phone": r["phone"]})
    return out


def _load_deals(conn, where: str = "", args: tuple = ()) -> list[dict]:
    rows = [_decode_lists(dict(r), DEAL_FIELDS) for r in conn.execute(
        f"SELECT * FROM deals {where} ORDER BY updated_at DESC", args)]
    people = _deal_people(conn, [r["id"] for r in rows])
    for r in rows:
        r.update(people[r["id"]])
    return rows


def _touch_deal(conn, deal_id: int) -> None:
    conn.execute("UPDATE deals SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (deal_id,))


@app.route("/deals")
def deals_page():
    return send_from_directory(APP_DIR / "static", "deals.html")


@app.route("/api/deal-options")
def deal_options():
    return jsonify({
        "stages": STAGES, "asks": DEAL_ASKS, "statuses": DEAL_STATUSES, "convictions": CONVICTIONS,
        "investor_types": INVESTOR_TYPES, "closeness": CLOSENESS, "share_bars": SHARE_BARS, "activity": ACTIVITY,
    })


@app.route("/api/contacts/search")
def search_contacts():
    """Lightweight name search for pickers (skips the Messages stats the full list computes)."""
    q = (request.args.get("q") or "").strip()
    with _db() as conn:
        rows = conn.execute(
            """SELECT id, name, email, phone, occupations FROM contacts
               WHERE archived = 0 AND name LIKE ? ORDER BY name COLLATE NOCASE LIMIT 20""",
            (f"%{q}%",),
        ).fetchall()
    return jsonify([{**dict(r), "occupations": parse_locations(r["occupations"])} for r in rows])


@app.route("/api/deals", methods=["GET"])
def list_deals():
    with _db() as conn:
        return jsonify(_load_deals(conn))


@app.route("/api/deals", methods=["POST"])
def create_deal():
    data = request.get_json(force=True)
    founder_ids = [int(x) for x in (data.get("founder_ids") or [])]
    if not (data.get("company_name") or "").strip() and not founder_ids:
        return jsonify({"error": "company name or a founder is required"}), 400
    try:
        cols, vals = _apply_fields(DEAL_FIELDS, data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    with _db() as conn:
        if cols:
            cur = conn.execute(f"INSERT INTO deals ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        else:
            cur = conn.execute("INSERT INTO deals DEFAULT VALUES")
        deal_id = cur.lastrowid
        for cid in founder_ids:
            conn.execute("INSERT OR IGNORE INTO deal_contacts (deal_id, contact_id, role) VALUES (?, ?, 'founder')", (deal_id, cid))
    return jsonify({"id": deal_id}), 201


@app.route("/api/deals/<int:deal_id>", methods=["GET"])
def get_deal(deal_id: int):
    with _db() as conn:
        rows = _load_deals(conn, "WHERE id=?", (deal_id,))
    if not rows:
        return jsonify({"error": "not found"}), 404
    return jsonify(rows[0])


@app.route("/api/deals/<int:deal_id>", methods=["PUT"])
def update_deal(deal_id: int):
    """Partial update, same contract as update_contact: only fields present are touched."""
    data = request.get_json(force=True)
    try:
        cols, vals = _apply_fields(DEAL_FIELDS, data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    with _db() as conn:
        if cols:
            conn.execute(
                f"UPDATE deals SET {', '.join(c + '=?' for c in cols)}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                vals + [deal_id],
            )
        rows = _load_deals(conn, "WHERE id=?", (deal_id,))
    if not rows:
        return jsonify({"error": "not found"}), 404
    return jsonify(rows[0])


@app.route("/api/deals/<int:deal_id>", methods=["DELETE"])
def delete_deal(deal_id: int):
    with _db() as conn:
        conn.execute("DELETE FROM deal_contacts WHERE deal_id=?", (deal_id,))
        conn.execute("DELETE FROM deals WHERE id=?", (deal_id,))
    return jsonify({"ok": True})


@app.route("/api/deals/<int:deal_id>/people/<role>", methods=["POST"])
def add_deal_person(deal_id: int, role: str):
    """Body: {contact_id}. role = founder | shared."""
    if role not in ("founder", "shared"):
        return jsonify({"error": "role must be founder or shared"}), 404
    cid = int(request.get_json(force=True).get("contact_id"))
    with _db() as conn:
        conn.execute("INSERT OR IGNORE INTO deal_contacts (deal_id, contact_id, role) VALUES (?, ?, ?)", (deal_id, cid, role))
        _touch_deal(conn, deal_id)
    return jsonify({"ok": True})


@app.route("/api/deals/<int:deal_id>/people/<role>/<int:cid>", methods=["DELETE"])
def remove_deal_person(deal_id: int, role: str, cid: int):
    with _db() as conn:
        conn.execute("DELETE FROM deal_contacts WHERE deal_id=? AND contact_id=? AND role=?", (deal_id, cid, role))
        _touch_deal(conn, deal_id)
    return jsonify({"ok": True})


def _load_investors(conn, where: str = "", args: tuple = ()) -> list[dict]:
    rows = conn.execute(
        f"""SELECT p.*, c.name, c.email, c.phone, c.tier, c.occupations, c.location,
                   (SELECT COUNT(*) FROM deal_contacts dc WHERE dc.contact_id = p.contact_id AND dc.role='shared') AS deals_seen
            FROM investor_profiles p JOIN contacts c ON c.id = p.contact_id
            {where} ORDER BY c.name COLLATE NOCASE""",
        args,
    ).fetchall()
    out = []
    for r in rows:
        d = _decode_lists(dict(r), INVESTOR_FIELDS)
        d["occupations"] = parse_locations(d.get("occupations"))
        d["locations"] = parse_locations(d.pop("location", None))
        out.append(d)
    return out


@app.route("/api/investors")
def list_investors():
    with _db() as conn:
        return jsonify(_load_investors(conn))


@app.route("/api/investors/<int:cid>", methods=["GET"])
def get_investor(cid: int):
    """Profile (or null) for one contact, plus the deals they've been shown. Works for
    any contact so the contact modal can offer 'add investor profile'."""
    with _db() as conn:
        contact = conn.execute("SELECT id, name, email, phone, occupations FROM contacts WHERE id=?", (cid,)).fetchone()
        if not contact:
            return jsonify({"error": "not found"}), 404
        prof = _load_investors(conn, "WHERE p.contact_id=?", (cid,))
        seen = conn.execute(
            """SELECT d.id, d.company_name, d.status, d.conviction, dc.created_at AS shared_at,
                      (SELECT GROUP_CONCAT(c.name, ' & ') FROM deal_contacts f JOIN contacts c ON c.id = f.contact_id
                       WHERE f.deal_id = d.id AND f.role = 'founder') AS founder_names
               FROM deal_contacts dc JOIN deals d ON d.id = dc.deal_id
               WHERE dc.contact_id=? AND dc.role='shared' ORDER BY dc.created_at DESC""",
            (cid,),
        ).fetchall()
        founded = conn.execute(
            """SELECT d.id, d.company_name, d.status FROM deal_contacts dc JOIN deals d ON d.id = dc.deal_id
               WHERE dc.contact_id=? AND dc.role='founder'""",
            (cid,),
        ).fetchall()
    c = dict(contact)
    c["occupations"] = parse_locations(c["occupations"])
    return jsonify({
        "contact": c,
        "profile": prof[0] if prof else None,
        "deals_seen": [dict(r) for r in seen],
        "deals_founded": [dict(r) for r in founded],
    })


@app.route("/api/investors/<int:cid>", methods=["PUT"])
def upsert_investor(cid: int):
    data = request.get_json(force=True)
    try:
        cols, vals = _apply_fields(INVESTOR_FIELDS, data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    with _db() as conn:
        if not conn.execute("SELECT 1 FROM contacts WHERE id=?", (cid,)).fetchone():
            return jsonify({"error": "contact not found"}), 404
        conn.execute("INSERT OR IGNORE INTO investor_profiles (contact_id) VALUES (?)", (cid,))
        if cols:
            conn.execute(
                f"UPDATE investor_profiles SET {', '.join(c + '=?' for c in cols)}, updated_at=CURRENT_TIMESTAMP WHERE contact_id=?",
                vals + [cid],
            )
        return jsonify(_load_investors(conn, "WHERE p.contact_id=?", (cid,))[0])


@app.route("/api/investors/<int:cid>", methods=["DELETE"])
def delete_investor(cid: int):
    with _db() as conn:
        conn.execute("DELETE FROM investor_profiles WHERE contact_id=?", (cid,))
    return jsonify({"ok": True})


# How selective you are with someone → which conviction levels they get to see.
_BAR_ALLOWS = {"only_best": {"A"}, "good_or_better": {"A", "B"}, "anything_relevant": {"A", "B"}}
_CLOSENESS_BONUS = {"close_friend": 1.5, "friend": 1.0, "friendly": 0.5}
_ACTIVE_BONUS = {"very_active": 1.0, "active": 0.5}


def _rank_investor(deal: dict, inv: dict) -> dict:
    """Score one investor against one deal. Missing data never excludes anyone — it only
    earns no points. Returns {score, fit, reasons, flags}. `fit` False means a known mismatch
    (type or stage); those only show under 'show all'."""
    score, reasons, flags, fit = 0.0, [], [], True
    asks = set(deal.get("asks") or [])
    wants = ({"angel"} if "angel_intros" in asks else set()) | ({"vc"} if "vc_intros" in asks else set())
    types = set(inv.get("investor_types") or [])
    if wants and types:
        if wants & types:
            score += 3; reasons.append("/".join(sorted(t.upper() if t == "vc" else t for t in wants & types)) + " intro ask")
        else:
            fit = False; flags.append("type doesn't match ask")
    stage, inv_stages = deal.get("stage"), set(inv.get("stages") or [])
    if stage and inv_stages:
        if stage in inv_stages:
            score += 2; reasons.append(f"invests {stage.replace('_', '-')}")
        else:
            fit = False; flags.append(f"doesn't do {stage.replace('_', '-')}")
    overlap = {s.lower() for s in deal.get("sectors") or []} & {s.lower() for s in inv.get("sectors") or []}
    if overlap:
        score += 2; reasons.append(", ".join(sorted(overlap)))
    if inv.get("closeness") in _CLOSENESS_BONUS:
        score += _CLOSENESS_BONUS[inv["closeness"]]; reasons.append(inv["closeness"].replace("_", " "))
    score += _ACTIVE_BONUS.get(inv.get("active"), 0)
    cmin = inv.get("check_min")
    if cmin:
        rs = deal.get("round_size")
        if (rs and cmin > rs * 0.1) or (not rs and cmin >= 25000 and stage in (None, "pre_seed", "seed")):
            flags.append(f"min check ${cmin:,}")
    return {"score": score, "fit": fit, "reasons": reasons, "flags": flags}


@app.route("/api/deals/<int:deal_id>/suggestions")
def deal_suggestions(deal_id: int):
    """Who should see this deal? Share bar is a hard rule (an only_best person never
    appears for a B deal, even with all=1); type/stage mismatches hide unless all=1."""
    show_all = request.args.get("all") == "1"
    with _db() as conn:
        rows = _load_deals(conn, "WHERE id=?", (deal_id,))
        if not rows:
            return jsonify({"error": "not found"}), 404
        deal = rows[0]
        investors = _load_investors(conn)
    conviction = deal.get("conviction") or "unrated"
    if conviction == "C":
        return jsonify({"blocked": True, "reason": "This deal is rated C (don't share).", "investors": []})
    effective = "B" if conviction == "unrated" else conviction
    seen_ids = {p["id"] for p in deal["shared_with"]}
    founder_ids = {p["id"] for p in deal["founders"]}
    out, hidden_by_bar, hidden_by_fit = [], 0, 0
    for inv in investors:
        if inv["contact_id"] in founder_ids:
            continue
        if effective not in _BAR_ALLOWS.get(inv.get("share_bar") or "good_or_better", {"A", "B"}):
            hidden_by_bar += 1
            continue
        r = _rank_investor(deal, inv)
        if not r["fit"] and not show_all:
            hidden_by_fit += 1
            continue
        out.append({
            "contact_id": inv["contact_id"], "name": inv["name"], "email": inv["email"], "phone": inv["phone"],
            "firm": inv.get("firm"), "investor_types": inv.get("investor_types"), "closeness": inv.get("closeness"),
            "seen": inv["contact_id"] in seen_ids, **r,
        })
    # Fits first, unseen before seen, then score.
    out.sort(key=lambda x: (not x["fit"], x["seen"], -x["score"], (x["name"] or "").lower()))
    return jsonify({
        "blocked": False,
        "unrated": conviction == "unrated",
        "investors": out,
        "hidden_by_bar": hidden_by_bar,
        "hidden_by_fit": hidden_by_fit,
    })


# ---------------------------------------------------------------------------
# Portfolio: my angel investments, pipeline, who to forward deals to.
# Standalone on purpose — no links into contacts.
# ---------------------------------------------------------------------------

# table -> {column: kind}. kinds: text, int, date
PORTFOLIO_TABLES = {
    "investments": {
        "company": "text", "legal_name": "text", "kind": "text", "status": "text", "bucket": "text",
        "invested_on": "date", "amount": "int", "vehicle": "text",
        "entry_valuation": "text", "entry_stage": "text",
        "current_valuation": "text", "current_stage": "text", "current_value": "int", "returned": "int",
        "valuation_as_of": "text", "valuation_source": "text",
        # Funds (LP positions): amount = committed; called = paid in so far.
        "vintage": "text", "fund_size": "text", "called": "int",
        "source": "text", "why": "text", "learnings": "text", "gained": "text", "notes": "text",
    },
    # amount + bucket = money set aside for a deal I'm investing in but haven't wired yet
    # timing: when I think a tracked person might start a company (Now, 3 mo+, 6 mo+, ...)
    "pipeline": {"name": "text", "list": "text", "note": "text", "amount": "int", "bucket": "text", "timing": "text", "potential": "text"},
    "forward_to": {"name": "text", "kind": "text", "note": "text"},
    # Live deals I'm deciding on. Commit turns one into a pending investment; Pass moves it to pipeline/passed.
    "evaluating": {"company": "text", "round": "text", "valuation": "text", "check_size": "int", "deadline": "date",
                   "source": "text", "next_step": "text", "note": "text"},
}
PORTFOLIO_DEFAULT_RULES = {
    "annual_budget": 100000,
    "early": {"check": 10000, "per_year": 15},
    "mid": {"check": 25000, "per_year": 5},
    "late": {"check": 50000, "per_year": 5},
}


def init_portfolio_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        for table, cols in PORTFOLIO_TABLES.items():
            col_sql = ", ".join(f"{c} {'INTEGER' if k == 'int' else 'TEXT'}" for c, k in cols.items())
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {table} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, {col_sql},
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"""
            )
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for c, k in cols.items():
                if c not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {c} {'INTEGER' if k == 'int' else 'TEXT'}")
        conn.execute("CREATE TABLE IF NOT EXISTS portfolio_settings (key TEXT PRIMARY KEY, value TEXT)")


def _portfolio_values(table: str, data: dict) -> tuple[list[str], list]:
    cols, vals = [], []
    for c, kind in PORTFOLIO_TABLES[table].items():
        if c not in data:
            continue
        v = data[c]
        if isinstance(v, str):
            v = v.strip()
        if v in ("", None):
            v = None
        elif kind == "int":
            try:
                v = int(float(str(v).replace(",", "").replace("$", "")))
            except ValueError:
                raise ValueError(f"{c}: '{v}' is not a number")
        elif kind == "date":
            try:
                v = datetime.fromisoformat(str(v)).date().isoformat()
            except ValueError:
                raise ValueError(f"{c}: '{v}' is not a date (YYYY-MM-DD)")
        cols.append(c); vals.append(v)
    return cols, vals


@app.route("/portfolio")
def portfolio_page():
    return send_from_directory(APP_DIR / "static", "portfolio.html")


@app.route("/api/portfolio/settings", methods=["GET", "PUT"])
def portfolio_settings():
    with _db() as conn:
        if request.method == "PUT":
            conn.execute(
                "INSERT OR REPLACE INTO portfolio_settings (key, value) VALUES ('rules', ?)",
                (json.dumps(request.get_json(force=True).get("rules")),),
            )
        row = conn.execute("SELECT value FROM portfolio_settings WHERE key='rules'").fetchone()
    return jsonify({"rules": json.loads(row["value"]) if row else PORTFOLIO_DEFAULT_RULES})


@app.route("/api/portfolio/<table>", methods=["GET", "POST"])
def portfolio_rows(table: str):
    if table not in PORTFOLIO_TABLES:
        return jsonify({"error": "unknown table"}), 404
    with _db() as conn:
        if request.method == "POST":
            try:
                cols, vals = _portfolio_values(table, request.get_json(force=True))
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            if not cols:
                return jsonify({"error": "empty row"}), 400
            cur = conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
            return jsonify(dict(conn.execute(f"SELECT * FROM {table} WHERE id=?", (cur.lastrowid,)).fetchone())), 201
        return jsonify([dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")])


@app.route("/api/portfolio/<table>/<int:row_id>", methods=["PUT", "DELETE"])
def portfolio_row(table: str, row_id: int):
    if table not in PORTFOLIO_TABLES:
        return jsonify({"error": "unknown table"}), 404
    with _db() as conn:
        if request.method == "DELETE":
            conn.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))
            return jsonify({"ok": True})
        try:
            cols, vals = _portfolio_values(table, request.get_json(force=True))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if cols:
            conn.execute(
                f"UPDATE {table} SET {', '.join(c + '=?' for c in cols)}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                vals + [row_id],
            )
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
    return jsonify(dict(row)) if row else (jsonify({"error": "not found"}), 404)


VALUATION_PROMPT = """Find the most recent publicly reported valuation and funding stage for this private company I invested in.

Company: {company}{legal}
What I know: invested {invested_on} at {entry_stage} stage, valuation then {entry_valuation}. Notes: {notes}

Search the web; one or two searches are usually enough. Names like this are often ambiguous, so make sure the company you find matches what I know (stage and valuation at the time I invested should be consistent with its history). Prefer funding announcements, reputable press (TechCrunch, Bloomberg, The Information, Reuters) and the company's own posts. A secondary-market or tender price counts if it's the most recent figure; say so in the summary.

If you can't find anything newer than what I already know, or you're not confident it's the same company, say found=false. Never estimate or guess a number.

End your reply with only this JSON object:
{{"found": true|false, "valuation": "<short post-money figure only, e.g. $350B or $45M, or null>", "valuation_usd": <number or null>, "stage": "<e.g. Series A, Seed, Public, Acquired, or null>", "as_of": "<YYYY-MM or YYYY-MM-DD, or null>", "source_url": "<url or null>", "note": "<only if there is a caveat, under 6 words, e.g. pending Series C, IPO filed, tender offer price, acquired by X; otherwise null>"}}"""


def _lookup_valuation(inv: dict) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(f"ANTHROPIC_API_KEY not set. Edit {APP_DIR / '.env'} and restart.")
    from anthropic import Anthropic
    client = Anthropic(api_key=key)
    prompt = VALUATION_PROMPT.format(
        company=inv["company"],
        legal=f" (legal name: {inv['legal_name']})" if inv.get("legal_name") else "",
        invested_on=inv.get("invested_on") or "unknown date",
        entry_stage=inv.get("entry_stage") or "unknown",
        entry_valuation=inv.get("entry_valuation") or "unknown",
        notes=inv.get("notes") or "none",
    )
    messages = [{"role": "user", "content": prompt}]
    # Web search can pause a long turn; resume by sending the partial turn back (max a few times).
    for _ in range(4):
        resp = client.beta.messages.create(
            model="claude-opus-5-5",
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default"},
            output_config={"effort": "low"},
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}],
            messages=messages,
        )
        if resp.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": resp.content}]
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined this lookup")
    text = "".join(b.text for b in resp.content if b.type == "text")
    start, end = text.rfind("{"), text.rfind("}")
    # The JSON object is the last thing in the reply; take the outermost braces before the end.
    depth, i = 0, end
    while i >= 0:
        depth += text[i] == "}"
        depth -= text[i] == "{"
        if depth == 0:
            start = i
            break
        i -= 1
    try:
        out = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        raise RuntimeError("Couldn't read the lookup result")
    return out


@app.route("/api/portfolio/investments/<int:row_id>/lookup", methods=["POST"])
def portfolio_lookup(row_id: int):
    """Propose a fresh valuation/stage from a web search. Writes nothing — the page shows
    the proposal and saves only what you accept."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM investments WHERE id=?", (row_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    try:
        return jsonify(_lookup_valuation(dict(row)))
    except Exception as e:  # surface API/key problems to the page instead of a bare 500
        return jsonify({"error": str(e)}), 502


# ---------------------------------------------------------------------------
# Public-market benchmark: what the same dollars, on the same dates, would be worth in an index.
# ---------------------------------------------------------------------------

BENCHMARKS = {
    "sp500": ("S&P 500", ["SPY"]),
    "nasdaq": ("Nasdaq-100", ["QQQ"]),
    "mag7": ("Magnificent 7", ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"]),
    "nvda": ("Nvidia", ["NVDA"]),
}
# Weekly is plenty for this comparison, and Yahoo is slow (~9s per symbol for 10y weekly), so
# prices are fetched once a day and kept on disk; the server warms them in the background at start.
PRICE_CACHE_FILE = APP_DIR / "price_cache.json"
_PRICE_CACHE: dict[str, tuple[str, list[str], list[float]]] = {}
try:
    _PRICE_CACHE.update({k: tuple(v) for k, v in json.loads(PRICE_CACHE_FILE.read_text()).items()})
except (OSError, ValueError):
    pass


def _price_series(sym: str) -> tuple[list[str], list[float]]:
    """Weekly adjusted closes (dividends reinvested) for ~10 years, from Yahoo Finance. Cached per day."""
    import urllib.request
    today = datetime.now().strftime("%Y-%m-%d")
    hit = _PRICE_CACHE.get(sym)
    if hit and hit[0] == today:
        return hit[1], hit[2]
    req = urllib.request.Request(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=10y&interval=1wk",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    import ssl, certifi  # python.org's macOS Python has no system CA certs; certifi ships with the SDK
    with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context(cafile=certifi.where())) as r:
        res = json.load(r)["chart"]["result"][0]
    pairs = [
        (datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"), v)
        for t, v in zip(res["timestamp"], res["indicators"]["adjclose"][0]["adjclose"]) if v
    ]
    dates, closes = [d for d, _ in pairs], [v for _, v in pairs]
    _PRICE_CACHE[sym] = (today, dates, closes)
    try:
        PRICE_CACHE_FILE.write_text(json.dumps(_PRICE_CACHE))
    except OSError:
        pass
    return dates, closes


def _all_series(syms: list[str]) -> dict[str, tuple[list[str], list[float]]]:
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip(syms, pool.map(_price_series, syms)))


def _warm_prices() -> None:
    try:
        _all_series(sorted({s for _, syms in BENCHMARKS.values() for s in syms}))
    except Exception as e:
        print(f"[prices] warm-up failed: {e}", flush=True)


@app.route("/api/benchmark/<key>", methods=["POST"])
def benchmark(key: str):
    """Body {dates: [YYYY-MM-DD...]}. Returns each symbol's close on (or just before) each date,
    plus the latest close, so the page can compute what each check would be worth in the index."""
    import bisect
    if key not in BENCHMARKS:
        return jsonify({"error": "unknown benchmark"}), 404
    label, syms = BENCHMARKS[key]
    dates = [d for d in (request.get_json(force=True).get("dates") or []) if d]
    try:
        series = _all_series(syms)
    except Exception as e:
        return jsonify({"error": f"Couldn't load prices: {e}"}), 502
    at = {}
    for d in dates:
        at[d] = {}
        for s, (ds, cs) in series.items():
            i = bisect.bisect_right(ds, d) - 1
            at[d][s] = cs[max(i, 0)]
    return jsonify({
        "label": label, "symbols": syms,
        "as_of": min(ds[-1] for ds, _ in series.values()),
        "latest": {s: cs[-1] for s, (_, cs) in series.items()},
        "at": at,
    })


BACKUP_DIR = APP_DIR / "backups"
BACKUP_KEEP = 30


def backup_db() -> Path | None:
    """One consistent copy of crm.db per day in backups/ (gitignored), keeping the last 30.
    Uses SQLite's online backup so it's safe while the server is writing."""
    BACKUP_DIR.mkdir(exist_ok=True)
    dest = BACKUP_DIR / f"{DB_PATH.stem}-{datetime.now().strftime('%Y-%m-%d')}.db"
    if dest.exists():
        return None
    with sqlite3.connect(DB_PATH) as src, sqlite3.connect(dest) as dst:
        src.backup(dst)
    for old in sorted(BACKUP_DIR.glob(f"{DB_PATH.stem}-*.db"))[:-BACKUP_KEEP]:
        old.unlink()
    return dest


def _backup_loop() -> None:
    while True:
        try:
            made = backup_db()
            if made:
                print(f"[backup] wrote {made.name}", flush=True)
        except Exception as e:  # never let a backup problem take the server down
            print(f"[backup] failed: {e}", flush=True)
        time.sleep(6 * 60 * 60)


@app.route("/api/portfolio/investments.csv")
def portfolio_csv():
    import csv, io
    cols = ["company", "status", "kind", "source", "invested_on", "amount", "called", "vehicle",
            "entry_stage", "entry_valuation", "current_stage", "current_valuation", "current_value",
            "returned", "vintage", "fund_size", "why", "valuation_as_of", "valuation_source"]
    with _db() as conn:
        rows = conn.execute(f"SELECT {', '.join(cols)} FROM investments ORDER BY invested_on").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    w.writerows([tuple(r) for r in rows])
    return app.response_class(buf.getvalue(), mimetype="text/csv",
                              headers={"Content-Disposition": f"attachment; filename=private-investments-{datetime.now():%Y-%m-%d}.csv"})


@app.route("/api/health")
def health():
    # os.access() lies under TCC - it reports the mode bits, not the sandbox
    # verdict - so actually open the file to find out whether we can read it.
    readable = False
    if MESSAGES_DB.exists():
        try:
            with open(MESSAGES_DB, "rb") as fh:
                fh.read(16)
            readable = True
        except OSError:
            readable = False
    return jsonify({
        "messages_db_accessible": readable,
        "messages_db_path": str(MESSAGES_DB),
        "python_executable": sys.executable,
    })


if __name__ == "__main__":
    init_db()
    init_portfolio_db()
    import threading
    if not DEMO_MODE:
        threading.Thread(target=_backup_loop, daemon=True).start()
    threading.Thread(target=_warm_prices, daemon=True).start()
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5555)), debug=False)
