"""
LLM Client — wraps NVIDIA NIM API (OpenAI-compatible).
All LLM calls go through here so swapping models is a one-line change.
"""

import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()


def get_client() -> OpenAI:
    return OpenAI(
        api_key=os.getenv("NVIDIA_API_KEY"),
        base_url=os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
    )


def chat(messages: list[dict], temperature: float = 0.2, max_tokens: int = 1024) -> str:
    """
    Send a list of messages to the LLM and return the response text.

    Args:
        messages: List of {"role": "system"|"user"|"assistant", "content": "..."}
        temperature: Lower = more deterministic. Use 0.2 for classification, 0.5 for drafts.
        max_tokens: Max tokens in the response.

    Returns:
        Response text as a string.
    """
    client = get_client()
    model = os.getenv("NVIDIA_MODEL", "meta/llama-3.1-70b-instruct")

    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content.strip()
