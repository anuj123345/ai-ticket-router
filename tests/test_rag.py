"""
RAG retrieval diagnostic test.
Run from ticket-router/ directory:

    python tests/test_rag.py

Requires .env with SUPABASE_URL, SUPABASE_KEY, NVIDIA_API_KEY.
Documents must be uploaded first (upload sample_docs/ via /onboarding/admin).
"""
import sys, os
# Always resolve relative to this file, not the cwd
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv
env_path = os.path.join(ROOT, ".env")
loaded = load_dotenv(env_path, override=True)
print(f"[DEBUG] .env path: {env_path}")
print(f"[DEBUG] .env loaded: {loaded}")
print(f"[DEBUG] SUPABASE_URL: {os.environ.get('SUPABASE_URL', 'NOT SET')[:40]}")
print(f"[DEBUG] SUPABASE_KEY: {'SET' if os.environ.get('SUPABASE_KEY') else 'NOT SET'}")

from services.onboarding_agent import (
    expand_query, extract_key_terms, search_doc_chunks, answer_question
)

# ─────────────────────────────────────────────────────────────────────────────
# PART 1 — Synonym expansion unit tests (no network)
# ─────────────────────────────────────────────────────────────────────────────

EXPANSION_TESTS = [
    ("how many PTO days do I get",         "vacation"),
    ("what is the maternal leave policy",  "parental"),
    ("can I work from home",               "remote work"),
    ("what is the retirement plan",        "401k"),
    ("what medical coverage do I get",     "health insurance"),
    ("how do I reimburse my expenses",     "expense reimbursement"),
    ("when does my trial period end",      "probation"),
    ("I need therapy sessions",            "EAP"),
    ("what is the training budget",        "learning development"),
    ("I was fired, what happens",          "termination"),
]

print("\n── PART 1: Synonym expansion ──────────────────────────────────────")
print(f"{'Query':<45} {'Expected term':<22} {'Result'}")
print("─" * 85)
passed = 0
for query, expected in EXPANSION_TESTS:
    expanded = expand_query(query)
    ok = expected.lower() in expanded.lower()
    status = "✓ PASS" if ok else "✗ FAIL"
    if ok:
        passed += 1
    print(f"{query:<45} {expected:<22} {status}")
    if not ok:
        print(f"  expanded → {expanded}")
print(f"\nExpansion: {passed}/{len(EXPANSION_TESTS)} passed\n")

# ─────────────────────────────────────────────────────────────────────────────
# PART 2 — Key term extraction unit tests (no network)
# ─────────────────────────────────────────────────────────────────────────────

KEY_TERM_TESTS = [
    ("how many PTO days do I get in my first year", ["pto", "days", "first", "year"]),
    ("what is the expense reimbursement process",   ["expense", "reimbursement", "process"]),
    ("can I bring my dog to the office",            ["bring", "dog", "office"]),
]

print("── PART 2: Key term extraction ────────────────────────────────────")
for query, expected_terms in KEY_TERM_TESTS:
    extracted = extract_key_terms(query)
    found = [t for t in expected_terms if t in extracted.lower()]
    ok = len(found) >= len(expected_terms) // 2
    status = "✓ PASS" if ok else "✗ FAIL"
    print(f"  {query[:50]:<52} → '{extracted}' {status}")
print()

# ─────────────────────────────────────────────────────────────────────────────
# PART 3 — Retrieval tests (requires Supabase + uploaded docs)
# ─────────────────────────────────────────────────────────────────────────────

RETRIEVAL_TESTS = [
    # (label, question, keyword_must_appear_in_chunks)
    # Easy — direct match
    ("EASY  vacation direct",        "vacation policy",                       "vacation"),
    ("EASY  sick leave direct",      "sick leave policy",                     "sick"),
    ("EASY  expense direct",         "expense reimbursement",                 "expense"),

    # Synonym / semantic gap
    ("SYN   PTO→vacation",           "how many PTO days do I get",            "vacation"),
    ("SYN   maternal→parental",      "what is the maternal leave policy",     "parental"),
    ("SYN   WFH→remote",             "can I work from home",                  "remote"),
    ("SYN   retirement→401k",        "what is the retirement plan",           "401"),
    ("SYN   medical→health",         "what medical coverage do I get",        "health"),
    ("SYN   trial period→probation", "when does my trial period end",         "probation"),
    ("SYN   training→L&D",           "how do I use my training budget",       "learning"),

    # Paraphrase / implied
    ("PARA  days off",               "how many days off do I have per year",  "vacation"),
    ("PARA  doctor",                 "what do I do if I'm sick",              "sick"),
    ("PARA  new laptop",             "how do I set up my new work computer",  "laptop"),
    ("PARA  refer a friend",         "can I refer someone for a job",         "referral"),

    # Multi-hop (answer spans multiple sections)
    ("MULTI benefits summary",       "what benefits do I get as an employee", "health"),

    # Negative — should return 0 or irrelevant chunks
    ("MISS  dog policy",             "what is the office dog policy",          None),
    ("MISS  parking",                "is there parking at the office",         None),
]

print("── PART 3: Retrieval quality (requires docs uploaded) ─────────────")
print(f"{'Label':<35} {'Chunks':>6}  {'Result':<8}  Docs found")
print("─" * 80)

retrieval_passed = 0
retrieval_total  = 0

try:
    for label, q, keyword in RETRIEVAL_TESTS:
        retrieval_total += 1
        chunks = search_doc_chunks(q, max_results=5)

        if keyword is None:
            # Expect zero relevant chunks
            ok = len(chunks) == 0
        else:
            ok = any(keyword.lower() in c.get('content', '').lower() for c in chunks)

        if ok:
            retrieval_passed += 1

        status = "✓ PASS" if ok else "✗ FAIL"
        doc_names = list({c.get('document_name', '?') for c in chunks})[:2]
        print(f"{label:<35} {len(chunks):>6}  {status:<8}  {doc_names}")

    print(f"\nRetrieval: {retrieval_passed}/{retrieval_total} passed\n")

except Exception as e:
    print(f"\n[ERROR] Could not connect to Supabase: {e}")
    print("Make sure .env has SUPABASE_URL and SUPABASE_KEY set.\n")

# ─────────────────────────────────────────────────────────────────────────────
# PART 4 — End-to-end answer quality (requires Supabase + NVIDIA)
# ─────────────────────────────────────────────────────────────────────────────

E2E_TESTS = [
    {
        "question": "How many PTO days do I get in my first year?",
        "must_contain": ["15"],
        "must_not_contain": ["I couldn't find"],
    },
    {
        "question": "What is the maternity leave policy?",
        "must_contain": ["parental", "16"],
        "must_not_contain": ["I couldn't find"],
    },
    {
        "question": "Can I work from home every day?",
        "must_contain": ["3"],  # "up to 3 days"
        "must_not_contain": ["I couldn't find"],
    },
    {
        "question": "What is the office dog policy?",
        "must_contain": ["couldn't find"],
        "must_not_contain": [],
    },
    {
        "question": "How do I submit expenses over $100?",
        "must_contain": ["manager", "approval"],
        "must_not_contain": ["I couldn't find"],
    },
]

print("── PART 4: End-to-end answer quality ───────────────────────────────")
e2e_passed = 0

try:
    for t in E2E_TESTS:
        result = answer_question(t["question"])
        answer = result.get("answer", "").lower()
        has_docs = result.get("has_docs", False)

        ok = (
            all(kw.lower() in answer for kw in t["must_contain"]) and
            all(kw.lower() not in answer for kw in t["must_not_contain"])
        )
        if ok:
            e2e_passed += 1

        status = "✓ PASS" if ok else "✗ FAIL"
        print(f"\n  Q: {t['question']}")
        print(f"  A (truncated): {result.get('answer','')[:120]}...")
        print(f"  has_docs={has_docs}  sources={len(result.get('sources',[]))}  {status}")
        if not ok:
            missing = [kw for kw in t["must_contain"] if kw.lower() not in answer]
            print(f"  MISSING expected terms: {missing}")

    print(f"\nE2E: {e2e_passed}/{len(E2E_TESTS)} passed")

except Exception as e:
    print(f"\n[ERROR] Could not run E2E: {e}")
    print("Make sure NVIDIA_API_KEY is set and docs are uploaded.\n")

print("\n── Test complete ───────────────────────────────────────────────────")
