# Engineering Runbook & Support Knowledge Base

## Deployment Process
All deployments go through CI/CD. Push to main triggers staging deploy, tagging a release triggers production.
Production deploys require: passing CI, code review approval, staging verification.
Deployment windows: Tuesday and Thursday, 10am–2pm PST. No deploys on Fridays.
Hotfixes can bypass the window with on-call engineer approval.

## On-Call Rotation
On-call schedule is managed in PagerDuty. Rotation is weekly, Monday to Monday.
Current on-call: check #oncall-schedule in Slack.
Response SLAs: P0 (15 min), P1 (1 hour), P2 (4 hours), P3 (next business day).
Escalation path: on-call engineer → tech lead → engineering manager → CTO.

## Incident Response
For P0/P1 incidents: page on-call, create incident in PagerDuty, post in #incidents Slack channel.
Incident commander leads response. Communication lead posts updates every 30 min.
Post-incident review (PIR) is required for all P0/P1 incidents within 5 business days.

## Access Requests
GitHub repository access: request via the engineering-access Slack channel with manager approval.
AWS access: submit a ticket with least-privilege justification, approved by Security.
Database access: requires DBA approval and quarterly review. No direct prod DB access for engineers.
Third-party tool access: request in #tools-access with business justification.

## Code Review Policy
All PRs require at least 1 approval (2 for changes to core infrastructure).
PRs should be small and focused. Target <400 lines changed.
Draft PRs are encouraged for early feedback. Do not merge draft PRs.
Security-sensitive changes (auth, payments, PII) require security team review.

## Architecture & Tech Stack
Backend: Python (FastAPI), Node.js (Express) for legacy services.
Frontend: React, TypeScript.
Databases: PostgreSQL (primary), Redis (cache), Elasticsearch (search).
Infrastructure: AWS, Terraform, Kubernetes (EKS).
Monitoring: Datadog, PagerDuty, Sentry.

## Bug Reporting
All bugs should be filed in Jira with: steps to reproduce, expected vs actual behavior, severity.
P0 bugs block release. P1 bugs are fixed in current sprint. P2/P3 go to backlog triage.
Security vulnerabilities: report privately to security@company.com, do not file in public Jira.

## Development Environment Setup
Setup docs: https://eng.company.com/onboarding
Requires: Docker, Node 20+, Python 3.11+, AWS CLI configured.
Common issues: see the #dev-help Slack channel or the troubleshooting wiki.
