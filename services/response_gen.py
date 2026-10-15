"""
Response Generator — uses RAG context + LLM to draft a helpful,
professional reply to a support ticket. Human reviews before sending.
"""

import logging
import re

from services.llm_client import chat
from services import rag

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a helpful internal support agent at a tech company.
Your job is to draft a clear, professional, and empathetic response to an employee support ticket.

Guidelines:
- Be concise and direct. Get to the answer quickly.
- Use the provided company documentation to give accurate information.
- If the documentation doesn't cover the issue, acknowledge that and suggest next steps.
- Never make up policies, procedures, or contact information.
- End with a friendly offer to help further.
- Use a professional but warm tone (not robotic, not over-casual).
- Format the response with clear paragraphs. Do not use bullet points unless listing steps.
- Write plain text only: it is sent as a plain email. No Markdown - no **bold**, no # headings. Numbered steps like "1. " are fine.

You will be given:
1. The ticket subject and description
2. Relevant documentation from the company knowledge base
3. The department this ticket was routed to

Draft ONLY the response body. Do not include "Subject:", greetings like "Dear [Name]", or sign-offs like "Best regards, [Agent]" — those are added automatically."""


def generate_draft(
    subject: str,
    description: str,
    department: str,
    submitter_name: str = "there",
) -> dict:
    """
    Generate a draft response for a ticket using RAG.

    Returns:
        {
            "draft": str,
            "sources": list[dict],
            "context_used": str
        }
    """
    # Step 1: Retrieve relevant docs
    query = f"{subject} {description}"
    chunks = rag.retrieve(query, n_results=4)
    context = rag.format_context(chunks)

    # Step 2: Build the prompt
    user_message = f"""Ticket Subject: {subject}

Ticket Description:
{description}

Routed to: {department} team

Relevant company documentation:
---
{context}
---

Please draft a helpful response to this ticket."""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    try:
        draft = chat(messages, temperature=0.4, max_tokens=600)
    except Exception as e:
        log.error("Draft generation failed: %s", e)
        draft = (f"[AI draft unavailable: {e}. "
                 "Please write the response manually before approving.]")

    return {
        "draft": _plain_text(draft),
        "sources": chunks,
        "context_used": context,
    }


def _plain_text(text: str) -> str:
    """Remove Markdown emphasis/headings the model may still add; the reply is a plain email."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    return text


def format_full_response(
    draft: str,
    submitter_name: str,
    agent_team: str,
) -> str:
    """Wrap the draft with greeting and sign-off for the final email."""
    greeting = f"Hi {submitter_name},"
    signoff = f"\nBest regards,\n{agent_team}"
    return f"{greeting}\n\n{draft}\n{signoff}"
