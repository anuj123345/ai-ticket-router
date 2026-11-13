-- Made-up bronze rows for CI and local runs. No real people or tickets.
-- Includes the messy cases silver must clean up.

insert into public.tickets
  (submitter, email, subject, description, department, priority, confidence, status,
   assignee_team, draft_response, created_at, resolved_at)
values
  ('Test User 1', 'user1@example.com', 'VPN down',       'x', 'IT',          'High',     0.9,  'resolved',
   'IT Support', 'draft', now() - interval '20 days', now() - interval '19 days'),
  ('Test User 2', 'user2@example.com', 'Payslip wrong',  'x', 'Finance',     'critical', 85,   ' Pending_Review ',
   'Finance Ops', '', now() - interval '12 days', null),
  ('Test User 3', 'user3@example.com', 'Leave policy',   'x', '',            null,       0.5,  'escalated',
   null, null, now() - interval '8 days', null),
  ('Test User 4', 'user4@example.com', 'Laptop request', 'x', 'IT',          'medium',   0.7,  'resolved',
   'IT Support', 'draft', now() - interval '3 days', now() - interval '2 days'),
  ('Test User 5', 'user5@example.com', 'Build failing',  'x', 'Engineering', 'Low',      1.0,  'pending_review',
   'Platform', 'draft', now() - interval '1 day', null),
  -- Resolved time before created time: silver keeps it, a warning test flags it.
  ('Test User 6', 'user6@example.com', 'Clock skew',     'x', 'HR',          'Low',      0.6,  'resolved',
   'People Team', 'draft', now() - interval '5 hours', now() - interval '6 hours');

insert into public.conversations (question, answer, sources, created_at)
values
  ('q1', 'a1', '[{"source": "Handbook.pdf"}]',                            now() - interval '4 days'),
  ('q2', 'a2', '[{"source": "Sample data.xlsx", "dataset_id": 1}]',       now() - interval '2 days'),
  ('q3', 'a3', null,                                                      now() - interval '1 day');

insert into public.feedback (conversation_id, rating, created_at)
select id, case when id % 2 = 0 then 1 else -1 end, created_at + interval '1 minute'
from public.conversations;
