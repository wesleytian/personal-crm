"""One-off seed for the Deals + Investors module against an existing contacts DB.

Matches names against existing contacts; only creates a contact where noted
("create" = no phone/email, "create_from_macos" = copy the phone from macOS Contacts).
Safe to re-run: existing investor profiles and deals are left alone.
The names below are fictional examples; edit them to match your own contacts.
(To try the app with a full fake dataset instead, see seed_demo.py.)

    ./venv/bin/python seed_deals.py           # dry run, prints the plan
    ./venv/bin/python seed_deals.py --apply   # writes
"""
import json
import sqlite3
import sys

import app as crm

# (search name, expected contact id or None to require a unique match, profile fields)
INVESTORS = [
    ("Avery Lindqvist", None, {"firm": "Bluebird Capital", "investor_types": ["vc"], "closeness": "close_friend"}),
    ("Jonah Okafor", None, {"firm": "Ironwood Partners", "investor_types": ["vc", "angel"], "closeness": "friend"}),
    ("Rosa", "create", {"firm": "Lanternfish Ventures", "investor_types": ["vc", "angel"], "closeness": "friend"}),
    ("Dmitri Halvorsen", None, {"investor_types": ["angel"], "investor_notes": "very hands-on with early hiring"}),
    ("Keiko Marchetti", None, {"investor_types": ["angel"], "check_min": 50000, "knows_well": "developer tools"}),
]

# founder name -> deal fields (+ shared_with names)
DEALS = [
    ("Priya Ramaswamy", {"status": "invested", "asks": ["angel_intros"]}, []),
    ("Tobias Wren", {"status": "evaluating", "asks": ["my_investment"], "one_liner": "warehouse robots with remote teleop fallback"}, []),
    ("Lena Morozova", {"status": "committed"}, ["Jonah Okafor"]),
]

# (contact id, current name, new name): fix contacts saved under a nickname before matching.
RENAMES: list[tuple[int, str, str]] = []


def main(apply: bool) -> None:
    crm.init_db()
    conn = sqlite3.connect(crm.DB_PATH)
    conn.row_factory = sqlite3.Row
    unresolved: list[str] = []
    ids: dict[str, int] = {}

    for cid, old, new in RENAMES:
        row = conn.execute("SELECT name FROM contacts WHERE id=?", (cid,)).fetchone()
        if row and row["name"] == old:
            print(f"rename   #{cid} {old!r} -> {new!r}")
            if apply:
                conn.execute("UPDATE contacts SET name=? WHERE id=?", (new, cid))
        elif row and row["name"] == new:
            pass
        else:
            unresolved.append(f"rename #{cid}: expected {old!r}, found {row['name'] if row else None!r}")
    if not apply:  # let the dry run resolve the post-rename name
        for cid, old, new in RENAMES:
            ids[new] = cid

    def resolve(name: str, expected) -> int | None:
        if name in ids:
            return ids[name]
        rows = conn.execute(
            "SELECT id, name FROM contacts WHERE name LIKE ? COLLATE NOCASE", (f"%{name}%",)
        ).fetchall()
        exact = [r for r in rows if r["name"].lower() == name.lower()]
        if isinstance(expected, int):
            hit = next((r for r in rows if r["id"] == expected), None)
            if not hit:
                unresolved.append(f"{name}: expected #{expected}, not found")
                return None
            ids[name] = expected
            return expected
        if len(exact) == 1:
            ids[name] = exact[0]["id"]
            return exact[0]["id"]
        if expected in ("create", "create_from_macos") and not exact:
            phone = None
            if expected == "create_from_macos":
                mac = [p for p in crm.get_macos_contacts() if p["name"].lower() == name.lower()]
                if len(mac) != 1:
                    unresolved.append(f"{name}: {len(mac)} matches in macOS Contacts")
                    return None
                phone = (mac[0]["phones"] or [None])[0]
            print(f"create   contact {name!r}" + (" (phone from macOS Contacts)" if phone else " (no phone/email)"))
            if not apply:
                ids[name] = -1
                return -1
            cur = conn.execute("INSERT INTO contacts (name, phone) VALUES (?, ?)", (name, phone))
            ids[name] = cur.lastrowid
            return cur.lastrowid
        unresolved.append(f"{name}: {len(rows)} matches {[(r['id'], r['name']) for r in rows]}")
        return None

    for name, expected, fields in INVESTORS:
        cid = resolve(name, expected)
        if cid is None:
            continue
        if cid > 0 and conn.execute("SELECT 1 FROM investor_profiles WHERE contact_id=?", (cid,)).fetchone():
            print(f"skip     investor {name} (#{cid}) already has a profile")
            continue
        print(f"investor {name} (#{cid}): {fields}")
        if apply:
            cols, vals = crm._apply_fields(crm.INVESTOR_FIELDS, fields)
            conn.execute(
                f"INSERT INTO investor_profiles (contact_id, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
                [cid] + vals,
            )

    for founder, fields, shared in DEALS:
        fid = resolve(founder, None)
        if fid is None:
            continue
        if fid > 0 and conn.execute(
            "SELECT 1 FROM deal_contacts WHERE contact_id=? AND role='founder'", (fid,)
        ).fetchone():
            print(f"skip     deal for {founder} (#{fid}) already exists")
            continue
        shared_ids = [resolve(s, None) for s in shared]
        print(f"deal     founder {founder} (#{fid}): {fields}, shared_with {shared}")
        if apply:
            cols, vals = crm._apply_fields(crm.DEAL_FIELDS, fields)
            deal_id = conn.execute(
                f"INSERT INTO deals ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals
            ).lastrowid
            conn.execute("INSERT INTO deal_contacts (deal_id, contact_id, role) VALUES (?, ?, 'founder')", (deal_id, fid))
            for sid in shared_ids:
                if sid:
                    conn.execute("INSERT OR IGNORE INTO deal_contacts (deal_id, contact_id, role) VALUES (?, ?, 'shared')", (deal_id, sid))

    if apply:
        conn.commit()
    conn.close()
    print("\nUnresolved — fix by hand:" if unresolved else "\nAll names resolved.")
    for u in unresolved:
        print("  -", u)
    if not apply:
        print("\n(dry run — re-run with --apply to write)")


if __name__ == "__main__":
    main("--apply" in sys.argv)
