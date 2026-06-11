"""
RAG Service — indexes knowledge base docs and retrieves relevant context
using TF-IDF keyword matching (pure Python, no external API needed).
The LLM still uses NVIDIA NIM for classification and response generation.
"""

import os
import re
import glob
import math
from collections import Counter

KB_DIR = os.path.join(os.path.dirname(__file__), "..", "knowledge_base")

# In-memory index: list of {"text", "source", "category", "tf"}
_index: list[dict] = []
_indexed = False


def _tokenize(text: str) -> list[str]:
    return re.findall(r'\b[a-z][a-z0-9]{2,}\b', text.lower())


def _tf(tokens: list[str]) -> dict:
    count = Counter(tokens)
    total = max(len(tokens), 1)
    return {w: c / total for w, c in count.items()}


def _chunk_text(text: str, chunk_size: int = 400, overlap: int = 80) -> list[str]:
    words = text.split()
    chunks = []
    start = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunks.append(" ".join(words[start:end]))
        start += chunk_size - overlap
    return chunks


def _build_index():
    global _index, _indexed
    if _indexed:
        return

    md_files = glob.glob(os.path.join(KB_DIR, "*.md"))
    if not md_files:
        print("Warning: No knowledge base files found.")
        _indexed = True
        return

    for filepath in md_files:
        filename = os.path.basename(filepath)
        category = filename.replace(".md", "").replace("_", " ").title()
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        for chunk in _chunk_text(content):
            tokens = _tokenize(chunk)
            _index.append({
                "text":     chunk,
                "source":   filename,
                "category": category,
                "tf":       _tf(tokens),
                "tokens":   set(tokens),
            })

    # Compute IDF across all chunks
    N = len(_index)
    all_words = set(w for item in _index for w in item["tokens"])
    idf = {}
    for word in all_words:
        df = sum(1 for item in _index if word in item["tokens"])
        idf[word] = math.log((N + 1) / (df + 1)) + 1

    # Store tfidf score vector in each chunk
    for item in _index:
        item["tfidf"] = {w: item["tf"].get(w, 0) * idf.get(w, 1) for w in item["tokens"]}

    _indexed = True
    print(f"RAG: Indexed {len(_index)} chunks from {len(md_files)} files.")


def _score(query: str, item: dict) -> float:
    q_tokens = _tokenize(query)
    if not q_tokens:
        return 0.0
    return sum(item["tfidf"].get(t, 0) for t in q_tokens)


def retrieve(query: str, n_results: int = 4) -> list[dict]:
    """Return the most relevant knowledge base chunks for a query."""
    _build_index()
    if not _index:
        return []

    scored = sorted(_index, key=lambda x: _score(query, x), reverse=True)
    seen, results = set(), []
    for item in scored:
        key = item["source"] + item["text"][:60]
        if key not in seen:
            seen.add(key)
            results.append({"text": item["text"], "source": item["source"], "category": item["category"]})
        if len(results) >= n_results:
            break
    return results


def format_context(chunks: list[dict]) -> str:
    if not chunks:
        return "No relevant documentation found."
    return "\n\n---\n\n".join(f"[Source: {c['category']}]\n{c['text']}" for c in chunks)
