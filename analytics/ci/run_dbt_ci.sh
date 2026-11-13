#!/usr/bin/env bash
# Build a throwaway copy of the database, run dbt on it twice with a change
# in between, and check that incremental loads, deletes and SCD2 history
# behave. Used by GitHub Actions; runs locally too.
#
# Needs: psql, dbt; PGHOST/PGPORT/PGUSER/PGPASSWORD for an admin connection
# to an empty database named in PGDATABASE.
set -euo pipefail
cd "$(dirname "$0")/../.."          # repo root

PSQL="psql -v ON_ERROR_STOP=1 -q"
DBT_PASSWORD="ci-only-$(date +%s)"

echo "== schema: baseline + every migration"
$PSQL -f supabase/baseline.sql
for f in supabase/migrations/*.sql; do
  echo "   $f"
  $PSQL -f "$f"
done
$PSQL -c "alter role dbt_runner with login password '$DBT_PASSWORD'"

echo "== bronze sample data"
$PSQL -f analytics/ci/sample_bronze.sql

export DBT_TARGET=ci DBT_HOST="$PGHOST" DBT_PORT="${PGPORT:-5432}" DBT_DBNAME="$PGDATABASE" \
       DBT_USER=dbt_runner DBT_PASSWORD DBT_PROFILES_DIR="$PWD/analytics"
cd analytics

echo "== dbt build #1 (full load)"
dbt build

echo "== change bronze: resolve one ticket, delete one, add one"
$PSQL -c "update public.tickets set status = 'resolved', resolved_at = now() where subject = 'Payslip wrong'"
$PSQL -c "delete from public.tickets where subject = 'Leave policy'"
$PSQL -c "insert into public.tickets (submitter, email, subject, description, department, priority, confidence, status)
          values ('Test User 7', 'user7@example.com', 'New starter', 'x', 'HR', 'High', 0.8, 'pending_review')"

echo "== dbt build #2 (incremental)"
dbt build

echo "== dbt build #3 (nothing changed: must still pass)"
dbt build

echo "== checks"
check() {  # description, SQL returning true
  if [ "$($PSQL -Atc "$2")" = "t" ]; then echo "   ok   $1"; else echo "   FAIL $1"; exit 1; fi
}
check "gold matches bronze"            "select (select count(*) from gold.fct_tickets) = (select count(*) from public.tickets)"
check "deleted ticket left gold"       "select not exists (select 1 from gold.fct_tickets f join snapshots.snap_tickets s using (ticket_id) where s.dbt_valid_to is not null and not exists (select 1 from public.tickets t where t.id = f.ticket_id))"
check "resolved ticket has 2 versions" "select count(*) = 2 from snapshots.snap_tickets s join public.tickets t on t.id = s.ticket_id where t.subject = 'Payslip wrong'"
check "status change recorded"         "select exists (select 1 from gold.fct_ticket_status_changes c join public.tickets t on t.id = c.ticket_id where t.subject = 'Payslip wrong' and c.from_status = 'pending_review' and c.to_status = 'resolved')"
check "deleted ticket's history closed" "select count(*) = 1 and bool_and(dbt_valid_to is not null) from snapshots.snap_tickets s where not exists (select 1 from public.tickets t where t.id = s.ticket_id)"
check "3 runs logged, all success"      "select count(*) = 3 and bool_and(status = 'success') from public.pipeline_runs where pipeline = 'dbt'"
echo "== all good"
