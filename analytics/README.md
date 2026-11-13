# Solace analytics (dbt)

The data layer under Solace: the app writes tickets; dbt turns them into a
clean, tested star schema every hour.

```
bronze (public.*)            silver (views)          gold (tables)                    snapshots
tickets ─────────────────▶ stg_tickets ─────────▶ fct_tickets (incremental) ◀── dim_department
conversations ───────────▶ stg_conversations        agg_tickets_daily           dim_priority
feedback ────────────────▶ stg_feedback             fct_ticket_status_changes ◀ dim_status / dim_date
                              │                              ▲
                              └──────────▶ snap_tickets (SCD Type 2) ──┘
```

| Layer | What it holds | How it's built |
|---|---|---|
| Bronze | The app's tables, exactly as written. Includes names, emails, ticket text. | The app |
| Silver | One clean row per ticket / answer / rating. Types fixed, values standardised (`'high'` → `High`, 0–100 confidence → 0–1), **no personal data**. | Views |
| Gold | Star schema: `fct_tickets` + four dimensions; daily aggregate with running total and 7-day rolling average; status-change fact. | Tables; `fct_tickets` is incremental |
| Snapshots | `snap_tickets`: every version of every ticket, with `dbt_valid_from` / `dbt_valid_to`. | dbt snapshot, timestamp strategy |

## Design decisions

- **Incremental load with a watermark.** `fct_tickets` loads only tickets whose
  `updated_at` is newer than the latest already loaded, minus a 1-hour look-back
  for late commits. `delete+insert` on `ticket_id` makes re-processing safe, so
  a re-run never duplicates. A trigger on `public.tickets` keeps `updated_at` honest.
- **Hard deletes.** An incremental load can't see deleted rows, so a post-hook
  removes tickets that no longer exist in bronze; the snapshot closes their
  history (`hard_deletes: invalidate`).
- **Stable keys.** Dimension keys are `md5(natural key)`, so rebuilding a
  dimension never breaks the incremental fact's foreign keys.
- **Gap-free dates.** The daily aggregate is built on a full date × department
  grid. Without it, a "7-day" rolling window over days with no tickets would
  cover more than 7 days.
- **Least privilege.** dbt connects as `dbt_runner`: it can read three bronze
  tables, write only `silver`, `gold`, `snapshots`, and insert its own rows
  into the run log. It cannot read users or documents, or change bronze.
- **PII stays in bronze.** Silver, gold and the snapshot carry no names,
  emails or ticket text.

## Tests (run on every build)

- Generic: `unique`, `not_null`, `accepted_values`, `relationships` (every
  fact key exists in its dimension), and a custom `between` test.
- `assert_gold_reconciles_with_bronze` – gold has exactly bronze's tickets.
- `assert_gold_is_current` – the incremental load missed no change.
- `assert_daily_agg_matches_fact` – the aggregate adds up to the fact table.
- `assert_snapshot_one_current_version` – SCD2 windows don't overlap.
- `assert_bronze_visible_to_dbt` – catches row-level security hiding bronze
  from dbt. Without it, every model "succeeds" with zero rows and the
  reconciliation test passes too. (This happened while building it.)
- `warn_resolved_before_created` – a data-quality warning that doesn't block the load.

Every build writes one row to `public.pipeline_runs` (an `on-run-end` hook),
pass or fail. The app shows it at **/admin/pipeline**.

## Where it runs

- **CI** (`.github/workflows/ci.yml`, every push / PR): a throwaway Postgres;
  applies `supabase/baseline.sql` and every migration, loads sample data,
  runs `dbt build`, changes data, runs again, and checks incremental loads,
  deletes and history (`ci/run_dbt_ci.sh`).
- **Production** (`.github/workflows/dbt-prod.yml`): hourly against Supabase.
  A failure fails the workflow and GitHub emails the owner.

## One-time production setup

1. Run `supabase/migrations/20261114100000_data_platform.sql` in the Supabase SQL editor.
2. Give dbt a password (choose your own; don't paste it anywhere else):
   ```sql
   alter role dbt_runner with login password 'choose-a-long-random-password';
   ```
3. GitHub → repository → Settings → Secrets and variables → Actions → add:
   - `DBT_HOST` – Supabase → Connect → **Session pooler** host (e.g. `aws-…-ap-northeast-2.pooler.supabase.com`)
   - `DBT_PORT` – `5432`
   - `DBT_USER` – `dbt_runner.txhqmizlpmzozmcslevh`
   - `DBT_PASSWORD` – the password from step 2
4. Actions → **dbt production build** → Run workflow.

The session pooler is used because GitHub's runners have no IPv6 and
Supabase's direct connection is IPv6-only.

## Run locally

```bash
pip install -r analytics/requirements.txt
export DBT_HOST=... DBT_PORT=5432 DBT_USER=dbt_runner... DBT_PASSWORD=... DBT_PROFILES_DIR=analytics
cd analytics && dbt build
```
