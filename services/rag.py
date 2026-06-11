"""
RAG Service — indexes knowledge base docs using NVIDIA NIM embeddings API
and retrieves relevant context via cosine similarity (pure Python/numpy).
No sentence-transformers or ChromaDB required — works on Vercel.
"""

import os
import glob
import json
import math
from openai import OpenAI

KB_DIR = os.path.join(os.path.dirname(__file__), "..", "knowledge_base")

# In-memory vector store: list of {"text", "source", "category", "embedding"}
_index: list[dict] = []
_indexed = False


def _get_embed_client() -> OpenAI:
    return OpenAI(
        api_key=os.getenv("NVIDIA_API_KEY"),
        base_url=os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
    )


def _embed(texts: list[str]) -> list[list[float]]:
    """Get embeddings from NVIDIA NIM for a list of texts."""
    client = _get_embed_client()
    response = client.embeddings.create(
        model="nvidia/nv-embedqa-mistral-7b-v2",
        input=texts,
        encoding_format="float",
        extra_body={"input_type": "passage", "truncate": "END"},
    )
    return [item.embedding for item in response.data]


def _embed_query(text: str) -> list[float]:
    """Get embedding for a query string."""
    client = _get_embed_client()
    response = client.embeddings.create(
        model="nvidia/nv-embedqa-mistral-7b-v2",
        input=[text],
        encoding_format="float",
        extra_body={"input_type": "query", "truncate": "END"},
    )
    return response.data[0].embedding


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _chunk_text(text: str, chunk_size: int = 400, overlap: int = 80) -> list[str]:
    """Split text into overlapping chunks for better retrieval."""
    words = text.split()
    chunks = []
    start = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunks.append(" ".join(words[start:end]))
        start += chunk_size - overlap
    return chunks


def _build_index():
    """Load all markdown files, embed them, and store in memory."""
    global _index, _indexed
    if _indexed:
        return

    md_files = glob.glob(os.path.join(KB_DIR, "*.md"))
    if not md_files:
        print("Warning: No knowledge base files found.")
        _indexed = True
        return

    all_chunks = []
    all_meta = []

    for filepath in md_files:
        filename = os.path.basename(filepath)
        category = filename.replace(".md", "").replace("_", " ").title()
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        chunks = _chunk_text(content)
        for chunk in chunks:
            all_chunks.append(chunk)
            all_meta.append({"source": filename, "category": category})

    # Embed in batches of 10 to stay within API limits
    batch_size = 10
    embeddings = []
    for i in range(0, len(all_chunks), batch_size):
        batch = all_chunks[i:i + batch_size]
        batch_embeddings = _embed(batch)
        embeddings.extend(batch_embeddings)

    for chunk, meta, emb in zip(all_chunks, all_meta, embeddings):
        _index.append({
            "text": chunk,
            "source": meta["source"],
            "category": meta["category"],
            "embedding": emb,
        })

    print(f"RAG: Indexed {len(_index)} chunks from {len(md_files)} files.")
    _indexed = True


def retrieve(query: str, n_results: int = 4) -> list[dict]:
    """
    Retrieve the most relevant knowledge base chunks for a query.
    Returns list of dicts: {"text", "source", "category"}
    """
    _build_index()

    if not _index:
        return []

    query_emb = _embed_query(query)

    scored = []
    for item in _index:
        score = _cosine_similarity(query_emb, item["embedding"])
        scored.append((score, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:n_results]

    return [
        {"text": item["text"], "source": item["source"], "category": item["category"]}
        for _, item in top
    ]


def format_context(chunks: list[dict]) -> str:
    """Format retrieved chunks into a single context string for the LLM prompt."""
    if not chunks:
        return "No relevant documentation found."
    parts = []
    for chunk in chunks:
        parts.append(f"[Source: {chunk['category']}]\n{chunk['text']}")
    return "\n\n---\n\n".join(parts)
