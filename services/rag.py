"""
RAG Service — indexes knowledge base docs into ChromaDB and retrieves
relevant context for a given query using sentence-transformers embeddings.
"""

import os
import glob
import chromadb
from chromadb.utils import embedding_functions

# Use a local sentence-transformer model for embeddings (no API cost).
EMBED_MODEL = "all-MiniLM-L6-v2"
COLLECTION_NAME = "knowledge_base"
KB_DIR = os.path.join(os.path.dirname(__file__), "..", "knowledge_base")

# Singleton ChromaDB client + collection
_client = None
_collection = None


def _get_collection():
    global _client, _collection
    if _collection is not None:
        return _collection

    _client = chromadb.Client()
    ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)
    _collection = _client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )

    # Index all .md files from knowledge_base/ on first load
    _index_knowledge_base(_collection)
    return _collection


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


def _index_knowledge_base(collection):
    """Load all markdown files and add them to the vector store."""
    md_files = glob.glob(os.path.join(KB_DIR, "*.md"))
    if not md_files:
        print("Warning: No knowledge base files found.")
        return

    docs, ids, metas = [], [], []
    for filepath in md_files:
        filename = os.path.basename(filepath)
        category = filename.replace(".md", "").replace("_", " ").title()
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        chunks = _chunk_text(content)
        for i, chunk in enumerate(chunks):
            doc_id = f"{filename}_{i}"
            docs.append(chunk)
            ids.append(doc_id)
            metas.append({"source": filename, "category": category, "chunk": i})

    # Only add if not already indexed (avoid duplicates on restart)
    existing = collection.get()["ids"]
    new_docs = [(d, i, m) for d, i, m in zip(docs, ids, metas) if i not in existing]
    if new_docs:
        d, i, m = zip(*new_docs)
        collection.add(documents=list(d), ids=list(i), metadatas=list(m))
        print(f"RAG: Indexed {len(new_docs)} chunks from {len(md_files)} files.")


def retrieve(query: str, n_results: int = 4) -> list[dict]:
    """
    Retrieve the most relevant knowledge base chunks for a query.

    Returns:
        List of dicts: {"text": str, "source": str, "category": str}
    """
    collection = _get_collection()
    results = collection.query(query_texts=[query], n_results=n_results)

    retrieved = []
    for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
        retrieved.append({
            "text": doc,
            "source": meta.get("source", "unknown"),
            "category": meta.get("category", "General"),
        })
    return retrieved


def format_context(chunks: list[dict]) -> str:
    """Format retrieved chunks into a single context string for the LLM prompt."""
    if not chunks:
        return "No relevant documentation found."

    parts = []
    for chunk in chunks:
        parts.append(f"[Source: {chunk['category']}]\n{chunk['text']}")
    return "\n\n---\n\n".join(parts)
