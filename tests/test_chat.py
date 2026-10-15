"""
Tests for the shared chat path (services/ai_models.chat_completion) and the
ticket flow that depends on it. Offline: the NVIDIA client is faked.

Reproduces the 2026-10-15 bug: a reasoning model spent its whole token budget
thinking, returned an empty answer, and the ticket was saved as
General/Medium with a blank draft.
"""
import types

import pytest

from services import ai_models
from services.ai_models import EmptyReplyError, ModelUnavailableError


class HTTPError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status_code, self.body = status, {"detail": detail}


class FakeChat:
    """content: what the model returns. reject_template: 400 on chat_template_kwargs."""
    def __init__(self, content="OK", finish="stop", reject_template=False, error=None):
        self.content, self.finish = content, finish
        self.reject_template, self.error = reject_template, error
        self.calls = []
        self.chat = types.SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        if self.reject_template and "extra_body" in kwargs:
            raise HTTPError(400, "Unknown field chat_template_kwargs")
        msg = types.SimpleNamespace(content=self.content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason=self.finish)])


def test_thinking_is_switched_off_by_default():
    nim = FakeChat()
    ai_models.chat_completion([{"role": "user", "content": "hi"}], max_tokens=50, nim=nim)
    assert nim.calls[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_thinking_can_be_requested():
    nim = FakeChat()
    ai_models.chat_completion([{"role": "user", "content": "hi"}], max_tokens=50, nim=nim, thinking=True)
    assert "extra_body" not in nim.calls[0]


def test_budget_spent_thinking_raises_instead_of_returning_blank():
    nim = FakeChat(content="<think>Let me consider the departments one by one", finish="length")
    with pytest.raises(EmptyReplyError, match="ran out of tokens"):
        ai_models.chat_completion([{"role": "user", "content": "classify"}], max_tokens=256, nim=nim)


def test_model_that_rejects_the_switch_is_retried_without_it():
    nim = FakeChat(content="fine", reject_template=True)
    out = ai_models.chat_completion([{"role": "user", "content": "hi"}], max_tokens=50, nim=nim)
    assert out == "fine"
    assert len(nim.calls) == 2 and "extra_body" not in nim.calls[1]


def test_retired_chat_model_is_a_clear_error():
    nim = FakeChat(error=HTTPError(410, "The model has reached its end of life"))
    with pytest.raises(ModelUnavailableError, match="end of life"):
        ai_models.chat_completion([{"role": "user", "content": "hi"}], max_tokens=50, nim=nim)


# ── Ticket flow ──────────────────────────────────────────────────────────────

@pytest.fixture
def model_says(monkeypatch):
    def _set(fake):
        monkeypatch.setattr(ai_models, "client", lambda **kw: fake)
        return fake
    return _set


def test_vpn_ticket_is_classified_from_a_clean_json_reply(model_says):
    from services.classifier import classify_ticket
    model_says(FakeChat(content='{"department": "IT", "priority": "High", "confidence": 0.92, '
                                '"reasoning": "VPN outage blocking one person before a client call."}'))
    out = classify_ticket("vpn issue", "My laptop won't connect to VPN, client call at 3pm")
    assert (out["department"], out["priority"]) == ("IT", "High")


def test_classification_survives_a_reasoning_block_before_the_json(model_says):
    from services.classifier import classify_ticket
    model_says(FakeChat(content='<think>VPN is IT.</think>\n{"department": "IT", "priority": "High", '
                                '"confidence": 0.9, "reasoning": "VPN."}'))
    assert classify_ticket("vpn", "down")["department"] == "IT"


def test_failed_classification_says_why_and_has_zero_confidence(model_says):
    from services.classifier import classify_ticket
    model_says(FakeChat(content="<think>hmm", finish="length"))
    out = classify_ticket("vpn", "down")
    assert out["confidence"] == 0.0
    assert "EmptyReplyError" in out["reasoning"]


def test_failed_draft_is_never_saved_blank(model_says, monkeypatch):
    from services import response_gen
    monkeypatch.setattr(response_gen.rag, "retrieve", lambda q, n_results=4: [])
    monkeypatch.setattr(response_gen.rag, "format_context", lambda chunks: "")
    model_says(FakeChat(content="", finish="length"))
    draft = response_gen.generate_draft("vpn", "down", "IT")["draft"]
    assert draft.startswith("[AI draft unavailable")


def test_draft_markdown_is_converted_to_plain_text(model_says, monkeypatch):
    from services import response_gen
    monkeypatch.setattr(response_gen.rag, "retrieve", lambda q, n_results=4: [])
    monkeypatch.setattr(response_gen.rag, "format_context", lambda chunks: "")
    model_says(FakeChat(content="## Fix\n1. **Restart the VPN client** completely.\n2. Check __2FA__."))
    draft = response_gen.generate_draft("vpn", "down", "IT")["draft"]
    assert "**" not in draft and "__" not in draft and "#" not in draft
    assert "1. Restart the VPN client completely." in draft


def test_vpn_ticket_only_cites_the_it_doc():
    from services import rag
    sources = {c["source"] for c in rag.retrieve("vpn is not working in my laptop. My laptop won't connect to VPN since this morning")}
    assert sources == {"it_support.md"}
