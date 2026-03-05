#!/usr/bin/env python3
"""Test AI API connectivity for all configured providers.

Sends a minimal prompt to each provider and prints a results table.
"""

import asyncio
import os
import sys
import time

from dotenv import load_dotenv

# Load .env from project root
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

TEST_PROMPT = 'Reply with exactly this JSON and nothing else: {"test": true}'


async def test_anthropic() -> dict:
    """Test Anthropic API connectivity."""
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    if not api_key:
        return {"provider": "anthropic", "model": model, "status": "SKIP", "latency": 0, "error": "ANTHROPIC_API_KEY not set"}

    client = anthropic.AsyncAnthropic(api_key=api_key)
    start = time.perf_counter()
    try:
        resp = await client.messages.create(
            model=model,
            max_tokens=100,
            messages=[{"role": "user", "content": TEST_PROMPT}],
        )
        latency = time.perf_counter() - start
        text = resp.content[0].text if resp.content else ""
        return {"provider": "anthropic", "model": model, "status": "OK", "latency": latency, "error": "-", "response": text}
    except Exception as e:
        latency = time.perf_counter() - start
        return {"provider": "anthropic", "model": model, "status": "FAIL", "latency": latency, "error": str(e)[:80]}


async def test_openai() -> dict:
    """Test OpenAI API connectivity."""
    import openai

    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-nano")
    if not api_key:
        return {"provider": "openai", "model": model, "status": "SKIP", "latency": 0, "error": "OPENAI_API_KEY not set"}

    client = openai.AsyncOpenAI(api_key=api_key)
    start = time.perf_counter()
    try:
        resp = await client.chat.completions.create(
            model=model,
            max_completion_tokens=100,
            messages=[{"role": "user", "content": TEST_PROMPT}],
        )
        latency = time.perf_counter() - start
        text = resp.choices[0].message.content if resp.choices else ""
        return {"provider": "openai", "model": model, "status": "OK", "latency": latency, "error": "-", "response": text}
    except Exception as e:
        latency = time.perf_counter() - start
        return {"provider": "openai", "model": model, "status": "FAIL", "latency": latency, "error": str(e)[:80]}


async def test_gemini() -> dict:
    """Test Gemini via Vertex AI REST API with API key."""
    import httpx

    api_key = os.getenv("GEMINI_API_KEY")
    model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    project = os.getenv("VERTEX_PROJECT")
    location = os.getenv("VERTEX_LOCATION", "us-central1")

    if not api_key:
        return {"provider": "gemini", "model": model, "status": "SKIP", "latency": 0, "error": "GEMINI_API_KEY not set"}
    if not project:
        return {"provider": "gemini", "model": model, "status": "SKIP", "latency": 0, "error": "VERTEX_PROJECT not set"}

    url = (
        f"https://{location}-aiplatform.googleapis.com/v1/"
        f"projects/{project}/locations/{location}/publishers/google/models/"
        f"{model}:generateContent"
    )
    body = {"contents": [{"role": "user", "parts": [{"text": TEST_PROMPT}]}]}

    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, params={"key": api_key}, json=body)
        if resp.status_code >= 400:
            latency = time.perf_counter() - start
            return {
                "provider": "gemini",
                "model": model,
                "status": "FAIL",
                "latency": latency,
                "error": f"HTTP {resp.status_code}",
            }
        latency = time.perf_counter() - start
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return {"provider": "gemini", "model": model, "status": "OK", "latency": latency, "error": "-", "response": text}
    except Exception as e:
        latency = time.perf_counter() - start
        return {"provider": "gemini", "model": model, "status": "FAIL", "latency": latency, "error": str(e)[:80]}


def print_results(results: list[dict]) -> None:
    """Print formatted results table."""
    # Column widths
    pw, mw, sw, lw, ew = 12, 28, 8, 10, 50
    header = f"{'Provider':<{pw}}| {'Model':<{mw}}| {'Status':<{sw}}| {'Latency':<{lw}}| {'Error':<{ew}}"
    sep = f"{'-'*pw}|{'-'*mw}|{'-'*sw}|{'-'*lw}|{'-'*ew}"

    print()
    print(header)
    print(sep)
    for r in results:
        latency_str = f"{r['latency']:.2f}s" if r["latency"] else "-"
        print(f"{r['provider']:<{pw}}| {r['model']:<{mw}}| {r['status']:<{sw}}| {latency_str:<{lw}}| {r.get('error', '-'):<{ew}}")

    # Print response snippets for successful calls
    print()
    for r in results:
        if r.get("response"):
            snippet = r["response"][:120].replace("\n", " ")
            print(f"  {r['provider']} response: {snippet}")
    print()


async def main() -> None:
    print("ShareSentinel - AI API Connectivity Test")
    print("=" * 50)

    results = []

    # Run all three providers concurrently
    all_results = await asyncio.gather(
        test_anthropic(), test_openai(), test_gemini()
    )
    results.extend(all_results)

    print_results(results)

    # Summary
    ok_count = sum(1 for r in results if r["status"] == "OK")
    fail_count = sum(1 for r in results if r["status"] == "FAIL")
    skip_count = sum(1 for r in results if r["status"] == "SKIP")
    print(f"Results: {ok_count} OK, {fail_count} FAIL, {skip_count} SKIP")

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
