"""
One place for every model the app calls on NVIDIA NIM.

Why this exists: NVIDIA retires hosted models (nv-embedqa-e5-v5 and the
Llama 3.1 chat models returned "410 Gone" from Aug 2026). Model names were
hard-coded in six files with different defaults, so a retirement broke
features silently in several places at once. Change models here, or via the
environment variables below, and nowhere else.

Env vars:
  NVIDIA_API_KEY       required
  NVIDIA_BASE_URL      default https://integrate.api.nvidia.com/v1
  NVIDIA_MODEL         chat model (classification, drafts, Ops, chat, Data Studio)
  NVIDIA_POWER_MODEL   larger model behind the "Power" option in Smart Search
  NVIDIA_EMBED_MODEL   embedding model

Check them live at /admin/health (admin only).
"""
from __future__ import annotations

import logging
import math
import os
import re

from openai import OpenAI

log = logging.getLogger(__name__)

DEFAULT_BASE_URL    = "https://integrate.api.nvidia.com/v1"
# Picked from a live /admin/health run on 2026-10-15: both answered in <2s.
# (meta/llama-3.1-70b-instruct retired 2026-08-26; nemotron-3-ultra-550b timed out.)
DEFAULT_CHAT_MODEL  = "nvidia/nemotron-3.5-lightning-30b-a3b"
DEFAULT_POWER_MODEL = "nvidia/nemotron-3-super-120b-a12b"
DEFAULT_EMBED_MODEL = "nvidia/nemotron-3-embed-1b"

# Must match the column type public.doc_chunks.embedding vector(1024).
# nemotron-3-embed-1b returns 2048 numbers; its model card allows keeping the
# first 1024 and re-normalising (Matryoshka-style), so no schema change.
EMBED_DIM = 1024


class ModelUnavailableError(RuntimeError):
    """The model rejected the request in a way retrying won't fix
    (retired model, bad key, invalid request). Carries a readable reason."""


def chat_model() -> str:
    return os.environ.get("NVIDIA_MODEL") or DEFAULT_CHAT_MODEL


def power_model() -> str:
    return os.environ.get("NVIDIA_POWER_MODEL") or DEFAULT_POWER_MODEL


_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def clean_reply(text: str | None) -> str:
    """Strip a reasoning block (<think>...</think>) that some Nemotron models
    put before the answer, so JSON/SQL parsing sees only the answer."""
    text = _THINK_RE.sub("", text or "")
    # Unclosed block (hit max_tokens mid-thought): drop it entirely.
    if text.lstrip().lower().startswith("<think>"):
        return ""
    return text.strip()


def embed_model() -> str:
    return os.environ.get("NVIDIA_EMBED_MODEL") or DEFAULT_EMBED_MODEL


def client(timeout: float | None = None, max_retries: int = 2) -> OpenAI:
    kwargs = {
        "base_url": os.environ.get("NVIDIA_BASE_URL", DEFAULT_BASE_URL),
        "api_key": os.environ.get("NVIDIA_API_KEY", ""),
        "max_retries": max_retries,
    }
    if timeout is not None:
        kwargs["timeout"] = timeout
    return OpenAI(**kwargs)


def is_permanent_error(e: Exception) -> bool:
    """4xx errors other than timeout/conflict/rate-limit won't succeed on retry."""
    status = getattr(e, "status_code", None)
    return isinstance(status, int) and 400 <= status < 500 and status not in (408, 409, 429)


def describe_error(e: Exception) -> str:
    """Short human-readable reason, e.g. the 'detail' NVIDIA sends with a 410."""
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("message") or body.get("title")
        if detail:
            return str(detail)
    status = getattr(e, "status_code", None)
    return f"HTTP {status}" if status else f"{type(e).__name__}: {e}"


def fit_embedding(vec: list[float]) -> list[float]:
    """Trim to EMBED_DIM and L2-normalise."""
    if len(vec) < EMBED_DIM:
        raise ModelUnavailableError(
            f"Embedding model {embed_model()} returned {len(vec)} dimensions; "
            f"the database needs at least {EMBED_DIM}."
        )
    v = vec[:EMBED_DIM]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def embed(texts: list[str], input_type: str, nim: OpenAI | None = None) -> list[list[float]]:
    """
    Embed texts. input_type is "passage" for stored chunks, "query" for searches.
    Raises ModelUnavailableError for permanent failures; other exceptions
    (timeouts, 5xx, 429) propagate so callers can retry.
    """
    nim = nim or client()
    try:
        resp = nim.embeddings.create(
            model=embed_model(),
            input=texts,
            encoding_format="float",
            extra_body={"input_type": input_type, "truncate": "END"},
        )
    except Exception as e:
        if is_permanent_error(e):
            raise ModelUnavailableError(
                f"Embedding model {embed_model()} unavailable: {describe_error(e)}"
            ) from e
        raise
    ordered = sorted(resp.data, key=lambda x: x.index)
    return [fit_embedding(item.embedding) for item in ordered]


def query_embedding(text: str) -> list[float] | None:
    """Embedding for a search query, or None (callers fall back to keyword search)."""
    try:
        return embed([text], "query")[0]
    except Exception as e:
        log.warning("Query embedding failed, using keyword search only: %s", describe_error(e))
        return None
