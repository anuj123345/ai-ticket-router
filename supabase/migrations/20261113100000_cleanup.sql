-- Cleanup after the Data Studio and Smart Search rebuild.

-- 1. Old spreadsheet rows. Both files (lego_sets.csv, Sample data.xlsx) were
--    converted to datasets / dataset_records and checked against these rows
--    (Sample data: 700 rows, Sales 118,726,350.26; lego: 18,457 rows,
--    3,291,343 pieces). No code or database function reads this table now.
drop table if exists public.dataset_rows;

-- 2. Ticket #5: saved blank during the model outage on 2026-10-15
--    (General / Medium / 50%, empty draft). It skews the Ops stats.
delete from public.tickets
 where id = 5 and department = 'General' and coalesce(draft_response, '') = '';

-- 3. Indexes for the feedback table's foreign keys (performance advisor).
create index if not exists feedback_conversation_id_idx on public.feedback (conversation_id);
create index if not exists feedback_document_id_idx     on public.feedback (document_id);
