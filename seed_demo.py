"""Build a fake demo dataset so the app can be run and screenshotted without any real data.

Creates (or replaces) three files next to app.py, all gitignored:
    demo.db            contacts, deals, investors, portfolio  (stands in for crm.db)
    demo_messages.db   fake 1:1 + group texts in the macOS Messages schema
    demo_calls.db      fake phone/FaceTime calls in the macOS call-history schema

Every person, company, fund, phone number (555-01xx) and email (example.com) is made up.
It never opens crm.db or anything under ~/Library.

    ./venv/bin/python seed_demo.py
    CRM_DEMO=1 PORT=5556 ./venv/bin/python app.py      # then open http://127.0.0.1:5556
"""
import json
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["CRM_DEMO"] = "1"  # must be set before importing app so it points at demo.db
import app as crm  # noqa: E402

APP_DIR = Path(__file__).parent
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
NOW = datetime.now(timezone.utc)
rng = random.Random(42)

# name, tier, cadence, locations, occupations, starred, trajectory, chemistry, notes
PEOPLE = [
    ("Avery Lindqvist", 1, "weekly", ["SF"], ["Investor"], 1, 3, 3, "Partner at Bluebird. Met at a robotics dinner."),
    ("Jonah Okafor", 1, "weekly", ["NYC"], ["Investor", "Founder"], 1, 3, 3, "Ex-founder, now at Ironwood. Great on pricing."),
    ("Priya Ramaswamy", 1, "daily", ["SF"], ["Founder"], 1, 3, 3, "Founder of Lumen Robotics."),
    ("Tobias Wren", 2, "weekly", ["Boston"], ["Founder", "Researcher"], 0, 3, 2, "Warehouse robotics. PhD dropout."),
    ("Lena Morozova", 2, "monthly", ["NYC"], ["Founder"], 1, 2, 3, "Second-time founder, fintech infra."),
    ("Marco Lindgren", 2, "monthly", ["LA"], ["Investor"], 0, 2, 2, "Angel, consumer + creator tools."),
    ("Keiko Marchetti", 2, "monthly", ["Seattle"], ["Investor", "Engineer"], 1, 3, 2, "Angel. Ex-staff engineer at a big cloud co."),
    ("Dmitri Halvorsen", 2, "quarterly", ["Austin"], ["Investor", "Operator"], 0, 2, 3, "Ran sales at two dev-tools startups."),
    ("Rosa Villanueva", 2, "monthly", ["Miami"], ["Investor"], 0, 2, 2, "Lanternfish Ventures, seed."),
    ("Hugo Brandt", 3, "quarterly", ["Chicago"], ["Investor"], 0, 1, 2, "Family office. Slow but writes big checks."),
    ("Sana Qureshi", 1, "daily", ["SF"], ["Engineer"], 1, 3, 3, "College roommate."),
    ("Ellis Navarro", 1, "weekly", ["SF", "LA"], ["Frontier lab", "Researcher"], 1, 3, 3, "Research scientist on a pretraining team."),
    ("Mira Castellanos", 2, "weekly", ["NYC"], ["Operator"], 0, 2, 3, "Head of product, health-tech."),
    ("Fergus Abernathy", 3, "monthly", ["Boston"], ["Student"], 0, 3, 2, "Undergrad building a compiler startup."),
    ("Grace Whitfield", 2, "monthly", ["DC"], ["Operator"], 0, 2, 2, "Policy lead, climate nonprofit."),
    ("Theo Nakashima", 3, "quarterly", ["Seattle"], ["Engineer"], 0, 2, 2, ""),
    ("Imogen Fairclough", 3, "quarterly", ["LA"], ["Founder"], 0, 1, 2, "Consumer social app, pre-seed."),
    ("Ramiro Duarte", 2, "weekly", ["Austin"], ["Founder", "Engineer"], 1, 3, 3, "Building Tidewater Labs."),
    ("Nadia Petrova", 3, "yearly", ["NYC"], ["Retired"], 0, None, 3, "Former mentor. Birthday card every year."),
    ("Owen McAllister", 4, "yearly", ["Chicago"], ["Engineer"], 0, None, None, ""),
    ("Yara Haddad", 2, "monthly", ["SF"], ["Frontier lab"], 0, 3, 2, "Interpretability researcher."),
    ("Caleb Ashworth", 3, "quarterly", ["Las Vegas"], ["Operator"], 0, 1, 1, ""),
    ("Ingrid Solberg", 3, "monthly", ["Seattle"], ["Researcher"], 0, 2, 2, "Robotics lab postdoc."),
    ("Mateo Ferreira", 4, "yearly", ["Miami"], ["Student"], 0, None, None, ""),
    ("Zoe Kittredge", 2, "weekly", ["SF"], ["Founder"], 1, 3, 2, "Briefcase AI cofounder."),
    ("Arjun Mehta", 3, "monthly", ["NYC", "SF"], ["Investor"], 0, 2, 2, "Associate at a growth fund."),
    ("Beatrix Holloway", 4, "quarterly", ["Boston"], ["Researcher"], 0, None, 1, ""),
    ("Silas Mercer", 3, "quarterly", ["Austin"], ["Founder"], 0, 1, 2, "Shut down last startup; exploring."),
    ("Leila Farahani", 2, "weekly", ["LA"], ["Operator"], 0, 2, 3, "Growth lead at a consumer app."),
    ("Ronan Gallagher", 4, None, ["DC"], [], 0, None, None, ""),
    ("Esme Thornbury", 3, "monthly", ["SF"], ["Engineer"], 0, 2, 2, ""),
    ("Kofi Asante", 2, "monthly", ["NYC"], ["Founder"], 1, 3, 3, "Quillstack CEO."),
    ("Wren Ellery", 4, None, [], ["Student"], 0, None, None, ""),
    ("Dario Bellucci", 3, "quarterly", ["Chicago"], ["Investor"], 0, 1, 2, "LP in a few emerging managers."),
    ("Hana Kobayashi", 2, "weekly", ["Seattle"], ["Engineer", "Founder"], 0, 3, 2, "Forgepoint early engineer."),
    ("Vaughn Lemaire", 4, "yearly", ["Miami"], ["Retired"], 0, None, 2, "Uncle's business partner."),
    ("Juniper Oakes", 3, None, ["LA"], ["Operator"], 0, None, None, ""),
    ("Elif Aydin", 3, "monthly", ["Boston"], ["Frontier lab"], 0, 2, 2, ""),
    ("Gideon Pryce", None, None, [], [], 0, None, None, ""),
    ("Paloma Ruiz", None, "quarterly", ["Austin"], ["Founder"], 0, 2, None, "Met at a hackathon."),
    ("Anders Kjeldsen", None, None, ["NYC"], ["Investor"], 0, None, None, ""),
    ("Thalia Brooks", None, "yearly", [], [], 0, None, None, ""),
    # archived
    ("Cormac Byrne", 4, None, ["Boston"], ["Engineer"], 0, None, None, "Lost touch."),
    ("Delphine Moreau", None, None, [], [], 0, None, None, ""),
    ("Quentin Ashby", 4, "yearly", ["Chicago"], [], 0, None, None, ""),
]
ARCHIVED = {"Cormac Byrne", "Delphine Moreau", "Quentin Ashby"}
# Service providers: (name, role)
SERVICES = [
    ("Bianca (Realtor)", "Real estate agent"), ("Swift Movers - Dan", "Mover"), ("Lucia Cleaning", "Cleaner"),
    ("Dr. Patel's Office", "Dentist"), ("Coach Ramon", "Trainer"), ("Keystone Tax - Joyce", "Accountant"),
]
# Unnamed handles (as if auto-imported from Messages with no Contacts match)
UNNAMED = ["+1 (415) 555-0190", "+1 (415) 555-0191"]

# Average days between conversations per cadence (drives the fake Messages history).
CADENCE_GAP = {"daily": 1.5, "weekly": 6, "monthly": 22, "quarterly": 70, "yearly": 200, None: 400}
SNIPPETS = [
    "Coffee next week?", "Just sent the deck", "Congrats on the round!!", "Ha yes", "On my way",
    "Can you intro me to them?", "Running 5 late", "That demo was wild", "Happy birthday!",
    "Want to grab dinner Thursday?", "Thanks for the intro", "Let's catch up soon", "Sounds good",
    "Did you see the launch?", "Call me when free", "Back in town next week", "Love it",
]


def phone_for(i: int) -> str:
    return f"+1 (415) 555-01{i:02d}"


def email_for(name: str) -> str:
    return ".".join(name.lower().replace("'", "").split()[:2]) + "@example.com"


def apple_ns(dt: datetime) -> int:
    return int((dt - APPLE_EPOCH).total_seconds() * 1_000_000_000)


def fresh(path: Path) -> sqlite3.Connection:
    for ext in ("", "-wal", "-shm"):
        Path(str(path) + ext).unlink(missing_ok=True)
    return sqlite3.connect(path)


def seed_crm() -> dict[str, int]:
    crm.init_db()
    crm.init_portfolio_db()
    ids: dict[str, int] = {}
    with sqlite3.connect(crm.DB_PATH) as c:
        for i, (name, tier, cadence, locs, occs, star, traj, chem, notes) in enumerate(PEOPLE):
            bday = f"{rng.randint(1965, 2003)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}" if rng.random() < 0.6 else None
            # A few people get a manual cadence override that disagrees with the computed one.
            override = rng.choice(["weekly", "monthly"]) if i % 9 == 4 else None
            cur = c.execute(
                """INSERT INTO contacts (name, phone, email, birthday, notes, tier, location, occupations,
                       is_service, archived, starred, trajectory, chemistry, cadence_override,
                       cadence_override_at, cadence_computed_at_override)
                   VALUES (?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?)""",
                (name, phone_for(i), email_for(name) if i % 3 else None, bday, notes or None, tier,
                 json.dumps(locs) if locs else None, json.dumps(occs) if occs else None,
                 1 if name in ARCHIVED else 0, star, traj, chem, override,
                 NOW.isoformat() if override else None, cadence if override else None),
            )
            ids[name] = cur.lastrowid
        for j, (name, role) in enumerate(SERVICES):
            cur = c.execute(
                "INSERT INTO contacts (name, phone, role, is_service, location) VALUES (?,?,?,1,?)",
                (name, phone_for(60 + j), role, json.dumps(["SF"])),
            )
            ids[name] = cur.lastrowid
        for h in UNNAMED:
            ids[h] = c.execute("INSERT INTO contacts (name, phone) VALUES (?,?)", (h, h)).lastrowid

        # ---- investor profiles ----
        investors = [
            ("Avery Lindqvist", {"investor_types": ["vc"], "firm": "Bluebird Capital", "closeness": "close_friend", "share_bar": "good_or_better", "active": "very_active", "check_min": 500000, "check_max": 3000000, "stages": ["seed", "series_a"], "sectors": ["robotics", "AI infra"], "knows_well": "hardware supply chain"}),
            ("Jonah Okafor", {"investor_types": ["vc", "angel"], "firm": "Ironwood Partners", "closeness": "friend", "share_bar": "anything_relevant", "active": "active", "check_min": 25000, "check_max": 1000000, "stages": ["pre_seed", "seed"], "sectors": ["fintech", "dev tools"]}),
            ("Marco Lindgren", {"investor_types": ["angel"], "closeness": "friendly", "share_bar": "good_or_better", "active": "occasional", "check_min": 10000, "check_max": 25000, "stages": ["pre_seed"], "sectors": ["consumer", "creator tools"]}),
            ("Keiko Marchetti", {"investor_types": ["angel"], "closeness": "friend", "share_bar": "only_best", "active": "active", "check_min": 50000, "check_max": 100000, "stages": ["seed", "series_a"], "sectors": ["dev tools", "AI infra"], "knows_well": "developer tools"}),
            ("Dmitri Halvorsen", {"investor_types": ["angel"], "closeness": "friend", "active": "active", "check_min": 5000, "check_max": 25000, "sectors": ["dev tools", "b2b saas"], "investor_notes": "Very hands-on with early sales hiring."}),
            ("Rosa Villanueva", {"investor_types": ["vc"], "firm": "Lanternfish Ventures", "closeness": "friendly", "active": "very_active", "check_min": 250000, "check_max": 1500000, "stages": ["seed"], "sectors": ["climate", "fintech"]}),
            ("Hugo Brandt", {"investor_types": ["angel"], "firm": "Brandt Family Office", "closeness": "acquaintance", "share_bar": "only_best", "active": "unknown", "check_min": 250000, "stages": ["series_a", "later"]}),
            ("Arjun Mehta", {"investor_types": ["vc"], "firm": "Tallgrass Growth", "closeness": "acquaintance", "active": "occasional", "stages": ["later"], "sectors": ["AI infra"]}),
            ("Dario Bellucci", {"investor_types": ["angel"], "closeness": "friendly", "share_bar": "anything_relevant", "check_min": 10000, "check_max": 50000}),
            ("Anders Kjeldsen", {"investor_types": ["vc"], "firm": "Northlight Capital", "stages": ["series_a"], "sectors": ["health"]}),
        ]
        for name, fields in investors:
            cols, vals = crm._apply_fields(crm.INVESTOR_FIELDS, fields)
            c.execute(f"INSERT INTO investor_profiles (contact_id, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
                      [ids[name]] + vals)

        # ---- deals: every status, founders + who it was shared with ----
        deals = [
            ({"company_name": "Lumen Robotics", "status": "invested", "conviction": "A", "my_check": 10000, "invested_on": "2024-03-18", "stage": "seed", "sectors": ["robotics"], "round_size": 4000000, "round_terms": "$40M post SAFE", "one_liner": "Foundation models for robot arms", "asks": ["my_investment", "angel_intros"], "my_take": "Best team I've seen in the space."}, ["Priya Ramaswamy"], ["Avery Lindqvist", "Keiko Marchetti"]),
            ({"company_name": "Shelfwise", "status": "evaluating", "conviction": "B", "stage": "seed", "sectors": ["robotics", "logistics"], "round_size": 3000000, "one_liner": "Warehouse robots with remote teleop fallback", "asks": ["my_investment"], "deck_url": "https://example.com/shelfwise-deck"}, ["Tobias Wren"], []),
            ({"company_name": "Ledgerline", "status": "committed", "conviction": "A", "my_check": 15000, "stage": "pre_seed", "sectors": ["fintech"], "round_size": 1500000, "one_liner": "Reconciliation API for marketplaces", "asks": ["my_investment", "vc_intros"], "forwardable_blurb": "Lena (2nd-time founder, prior exit) is raising $1.5M for Ledgerline, a reconciliation API for marketplaces. 6 paying design partners."}, ["Lena Morozova"], ["Jonah Okafor", "Rosa Villanueva"]),
            ({"company_name": "Quillstack", "status": "advising", "conviction": "B", "stage": "series_a", "sectors": ["dev tools"], "one_liner": "Docs that write themselves from your codebase", "asks": ["advice", "vc_intros"]}, ["Kofi Asante"], ["Arjun Mehta"]),
            ({"company_name": "Loomkit", "status": "watching", "conviction": "unrated", "stage": "pre_seed", "sectors": ["consumer"], "one_liner": "Group trip planning for friends", "asks": ["angel_intros"]}, ["Imogen Fairclough"], ["Marco Lindgren"]),
            ({"company_name": "Gridlight", "status": "passed", "conviction": "C", "stage": "seed", "sectors": ["climate"], "one_liner": "Home battery leasing", "notes": "Capital intensive; passed politely."}, ["Silas Mercer"], []),
            ({"company_name": None, "status": "watching", "stage": "pre_seed", "sectors": ["AI infra"], "one_liner": "Exploring eval tooling after leaving the lab", "asks": ["advice"]}, ["Yara Haddad"], []),
            ({"company_name": "Tidewater Labs", "status": "committed", "conviction": "A", "my_check": 25000, "stage": "seed", "sectors": ["climate", "AI infra"], "round_size": 5000000, "asks": ["my_investment", "angel_intros"]}, ["Ramiro Duarte"], ["Dmitri Halvorsen", "Dario Bellucci", "Jonah Okafor"]),
        ]
        for fields, founders, shared in deals:
            cols, vals = crm._apply_fields(crm.DEAL_FIELDS, fields)
            deal_id = c.execute(f"INSERT INTO deals ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals).lastrowid
            for n in founders:
                c.execute("INSERT INTO deal_contacts (deal_id, contact_id, role) VALUES (?,?, 'founder')", (deal_id, ids[n]))
            for n in shared:
                c.execute("INSERT INTO deal_contacts (deal_id, contact_id, role) VALUES (?,?, 'shared')", (deal_id, ids[n]))
    return ids


def seed_portfolio() -> None:
    I = lambda **kw: kw  # noqa: E731
    investments = [
        I(company="Lumen Robotics", legal_name="Lumen Robotics, Inc.", kind="direct", status="active", source="Warm intro", invested_on="2024-03-18", amount=10000, vehicle="SAFE", entry_stage="Seed", entry_valuation="$40M", current_stage="Series B", current_valuation="$600M", current_value=104000, valuation_as_of="2026-05", valuation_source="https://example.com/lumen-series-b", why="Best robotics research team I've met.", learnings="Back technical founders early in hard categories."),
        I(company="Quillstack", kind="direct", status="active", source="VC co-invest", invested_on="2024-04-23", amount=5000, vehicle="SPV", entry_stage="Series A", entry_valuation="$90M", current_stage="Series C", current_valuation="$1.2B", current_value=46000, valuation_as_of="2025-09", why="Usage growing weekly with no sales team."),
        I(company="Briefcase AI", kind="direct", status="active", source="Syndicate / SPV", invested_on="2024-07-23", amount=10000, vehicle="SPV", entry_stage="Series A", entry_valuation="$150M", current_stage="Series C", current_valuation="$900M", current_value=44000, valuation_as_of="2026-09", why="Clear wedge into law firms."),
        I(company="Briefcase AI", kind="direct", status="active", source="Secondary platform", invested_on="2025-06-02", amount=7500, vehicle="Secondary", entry_stage="Secondary", entry_valuation="$600M", current_stage="Series C", current_valuation="$900M", current_value=10500, why="Topped up on a secondary."),
        I(company="Forgepoint", kind="direct", status="active", source="Secondary platform", invested_on="2026-02-10", amount=15000, vehicle="Secondary", entry_stage="Secondary", entry_valuation="$2.9B", current_stage="Series D", current_valuation="$5B", current_value=25600, bucket="late", why="Default dev tool for every engineer I know."),
        I(company="Ledgerline", kind="direct", status="pending", source="Warm intro", amount=15000, entry_stage="Pre-seed", entry_valuation="$12M", why="Second-time founder; wiring this month."),
        I(company="Tidewater Labs", kind="direct", status="pending", source="Direct", amount=25000, entry_stage="Seed", entry_valuation="$60M", why="Climate x AI with real revenue."),
        I(company="Halo Wearables", kind="direct", status="exited", source="Event", invested_on="2021-03-08", amount=10000, vehicle="SAFE", entry_stage="Series A", entry_valuation="$85M", current_stage="Acquired", current_valuation="$12M", returned=1400, why="New device category.", learnings="Hardware + no distribution is brutal."),
        I(company="Pebblepay", kind="direct", status="exited", source="Inbound", invested_on="2020-09-14", amount=5000, vehicle="SAFE", entry_stage="Pre-seed", entry_valuation="$8M", current_stage="Acquired", current_valuation="$140M", returned=61000, why="Payments for small clinics.", gained="Acquired by a larger fintech."),
        I(company="Snackbox", kind="direct", status="written_off", source="Event", invested_on="2021-11-01", amount=10000, vehicle="SAFE", entry_stage="Seed", entry_valuation="$20M", current_value=0, why="Strong consumer brand early.", learnings="Shut down: CAC never came down."),
        I(company="Driftwood AR", kind="direct", status="written_off", source="Direct", invested_on="2022-05-20", amount=5000, vehicle="SAFE", entry_stage="Pre-seed", entry_valuation="$10M", current_value=0, why="AR glasses for field techs.", learnings="Shut down after failing to raise a Series A."),
        I(company="Meadowlark Health", kind="direct", status="active", source="Warm intro", invested_on="2023-08-30", amount=10000, vehicle="SAFE", entry_stage="Seed", entry_valuation="$25M", current_stage="Series A", current_valuation="$110M", current_value=34000, valuation_as_of="2025-12", why="Home care scheduling; founders ex-nurses."),
        I(company="Copperline", kind="direct", status="active", source="VC co-invest", invested_on="2025-01-15", amount=10000, vehicle="SPV", entry_stage="Seed", entry_valuation="$30M", current_stage="Seed", current_valuation="$30M", current_value=10000, why="Grid interconnection software."),
        I(company="Orbitly", kind="direct", status="active", source="Inbound", invested_on="2022-10-04", amount=5000, vehicle="SAFE", entry_stage="Pre-seed", entry_valuation="$9M", current_stage="Seed", current_valuation="$18M", current_value=7500, why="Satellite data marketplace."),
        I(company="Kestrel Fund II", kind="fund", status="active", source="Direct", invested_on="2020-11-03", amount=25000, vehicle="LP", vintage="2020", fund_size="$35M", called=25000, current_value=44000, returned=8000, why="High-volume pre-seed access."),
        I(company="Saltmarsh Ventures III", kind="fund", status="active", source="Inbound", invested_on="2025-04-15", amount=25000, vehicle="LP", vintage="2025", fund_size="$60M", called=10000, current_value=11500, why="First-check specialist."),
        I(company="Bluebird Capital Fund I", kind="fund", status="active", source="Warm intro", invested_on="2023-02-01", amount=50000, vehicle="LP", vintage="2023", fund_size="$120M", called=30000, current_value=36000, why="Robotics-focused seed fund; I know the GPs well."),
        I(company="Lanternfish Opportunity Fund", kind="fund", status="pending", source="Direct", amount=25000, vehicle="LP", vintage="2026", fund_size="$40M", bucket="early", why="Emerging manager, climate seed."),
    ]
    pipeline = [
        dict(name="Yara Haddad", list="tracking", note="Leaving the lab; eval tooling idea", timing="3 mo+", potential="High"),
        dict(name="Fergus Abernathy", list="tracking", note="Undergrad compiler project", timing="1 yr+", potential="Medium"),
        dict(name="Silas Mercer", list="tracking", note="Exploring after shutdown", timing="Now", potential="Medium"),
        dict(name="Ellis Navarro", list="tracking", note="Might spin out of the lab", timing="Someday", potential="High"),
        dict(name="Hana Kobayashi", list="tracking", timing="2 yr+", potential="Low"),
        dict(name="Ledgerline", list="investing", note="SAFE signed, wire pending", amount=15000, bucket="early"),
        dict(name="Glacier Compute", list="missed", note="Round filled before I could get allocation"),
        dict(name="Gridlight", list="passed", note="Capital intensive"),
        dict(name="Pocketbot", list="passed", note="Hardware risk, unclear retention"),
    ]
    evaluating = [
        dict(company="Shelfwise", round="Seed", valuation="$30M", check_size=10000, deadline=(NOW + timedelta(days=9)).date().isoformat(), source="Warm intro", next_step="Reference call with ex-manager", note="Strong team; unclear go-to-market."),
        dict(company="Northwind Robotics", round="Seed", valuation="$40M", check_size=10000, deadline=(NOW + timedelta(days=2)).date().isoformat(), source="VC co-invest", next_step="Second meeting", note="Good demo."),
        dict(company="Loomkit", round="Pre-seed", valuation="$8M", check_size=5000, source="Event", note="Consumer; waiting on retention data."),
    ]
    forward_to = [
        dict(name="Bluebird Capital", kind="vc", note="robotics, seed"), dict(name="Ironwood Partners", kind="vc"),
        dict(name="Lanternfish Ventures", kind="vc", note="climate"), dict(name="Keiko Marchetti", kind="angel", note="dev tools"),
        dict(name="Dmitri Halvorsen", kind="angel"), dict(name="Marco Lindgren", kind="angel", note="consumer"),
    ]
    with sqlite3.connect(crm.DB_PATH) as c:
        for table, rows in (("investments", investments), ("pipeline", pipeline), ("evaluating", evaluating), ("forward_to", forward_to)):
            for r in rows:
                cols, vals = crm._portfolio_values(table, r)
                c.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        rules = dict(crm.PORTFOLIO_DEFAULT_RULES, annual_budget=120000)
        c.execute("INSERT OR REPLACE INTO portfolio_settings (key, value) VALUES ('rules', ?)", (json.dumps(rules),))


def seed_apple_dbs() -> None:
    """Fake Messages + call history in the (subset of the) macOS schemas app.py reads."""
    m = fresh(APP_DIR / "demo_messages.db")
    m.executescript("""
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, style INTEGER);
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, date INTEGER, text TEXT, is_from_me INTEGER,
                              handle_id INTEGER, attributedBody BLOB);
    """)
    k = fresh(APP_DIR / "demo_calls.db")
    k.execute("CREATE TABLE ZCALLRECORD (Z_PK INTEGER PRIMARY KEY, ZADDRESS TEXT, ZDATE REAL, ZDURATION REAL)")

    group_chat = m.execute("INSERT INTO chat (style) VALUES (43)").lastrowid
    talkers = [(i, p) for i, p in enumerate(PEOPLE) if p[0] not in ARCHIVED or rng.random() < 0.5]
    for i, (name, tier, cadence, *_rest) in talkers:
        handle = m.execute("INSERT INTO handle (id) VALUES (?)", (phone_for(i),)).lastrowid
        chat = m.execute("INSERT INTO chat (style) VALUES (45)").lastrowid
        gap = CADENCE_GAP[cadence]
        # Archived / low-tier people went quiet a while ago.
        t = NOW - timedelta(days=rng.uniform(1, 3) * gap + (200 if name in ARCHIVED else 0))
        start = NOW - timedelta(days=420)
        while t > start:
            for _ in range(rng.randint(1, 6)):  # one conversation = a few messages
                ts = t + timedelta(minutes=rng.randint(0, 90))
                mid = m.execute(
                    "INSERT INTO message (date, text, is_from_me, handle_id) VALUES (?,?,?,?)",
                    (apple_ns(ts), rng.choice(SNIPPETS), rng.random() < 0.5, handle),
                ).lastrowid
                m.execute("INSERT INTO chat_message_join VALUES (?,?)", (chat, mid))
                if tier in (1, 2) and rng.random() < 0.3:  # group-chat traffic counts toward volume
                    gid = m.execute("INSERT INTO message (date, text, is_from_me, handle_id) VALUES (?,?,0,?)",
                                    (apple_ns(ts), "lol", handle)).lastrowid
                    m.execute("INSERT INTO chat_message_join VALUES (?,?)", (group_chat, gid))
            if tier in (1, 2) and rng.random() < 0.15:
                k.execute("INSERT INTO ZCALLRECORD (ZADDRESS, ZDATE, ZDURATION) VALUES (?,?,?)",
                          (phone_for(i), (t - APPLE_EPOCH).total_seconds(), rng.randint(60, 3600)))
            t -= timedelta(days=rng.expovariate(1 / gap))
    # Automated sender (shortcode) and the unnamed handles, so cleanup has something to do.
    for h, from_me in (("72975", False), (UNNAMED[0], True), (UNNAMED[1], False)):
        hid = m.execute("INSERT INTO handle (id) VALUES (?)", (h,)).lastrowid
        chat = m.execute("INSERT INTO chat (style) VALUES (45)").lastrowid
        for d in range(0, 60, 7):
            mid = m.execute("INSERT INTO message (date, text, is_from_me, handle_id) VALUES (?,?,?,?)",
                            (apple_ns(NOW - timedelta(days=d)), "Your code is 123456", from_me, hid)).lastrowid
            m.execute("INSERT INTO chat_message_join VALUES (?,?)", (chat, mid))
    m.commit(); m.close()
    k.commit(); k.close()


def main() -> None:
    db = Path(crm.DB_PATH).resolve()
    if db.name == "crm.db" or not crm.DEMO_MODE:
        sys.exit(f"Refusing to seed {db}: seed_demo.py only writes demo databases.")
    fresh(db).close()
    seed_crm()
    seed_portfolio()
    seed_apple_dbs()
    with sqlite3.connect(db) as c:
        counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in
                  ("contacts", "investor_profiles", "deals", "deal_contacts", "investments", "pipeline", "evaluating", "forward_to")}
    print(f"Wrote {db.name}, demo_messages.db, demo_calls.db: {counts}")
    print("Run it:  CRM_DEMO=1 PORT=5556 ./venv/bin/python app.py   ->  http://127.0.0.1:5556")


if __name__ == "__main__":
    main()
