# AI Ticket Router

An internal support ticket system where AI classifies requests, routes them to the right team, and drafts responses grounded in company documentation — with a human-in-the-loop approval step before anything is sent.

Built as part of a Forward Deployed Engineer (FDE) portfolio to demonstrate real-world AI workflow integration skills.

---

## The Problem

Internal support teams at most companies handle hundreds of repetitive tickets per week. The average ticket takes 5–15 minutes to triage, route, and respond to — even when the answer exists in an internal doc. This is a pure workflow inefficiency problem that AI is well-suited to solve.

**What this system does:**

- Receives a support ticket (subject + description) from an employee
- Uses an LLM to classify it by department and priority in under 1 second
- Retrieves relevant company documentation using RAG (vector similarity search)
- Drafts a grounded, accurate response based only on real company docs
- Presents the draft to a human agent for review and editing before approval
- Logs all tickets and outcomes to a dashboard

---

## Architecture

```
Employee submits ticket
        │
        ▼
┌─────────────────────┐
│  Flask Web App      │  ← Handles routing, sessions, DB writes
└────────┬────────────┘
         │
         ├──► Classifier (LLM)
         │    • Sends subject + description to NVIDIA NIM API
         │    • Returns: department, priority, confidence, reasoning
         │
         ├──► RAG Pipeline
         │    • Embeds query with sentence-transformers (local)
         │    • Retrieves top-4 chunks from ChromaDB
         │    • Formats context for prompt injection
         │
         ├──► Response Generator (LLM + RAG context)
         │    • Drafts a grounded response using retrieved docs
         │    • Does NOT invent policies or contacts
         │
         └──► Human Review (HITL)
              • Agent reviews and edits the draft
              • Approves → stored as final, status = resolved
              • Escalates → flagged for manual handling
```

---

## Key Technical Decisions

| Decision | Choice | Why |
|---|---|---|
| LLM API | NVIDIA NIM (OpenAI-compatible) | Free tier, fast, swap-friendly |
| Embeddings | sentence-transformers (local) | No API cost, runs offline |
| Vector DB | ChromaDB (in-memory) | Zero config, easy to swap to Pinecone |
| Storage | SQLite | No infra needed for a portfolio project |
| Frontend | Flask + Bootstrap 5 | Clean, production-like UI without React complexity |

**Why human-in-the-loop matters here:** An AI response about payroll or security policy going directly to an employee without review is a real liability. The HITL step is not just a safety feature — it's the architecture choice that makes this deployable at an actual company.

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/your-username/ai-ticket-router
cd ai-ticket-router
pip install -r requirements.txt
```

### 2. Get a free NVIDIA NIM API key

1. Go to [build.nvidia.com](https://build.nvidia.com)
2. Sign up (free) and click any model → "Get API Key"
3. Copy the key (starts with `nvapi-...`)

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env and add your NVIDIA_API_KEY
```

### 4. Run

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000)

---

## Project Structure

```
ticket-router/
├── app.py                      # Flask routes and application logic
├── requirements.txt
├── .env.example
├── knowledge_base/             # Company docs indexed for RAG
│   ├── it_support.md
│   ├── hr_policies.md
│   ├── finance_procedures.md
│   └── engineering_runbook.md
├── services/
│   ├── llm_client.py           # NVIDIA NIM API wrapper
│   ├── rag.py                  # ChromaDB indexing + retrieval
│   ├── classifier.py           # Ticket classification
│   └── response_gen.py         # Draft response generation
├── models/
│   └── db.py                   # SQLite operations
├── templates/
│   ├── base.html
│   ├── index.html              # Ticket submission
│   ├── review.html             # Human-in-the-loop review
│   └── dashboard.html          # Ticket history + stats
└── static/
    └── style.css
```

---

## Extending This Project

Ideas to take this further:

- **Slack integration** — receive tickets from Slack messages via a slash command
- **Auto-approve threshold** — if confidence > 90% and priority is Low, skip human review
- **Email integration** — send the approved response via SendGrid or Gmail API
- **Analytics dashboard** — track resolution time, accuracy by department, agent edits
- **Feedback loop** — agents rate AI draft quality, use that data to improve prompts
- **Multi-tenant** — support multiple companies with their own knowledge bases

---

## Why This Project

This is a practical FDE-style project. Forward Deployed Engineers at companies like Anthropic, OpenAI, and Palantir build exactly this type of system: AI that plugs into existing workflows, respects human oversight, integrates with company data, and solves a real business problem — not a demo.

The skills demonstrated here: RAG pipeline design, LLM API integration, workflow logic, human-in-the-loop architecture, and building something production-adjacent from scratch.
