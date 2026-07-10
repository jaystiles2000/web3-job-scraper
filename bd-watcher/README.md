# BD Trigger Watcher

Watches a fixed list of Solana-ecosystem companies for new job postings
and pings Telegram the same day. **BD signal**, not candidate sourcing —
a company posting a role is a company with hiring budget and an open door.

## What lives in this folder

| File | Purpose |
|---|---|
| `companies.yaml` | Watchlist. One entry per company. Editable by hand. |
| `config.yaml` | Tunables: cooldown, keywords, angles, cron cadence hints. |
| `state.json` | Dedupe + cooldown state. Auto-committed by CI. Don't edit. |
| `discover.py` | ATS discovery script. Run via workflow. |
| `watch.py` | Main scheduled runner. |
| `core.py` | Shared helpers (ATS clients, classifier, Telegram send). |

## How it runs

- **GitHub Actions cron**: twice daily, 06:30 and 15:30 UTC (~07:30 and 16:30 UK).
- **workflow_dispatch**: manual trigger, with a `mode` input:
  - `watch` (default) — same as the cron does.
  - `discover` — probes ATS platforms (Greenhouse / Lever / Ashby) for
    any company with `ats: null`. Writes results back to `companies.yaml`.

## Adding a company

Append an entry to `companies.yaml`:

```yaml
- name: New Solana Co
  ats: null            # discovery will fill in
  ats_id: null
  aliases: []          # optional alternative names for fallback matching
  priority: normal     # or 'high' for weekend alerts
```

Then run **Actions → BD Trigger Watcher → Run workflow → mode: discover**.
The next scheduled run will start tracking it.

## Removing a company

Delete its entry from `companies.yaml`. State for that company will
eventually roll over (seen job ids stay until a manual cleanup, but that's
harmless — they just occupy a few bytes each).

## Priority

- `normal` (default): alerts run Mon-Fri only. Weekend hits get folded
  into the digest.
- `high`: alerts run any day of week. Use for top-tier targets.

## Cooldown

Max one instant alert per company per `cooldown_days` (default 7). If a
company drops 4 roles in the same window, you get one hero alert with
"+3 more open at this company" and the rest go into the daily digest.

## Lane classification

Every new role is classified into one of five lanes based on title
keywords in `config.yaml`:

- `engineering` — Rust, Solana, protocol, SVM, Anchor, blockchain eng
- `legal_compliance_finance` — legal, MLRO, AML, finance, accountant
- `bd_sales` — business development, sales, partnerships, growth
- `ops` — operations, chief of staff, people, HR, talent
- `other` — everything else

The first four go to **instant** Telegram alerts. `other` gets bundled
into the daily digest.

## Testing without spamming yourself

Set env `BD_WATCHER_DRY_RUN=1` when running `watch.py` locally. It will
parse, dedupe, classify, and log the messages it would send, but never
hit Telegram.

## Secrets required

Reuses the existing repo secrets from the candidate scraper:

- `TELEGRAM_TOKEN` (or `TELEGRAM_BOT_TOKEN`)
- `TELEGRAM_CHAT_ID`

Set both in **Settings → Secrets and variables → Actions**.

## First run

The first scheduled run is treated as a **seed**. It fetches every
current job, marks each as "seen", and sends **zero** alerts. From the
second run onwards, only truly new postings trigger.

## Out of scope

- No LinkedIn scraping (ban risk, and Dripify already covers it).
- No auto-sending outreach — alerts only. You write the messages.
- No funding-announcement watching yet — TODO in `config.yaml`.
