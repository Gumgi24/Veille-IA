"""Batch API adapters for the four providers.

Every provider exposes the same four operations, dispatched via submit() /
poll() / fetch() / cancel():

    submit(provider, client, settings, items) -> ref
        items: list of (custom_id, prompt_text); ref: JSON-serializable dict
        holding the provider-side identifiers (persisted in the batches table
        so a run can resume after a server restart).
    poll(provider, client, settings, ref)   -> "pending" | "done" | "failed"
    fetch(provider, client, settings, ref)  -> {custom_id: result}
        result: {"status": "ok", "model", "answer", "urls"}
              | {"status": "error", "message", "detail"}
    cancel(provider, client, settings, ref) -> None (best effort)

Request bodies and response parsing are shared with the live path via
providers.BUILDERS / providers.PARSERS — a batch result body has the same
shape as the corresponding synchronous API response.
"""
from __future__ import annotations

import json

import httpx

from .providers import (
    ANTHROPIC_URL,
    GEMINI_BASE,
    XAI_BASE,
    BUILDERS,
    PARSERS,
    ProviderError,
    anthropic_headers,
    gemini_headers,
    openai_headers,
    xai_headers,
)

OPENAI_BASE = "https://api.openai.com/v1"
ANTHROPIC_BATCHES_URL = ANTHROPIC_URL.replace("/messages", "/messages/batches")


class BatchError(Exception):
    def __init__(self, message: str, detail=None):
        super().__init__(message)
        self.detail = detail


def _check(resp: httpx.Response, provider: str) -> None:
    if resp.status_code >= 400:
        raise BatchError(f"{provider} batch: HTTP {resp.status_code}", detail=resp.text[:4000])


def _err(message: str, detail=None) -> dict:
    return {"status": "error", "message": message, "detail": detail}


def _parse_or_err(provider: str, settings: dict, data: dict) -> dict:
    try:
        return {"status": "ok", **PARSERS[provider](settings, data)}
    except ProviderError as exc:
        return _err(str(exc), exc.detail)
    except Exception as exc:  # defensive: never let one result kill the batch
        return _err(f"{provider}: parse error: {exc!r}")


# ================================================================ OpenAI
# Files API (JSONL, purpose=batch) + /v1/batches. Results come back as two
# JSONL files (output + errors), keyed by custom_id.

async def _openai_submit(client, settings, items) -> dict:
    lines = [
        json.dumps(
            {
                "custom_id": cid,
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": BUILDERS["openai"](settings, prompt),
            },
            ensure_ascii=False,
        )
        for cid, prompt in items
    ]
    headers = openai_headers(settings)
    resp = await client.post(
        f"{OPENAI_BASE}/files",
        headers=headers,
        data={"purpose": "batch"},
        files={"file": ("batch.jsonl", ("\n".join(lines) + "\n").encode(), "application/jsonl")},
    )
    _check(resp, "openai")
    input_file_id = resp.json()["id"]
    resp = await client.post(
        f"{OPENAI_BASE}/batches",
        headers=headers,
        json={
            "input_file_id": input_file_id,
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h",
        },
    )
    _check(resp, "openai")
    return {"batch_id": resp.json()["id"], "input_file_id": input_file_id}


async def _openai_poll(client, settings, ref) -> str:
    resp = await client.get(
        f"{OPENAI_BASE}/batches/{ref['batch_id']}", headers=openai_headers(settings)
    )
    _check(resp, "openai")
    status = resp.json().get("status", "")
    if status == "completed":
        return "done"
    if status in ("failed", "expired", "cancelled"):
        return "failed"
    return "pending"  # validating | in_progress | finalizing | cancelling


async def _openai_fetch(client, settings, ref) -> dict:
    headers = openai_headers(settings)
    resp = await client.get(f"{OPENAI_BASE}/batches/{ref['batch_id']}", headers=headers)
    _check(resp, "openai")
    data = resp.json()
    results: dict[str, dict] = {}
    for file_id in (data.get("output_file_id"), data.get("error_file_id")):
        if not file_id:
            continue
        resp = await client.get(f"{OPENAI_BASE}/files/{file_id}/content", headers=headers)
        _check(resp, "openai")
        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = row.get("custom_id")
            if not cid:
                continue
            response = row.get("response") or {}
            if response.get("status_code") == 200:
                results[cid] = _parse_or_err("openai", settings, response.get("body") or {})
            else:
                results[cid] = _err(
                    f"openai batch: HTTP {response.get('status_code')}",
                    row.get("error") or response.get("body"),
                )
    return results


async def _openai_cancel(client, settings, ref) -> None:
    await client.post(
        f"{OPENAI_BASE}/batches/{ref['batch_id']}/cancel", headers=openai_headers(settings)
    )


# ================================================================ Anthropic
# Message Batches API: one JSON request, results as a JSONL stream at
# results_url, keyed by custom_id.

async def _anthropic_submit(client, settings, items) -> dict:
    resp = await client.post(
        ANTHROPIC_BATCHES_URL,
        headers=anthropic_headers(settings),
        json={
            "requests": [
                {"custom_id": cid, "params": BUILDERS["anthropic"](settings, prompt)}
                for cid, prompt in items
            ]
        },
    )
    _check(resp, "anthropic")
    return {"batch_id": resp.json()["id"]}


async def _anthropic_poll(client, settings, ref) -> str:
    resp = await client.get(
        f"{ANTHROPIC_BATCHES_URL}/{ref['batch_id']}", headers=anthropic_headers(settings)
    )
    _check(resp, "anthropic")
    return "done" if resp.json().get("processing_status") == "ended" else "pending"


async def _anthropic_fetch(client, settings, ref) -> dict:
    headers = anthropic_headers(settings)
    resp = await client.get(f"{ANTHROPIC_BATCHES_URL}/{ref['batch_id']}", headers=headers)
    _check(resp, "anthropic")
    results_url = resp.json().get("results_url")
    if not results_url:
        raise BatchError("anthropic batch: no results_url on ended batch")
    resp = await client.get(results_url, headers=headers)
    _check(resp, "anthropic")
    results: dict[str, dict] = {}
    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        cid = row.get("custom_id")
        result = row.get("result") or {}
        if not cid:
            continue
        if result.get("type") == "succeeded":
            results[cid] = _parse_or_err("anthropic", settings, result.get("message") or {})
        else:
            results[cid] = _err(
                f"anthropic batch: request {result.get('type', 'unknown')}",
                result.get("error"),
            )
    return results


async def _anthropic_cancel(client, settings, ref) -> None:
    await client.post(
        f"{ANTHROPIC_BATCHES_URL}/{ref['batch_id']}/cancel",
        headers=anthropic_headers(settings),
    )


# ================================================================ Gemini
# models/{model}:batchGenerateContent with inline requests; the operation is
# polled at /v1beta/batches/{id}; inline results carry the metadata key back.

def _gemini_state(data: dict) -> str:
    return (
        (data.get("metadata") or {}).get("state")
        or data.get("state")
        or ""
    )


async def _gemini_submit(client, settings, items) -> dict:
    body = {
        "batch": {
            "display_name": "veille-ia",
            "input_config": {
                "requests": {
                    "requests": [
                        {
                            "request": BUILDERS["gemini"](settings, prompt),
                            "metadata": {"key": cid},
                        }
                        for cid, prompt in items
                    ]
                }
            },
        }
    }
    resp = await client.post(
        f"{GEMINI_BASE}/models/{settings['gemini_model']}:batchGenerateContent",
        headers=gemini_headers(settings),
        json=body,
    )
    _check(resp, "gemini")
    name = resp.json().get("name")
    if not name:
        raise BatchError("gemini batch: no operation name in response", detail=resp.text[:2000])
    # Keep submission order for result mapping in case metadata keys are absent.
    return {"name": name, "order": [cid for cid, _ in items]}


async def _gemini_poll(client, settings, ref) -> str:
    resp = await client.get(f"{GEMINI_BASE}/{ref['name']}", headers=gemini_headers(settings))
    _check(resp, "gemini")
    data = resp.json()
    state = _gemini_state(data)
    if state.endswith("SUCCEEDED"):
        return "done"
    if state.endswith(("FAILED", "CANCELLED", "EXPIRED")):
        return "failed"
    if data.get("done") and data.get("error"):
        return "failed"
    if data.get("done"):
        return "done"
    return "pending"


def _gemini_inline_items(data: dict) -> list[dict]:
    container = (data.get("response") or {}).get("inlinedResponses")
    if isinstance(container, dict):  # sometimes nested one level deeper
        container = container.get("inlinedResponses") or []
    return container if isinstance(container, list) else []


async def _gemini_fetch(client, settings, ref) -> dict:
    resp = await client.get(f"{GEMINI_BASE}/{ref['name']}", headers=gemini_headers(settings))
    _check(resp, "gemini")
    order = ref.get("order") or []
    results: dict[str, dict] = {}
    for index, item in enumerate(_gemini_inline_items(resp.json())):
        cid = (item.get("metadata") or {}).get("key")
        if not cid and index < len(order):
            cid = order[index]
        if not cid:
            continue
        if item.get("error"):
            results[cid] = _err("gemini batch: request failed", item["error"])
        else:
            results[cid] = _parse_or_err("gemini", settings, item.get("response") or {})
    return results


async def _gemini_cancel(client, settings, ref) -> None:
    await client.post(f"{GEMINI_BASE}/{ref['name']}:cancel", headers=gemini_headers(settings))


# ================================================================ xAI Grok
# Native batch API: create a batch, append requests (Responses API bodies),
# poll the pending counter, page through results keyed by batch_request_id.

XAI_ADD_CHUNK = 25  # requests per add-requests call


async def _xai_submit(client, settings, items) -> dict:
    headers = xai_headers(settings)
    resp = await client.post(f"{XAI_BASE}/batches", headers=headers, json={"name": "veille-ia"})
    _check(resp, "xai")
    data = resp.json()
    batch_id = data.get("batch_id") or data.get("id")
    if not batch_id:
        raise BatchError("xai batch: no batch_id in response", detail=resp.text[:2000])
    for start in range(0, len(items), XAI_ADD_CHUNK):
        chunk = items[start : start + XAI_ADD_CHUNK]
        resp = await client.post(
            f"{XAI_BASE}/batches/{batch_id}/requests",
            headers=headers,
            json={
                "batch_requests": [
                    {
                        "batch_request_id": cid,
                        "batch_request": {"responses": BUILDERS["xai"](settings, prompt)},
                    }
                    for cid, prompt in chunk
                ]
            },
        )
        _check(resp, "xai")
    return {"batch_id": batch_id}


async def _xai_poll(client, settings, ref) -> str:
    resp = await client.get(
        f"{XAI_BASE}/batches/{ref['batch_id']}", headers=xai_headers(settings)
    )
    _check(resp, "xai")
    state = resp.json().get("state") or {}
    # proto3 JSON omits zero-valued fields: a missing counter means 0.
    if int(state.get("num_requests", 0)) > 0 and int(state.get("num_pending", 0)) == 0:
        return "done"
    return "pending"


def _xai_unwrap_response(resp: dict) -> dict:
    """Results nest the Responses body under a per-endpoint wrapper key."""
    if "output" in resp:
        return resp
    for value in resp.values():
        if isinstance(value, dict) and ("output" in value or "choices" in value):
            return value
    return resp


async def _xai_fetch(client, settings, ref) -> dict:
    headers = xai_headers(settings)
    results: dict[str, dict] = {}
    token = ""
    for _ in range(200):  # hard page cap
        params = {"limit": 100}
        if token:
            params["pagination_token"] = token
        resp = await client.get(
            f"{XAI_BASE}/batches/{ref['batch_id']}/results", headers=headers, params=params
        )
        _check(resp, "xai")
        data = resp.json()
        page = data.get("results") or []
        for item in page:
            cid = item.get("batch_request_id")
            if not cid:
                continue
            if item.get("error_message"):
                results[cid] = _err(f"xai batch: {item['error_message']}")
                continue
            body = _xai_unwrap_response((item.get("batch_result") or {}).get("response") or {})
            if "output" in body:
                results[cid] = _parse_or_err("xai", settings, body)
            elif "choices" in body:  # chat-completions shape, defensive
                parsed = _parse_or_err("openai", settings, body)
                if parsed.get("status") == "ok":
                    parsed["model"] = settings["xai_model"]
                results[cid] = parsed
            else:
                results[cid] = _err("xai batch: unrecognized result shape", item)
        token = data.get("pagination_token") or ""
        if not token or not page:
            break
    return results


async def _xai_cancel(client, settings, ref) -> None:
    await client.post(
        f"{XAI_BASE}/batches/{ref['batch_id']}:cancel", headers=xai_headers(settings)
    )


# ================================================================ dispatch

_ADAPTERS = {
    "openai": (_openai_submit, _openai_poll, _openai_fetch, _openai_cancel),
    "anthropic": (_anthropic_submit, _anthropic_poll, _anthropic_fetch, _anthropic_cancel),
    "gemini": (_gemini_submit, _gemini_poll, _gemini_fetch, _gemini_cancel),
    "xai": (_xai_submit, _xai_poll, _xai_fetch, _xai_cancel),
}


async def submit(provider: str, client: httpx.AsyncClient, settings: dict,
                 items: list[tuple[str, str]]) -> dict:
    return await _ADAPTERS[provider][0](client, settings, items)


async def poll(provider: str, client: httpx.AsyncClient, settings: dict, ref: dict) -> str:
    return await _ADAPTERS[provider][1](client, settings, ref)


async def fetch(provider: str, client: httpx.AsyncClient, settings: dict, ref: dict) -> dict:
    return await _ADAPTERS[provider][2](client, settings, ref)


async def cancel(provider: str, client: httpx.AsyncClient, settings: dict, ref: dict) -> None:
    try:
        await _ADAPTERS[provider][3](client, settings, ref)
    except Exception:
        pass  # best effort — the provider expires unclaimed batches on its own
