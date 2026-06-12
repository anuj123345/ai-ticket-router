# Solace — AI-Powered Enterprise Support Platform

> The operating system for modern support teams.

[![Live Demo](https://img.shields.io/badge/Live%20Demo-ai--ticket--router.vercel.app-7367F0?style=flat-square&logo=vercel)](https://ai-ticket-router.vercel.app)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Flask](https://img.shields.io/badge/Flask-3.x-000000?style=flat-square&logo=flask)](https://flask.palletsprojects.com)
[![Supabase](https://img.shields.io/badge/Supabase-PostgreSQL-3ECF8E?style=flat-square&logo=supabase)](https://supabase.com)
[![NVIDIA NIM](https://img.shields.io/badge/NVIDIA-NIM-76B900?style=flat-square&logo=nvidia)](https://build.nvidia.com)
[![Vercel](https://img.shields.io/badge/Deployed%20on-Vercel-000000?style=flat-square&logo=vercel)](https://vercel.com)

---

Solace is a full-stack AI-powered enterprise support platform that automates ticket routing, enables natural-language data querying, and delivers a RAG-based onboarding assistant — all with a human-in-the-loop review layer before anything reaches your team.

Built as a production-grade portfolio project demonstrating end-to-end AI engineering: LLM classification, retrieval-augmented generation, natural language to SQL, and a modern Flask + Supabase backend deployed on Vercel.

---

## What It Does

### 🎫 AI Ticket Router
Employees submit support tickets. The AI instantly:
- Classifies the ticket into the correct department (IT, HR, Finance, Engineering, General)
- Assigns a priority level (Critical / High / Medium / Low) with confidence score and reasoning
- Searches a RAG knowledge base for relevant context
- Drafts a complete, context-aware response

A human reviewer then approves or escalates before anything is sent — keeping AI fast and humans accountable.

### 📊 Ops Analytics Dashboard
Ask questions about your ticket data in plain English:

> *"Which department has the most unresolved tickets this week?"*

The AI writes the SQL, runs it against the live database, auto-generates a chart, and explains the result in plain language. No SQL knowledge required.

### 🎓 Onboarding Assistant
A chat interface grounded in your company's uploaded documents (PDF, DOCX, TXT, MD). New hires ask anything about benefits, policies, or procedures and get cited, accurate answers. Admins can upload docs and verify Q&A pairs to improve quality over time.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11, Flask 3.x |
| AI / LLM | NVIDIA NIM — `meta/llama-3.1-70b-instruct` |
| Embeddings | NVIDIA NIM — `nvidia/nv-embedqa-e5-v5` |
| Database | Supabase (PostgreSQL + pgvector) |
| Frontend | Vanilla HTML/CSS/JS, Bootstrap 5, Chart.js |
| Deployment | Vercel (serverless Python) |
| Auth | Session-based with bcrypt password hashing |
| File Processing | PyPDF2, python-docx |

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Flask App (app.py)                   │
│                                                         │
│  POST /submit   →  classifier.py  →  response_gen.py   │
│  POST /ops/query →  nl_query.py                         │
│  POST /onboarding/chat → onboarding_agent.py            │
└──────────────────────┬──────────────────────────────────┘
                       │
           ┌───────────┴───────────┐
           │                       │
    NVIDIA NIM API           Supabase (PostgreSQL)
    LLM + Embeddings         tickets · users · docs
                             chunks (pgvector)
                             conversations · feedback
```

**Core services:**

| File | Responsibility |
|---|---|
| `classifier.py` | Sends ticket to LLM, parses structured JSON for dept/priority/confidence/reasoning |
| `rag.py` | Chunks documents, generates NVIDIA embeddings, stores in pgvector, retrieves top-k at query time |
| `response_gen.py` | Combines RAG context + ticket metadata to draft a personalized reply |
| `nl_query.py` | Converts natural language → safe SQL via LLM, runs query, generates chart config + plain-English summary |
| `doc_processor.py` | Parses PDF/DOCX/TXT/MD into chunks and stores embeddings |
| `onboarding_agent.py` | RAG-based Q&A with citation extraction and verified Q&A promotion |

---

## Getting Started

### Prerequisites
- Python 3.11+
- A [Supabase](https://supabase.com) project with `pgvector` enabled
- An [NVIDIA NIM](https://build.nvidia.com) API key

### 1. Clone the repo

```bash
git clone https://github.com/anuj123345/ai-ticket-router.git
cd ai-ticket-router/ticket-router
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

Create a `.env` file in `ticket-router/`:

```env
# NVIDIA NIM
NVIDIA_API_KEY=your_nvidia_nim_api_key

# Supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_supabase_anon_key

# Flask
SECRET_KEY=a-long-random-secret-string
ADMIN_PASSWORD=your-admin-panel-password
```

> `.env` is already in `.gitignore` — never commit it.

### 4. Set up the database

Run this SQL in your Supabase SQL editor:

```sql
-- Enable pgvector
create extension if not exists vector;

-- Tickets
create table tickets (
  id             bigint primary key generated always as identity,
  subject        text not null,
  description    text not null,
  submitter      text not null,
  email          text not null,
  department     text,
  priority       text,
  confidence     float,
  reasoning      text,
  assignee_team  text,
  assignee_email text,
  assignee_slack text,
  draft_response text,
  final_response text,
  sources        text,
  status         text default 'pending_review',
  created_at     timestamptz default now()
);

-- Users
create table users (
  id         bigint primary key generated always as identity,
  name       text not null,
  email      text unique not null,
  password   text not null,
  role       text default 'user',
  created_at timestamptz default now()
);

-- Documents (onboarding knowledge base)
create table documents (
  id          bigint primary key generated always as identity,
  name        text not null,
  type        text not null,
  chunk_count int default 0,
  created_at  timestamptz default now()
);

-- Chunks with vector embeddings
create table chunks (
  id          bigint primary key generated always as identity,
  document_id bigint references documents(id) on delete cascade,
  content     text not null,
  embedding   vector(1024),
  created_at  timestamptz default now()
);

-- Conversations (onboarding chat history)
create table conversations (
  id         bigint primary key generated always as identity,
  question   text not null,
  answer     text not null,
  sources    jsonb,
  created_at timestamptz default now()
);

-- Feedback (thumbs up/down + flagging)
create table feedback (
  id              bigint primary key generated always as identity,
  conversation_id bigint references conversations(id),
  rating          int,
  is_flagged      boolean default false,
  flag_comment    text,
  created_at      timestamptz default now()
);

-- Similarity search function
create or replace function match_chunks(
  query_embedding vector(1024),
  match_count     int default 5
)
returns table (id bigint, content text, similarity float)
language sql stable as $$
  select id, content,
    1 - (embedding <=> query_embedding) as similarity
  from chunks
  order by embedding <=> query_embedding
  limit match_count;
$$;
```

### 5. Run locally

```bash
python app.py
```

Open `http://localhost:5000` — register an account and start routing tickets.

---

## Project Structure

```
ticket-router/
├── app.py                    # Flask app — all routes
├── requirements.txt
├── vercel.json               # Vercel serverless config
├── api/
│   └── index.py              # Vercel entry point (wraps Flask)
├── models/
│   ├── auth.py               # User CRUD + bcrypt hashing
│   └── db.py                 # Supabase client, ticket CRUD, stats
├── services/
│   ├── classifier.py         # LLM ticket classification
│   ├── response_gen.py       # RAG-augmented response drafting
│   ├── rag.py                # Embedding + pgvector retrieval
│   ├── nl_query.py           # NL → SQL pipeline
│   ├── doc_processor.py      # Document chunking + embedding
│   └── onboarding_agent.py   # RAG Q&A with feedback loop
├── static/
│   └── style.css             # Global design system
├── knowledge_base/           # Seed docs for RAG
└── templates/
    ├── base.html             # Shared navbar + layout
    ├── landing.html          # Public marketing page
    ├── login.html / register.html
    ├── welcome.html          # Post-login home
    ├── index.html            # Ticket submission
    ├── review.html           # Human-in-the-loop review
    ├── dashboard.html        # Ticket management
    ├── ops_dashboard.html    # NL-to-SQL analytics
    ├── onboarding.html       # Employee chat UI
    ├── onboarding_admin.html # Document upload panel
    ├── use_cases.html        # Public use cases page
    ├── privacy.html
    ├── terms.html
    └── cookies.html
```

---

## Deploying to Vercel

1. Push to GitHub
2. Import the repo in [Vercel](https://vercel.com/new)
3. Set the **root directory** to `ticket-router/`
4. Add all `.env` variables under **Settings → Environment Variables**
5. Deploy

Vercel runs the app via `api/index.py` which wraps Flask as a serverless function.

---

## Design Decisions

**Human-in-the-loop by default.** The AI drafts but never sends. Every ticket goes through a review screen where a human edits the draft, approves, or escalates. This makes the system safe for production without sacrificing speed.

**RAG over fine-tuning.** Instead of fine-tuning on company data (slow, expensive, hard to update), the system uses retrieval-augmented generation. Upload a new policy PDF and it's instantly queryable — no retraining.

**pgvector over a dedicated vector DB.** Keeps the stack simple. Supabase's pgvector extension handles similarity search alongside relational queries in the same database, avoiding operational overhead of a separate service.

**NL-to-SQL with guardrails.** The LLM generates SQL but the system validates it (SELECT only), parameterizes inputs, and returns structured results with chart config and a natural-language explanation.

---

## Contributing

Contributions are welcome.

1. Fork the repo
2. Create a feature branch: `git checkout -b feat/your-feature`
3. Commit your changes with a clear message
4. Open a pull request describing what changed and why

Please keep PRs focused — one feature or fix per PR.

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

<p align="center">Built with NVIDIA NIM · Flask · Supabase · Vercel</p>
