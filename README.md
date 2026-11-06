# Personal CRM

A local Flask app (macOS) for keeping up with people and tracking private investments.

- **Contacts** (`/`): tiers, locations, occupations, stars, archive, cadence (computed from
  macOS Messages and call history, with manual overrides), AI insights.
- **Activity calendar** (`/calendar`): how many people you talked to each day.
- **Deals** (`/deals`): deals you're advising/evaluating/invested in, founders, which investors
  you've shared them with, and investor suggestions per deal.
- **Private investments** (`/portfolio`): direct and fund (LP) positions, pipeline, live deals
  being evaluated, who to forward deals to, and a public-market benchmark.

All data lives in a local SQLite file (`crm.db`), which is gitignored along with backups,
price caches, CSV exports and `.env`.

## Screenshots

All taken from the fake demo dataset (see [Demo with fake data](#demo-with-fake-data)).

Contacts, sorted by last contact, with tier, chemistry and cadence badges:
![Contacts list](docs/screenshots/contacts.png)

A contact, with recent messages pulled from Messages:
![Contact detail](docs/screenshots/contact-detail.png)

Deals you're advising, watching, evaluating or committed to:
![Deals](docs/screenshots/deals.png)

Private investments: totals, yearly pacing and a public-market comparison:
![Private investments](docs/screenshots/portfolio.png)

## Setup

```sh
python3 -m venv venv
./venv/bin/pip install flask anthropic certifi
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env   # optional: AI insights + valuation lookup
./run.sh                                      # http://127.0.0.1:5555
```

Reading Messages and call history needs Full Disk Access for the Python binary
(System Settings → Privacy & Security → Full Disk Access).

## Demo with fake data

`seed_demo.py` builds a fully made-up dataset (about 50 contacts, deals in every status,
investor profiles, 18 investments incl. funds, exits and write-offs, pipeline, evaluating,
forward-to list) plus fake Messages and call-history databases. Every name, company, phone
number (555-01xx) and email (example.com) is fictional.

```sh
./venv/bin/python seed_demo.py                     # writes demo.db, demo_messages.db, demo_calls.db
CRM_DEMO=1 PORT=5556 ./venv/bin/python app.py      # http://127.0.0.1:5556
```

With `CRM_DEMO=1` the app reads only those demo files: it never opens `crm.db`, your real
Messages / call history / Contacts, never pushes names to Apple Contacts, and skips backups.
The portfolio page also has a client-side sample mode at `/portfolio?demo=1`.

## Environment

| Variable | Default | |
|---|---|---|
| `CRM_DB` | `crm.db` (`demo.db` in demo mode) | SQLite path |
| `CRM_DEMO` | unset | `1` = demo mode (see above) |
| `PORT` | `5555` | |
| `ANTHROPIC_API_KEY` | unset | AI insights, valuation lookup |
