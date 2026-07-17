"""Provider clients for the four AI APIs used by the veille workflow.

Each provider is split into three parts so the live path and the Batch API
path (batch.py) share the exact same request/response logic:

    build_*_body(settings, prompt) -> dict   request body (same shape live/batch)
    parse_*_data(settings, data)   -> {"model", "answer", "urls"}
    call_*(client, settings, prompt)         live HTTP call = build + post + parse

They mirror the requests of the original n8n workflow (web search enabled on
every provider, one output row per cited source downstream).

Raw HTTP (httpx) is used for all four providers on purpose: the module is
provider-neutral and must route each request through an optional per-prompt
proxy, which is simplest with a shared httpx client pool.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx


class ProviderError(Exception):
    def __init__(self, message: str, detail=None, retryable: bool = False):
        super().__init__(message)
        self.detail = detail
        self.retryable = retryable


# ---------------------------------------------------------------- utilities

MD_LINK_RE = re.compile(r"\]\((https?://[^\s)]+)\)")
RAW_URL_RE = re.compile(r"https?://[^\s)\]\"'<>]+")


def to_domain(url: str) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        host = ""
    if not host:
        m = re.match(r"^https?://([^/?#]+)", url, re.I)
        host = m.group(1) if m else ""
    return re.sub(r"^www\.", "", host, flags=re.I)


def clean_openai_url(url: str) -> str:
    return (
        url.strip()
        .replace("?utm_source=openai", "")
        .replace("&utm_source=openai", "")
    )


def urls_from_text(text: str) -> list[str]:
    if not text:
        return []
    urls = MD_LINK_RE.findall(text)
    urls += RAW_URL_RE.findall(text)
    return urls


def dedupe(urls: list[str]) -> list[str]:
    seen, out = set(), []
    for u in urls:
        u = (u or "").strip().rstrip(".,;")
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _raise_for_status(resp: httpx.Response, provider: str) -> None:
    if resp.status_code < 400:
        return
    retryable = resp.status_code in (408, 409, 429) or resp.status_code >= 500
    body = resp.text[:4000]
    raise ProviderError(
        f"{provider}: HTTP {resp.status_code}",
        detail=body,
        retryable=retryable,
    )


# ---------------------------------------------------------------- OpenAI

OPENAI_URL = "https://api.openai.com/v1/chat/completions"


def openai_headers(settings: dict) -> dict:
    return {"Authorization": f"Bearer {settings['openai_api_key']}"}


def build_openai_body(settings: dict, prompt: str) -> dict:
    return {
        "model": settings["openai_model"],
        "messages": [
            {
                "role": "developer",
                "content": "You are a helpful assistant. Always look for sources online.",
            },
            {"role": "user", "content": prompt},
        ],
    }


def parse_openai_data(settings: dict, data: dict) -> dict:
    answer = ""
    urls: list[str] = []
    for choice in data.get("choices", []):
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    answer += part.get("text", "")
        elif isinstance(content, str):
            answer += content
        for ann in (message.get("annotations") or []) + (choice.get("annotations") or []):
            citation = ann.get("url_citation") or {}
            if citation.get("url"):
                urls.append(citation["url"])
            elif ann.get("url"):
                urls.append(ann["url"])
        # Some proxies put url_citation objects directly on the choice.
        citation = choice.get("url_citation") or {}
        if citation.get("url"):
            urls.append(citation["url"])

    if not answer:
        raise ProviderError("openai: empty answer", detail=data)

    urls += urls_from_text(answer)
    urls = [clean_openai_url(u) for u in urls]
    return {"model": settings["openai_model"], "answer": answer, "urls": dedupe(urls)}


async def call_openai(client: httpx.AsyncClient, settings: dict, prompt: str) -> dict:
    resp = await client.post(
        OPENAI_URL,
        headers=openai_headers(settings),
        json=build_openai_body(settings, prompt),
    )
    _raise_for_status(resp, "openai")
    return parse_openai_data(settings, resp.json())


# ---------------------------------------------------------------- Gemini

VERTEX_REDIRECT_HOST = "vertexaisearch.cloud.google.com"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Same preamble the n8n workflow prepended, to reproduce consumer-Gemini behavior.
GEMINI_PREAMBLE = (
    "You are Gemini, a helpful AI assistant built by Google. Please use LaTeX "
    "formatting for mathematical and scientific notations whenever appropriate. "
    "Enclose all LaTeX using '$' or '$$' delimiters. NEVER generate LaTeX code "
    "in a latex block unless the user explicitly asks for it. DO NOT use LaTeX "
    "for regular prose (e.g., resumes, letters, essays, CVs, etc.). "
)


def gemini_headers(settings: dict) -> dict:
    return {"x-goog-api-key": settings["gemini_api_key"]}


def build_gemini_body(settings: dict, prompt: str) -> dict:
    return {
        "contents": [{"parts": [{"text": GEMINI_PREAMBLE + prompt}]}],
        "tools": [{"google_search": {}}],
    }


def parse_gemini_data(settings: dict, data: dict) -> dict:
    candidates = data.get("candidates") or []
    if not candidates:
        raise ProviderError("gemini: no candidates in response", detail=data)
    candidate = candidates[0]

    parts = (candidate.get("content") or {}).get("parts") or []
    answer = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    if not answer:
        raise ProviderError("gemini: empty answer", detail=data)

    urls = []
    chunks = (candidate.get("groundingMetadata") or {}).get("groundingChunks") or []
    for chunk in chunks:
        uri = (chunk.get("web") or {}).get("uri")
        if uri:
            urls.append(uri)

    return {"model": settings["gemini_model"], "answer": answer, "urls": dedupe(urls)}


async def call_gemini(client: httpx.AsyncClient, settings: dict, prompt: str) -> dict:
    model = settings["gemini_model"]
    resp = await client.post(
        f"{GEMINI_BASE}/models/{model}:generateContent",
        headers=gemini_headers(settings),
        json=build_gemini_body(settings, prompt),
    )
    _raise_for_status(resp, "gemini")
    return parse_gemini_data(settings, resp.json())


# ---------------------------------------------------------------- Anthropic

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def anthropic_headers(settings: dict) -> dict:
    return {
        "x-api-key": settings["anthropic_api_key"],
        "anthropic-version": "2023-06-01",
    }


def build_anthropic_body(settings: dict, prompt: str) -> dict:
    return {
        "model": settings["anthropic_model"],
        "max_tokens": 8192,
        "tools": [
            {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}
        ],
        "system": (
            "Do not output reasoning, thinking, analysis, or progress notes. "
            "Do not say that you are searching. Return only the final answer "
            "to the user, with citations when web search is used."
        ),
        "messages": [{"role": "user", "content": prompt}],
    }


def parse_anthropic_data(settings: dict, data: dict) -> dict:
    if data.get("stop_reason") == "refusal":
        raise ProviderError("anthropic: request refused by safety classifiers", detail=data)

    answer_parts: list[str] = []
    urls: list[str] = []
    for block in data.get("content") or []:
        btype = block.get("type")
        if btype == "text":
            answer_parts.append(block.get("text", ""))
        elif btype == "web_search_tool_result":
            content = block.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("url"):
                        urls.append(item["url"])

    answer = "".join(answer_parts)
    if not answer:
        raise ProviderError("anthropic: empty answer", detail=data)

    model = data.get("model", settings["anthropic_model"])
    return {"model": model, "answer": answer, "urls": dedupe(urls)}


async def call_anthropic(client: httpx.AsyncClient, settings: dict, prompt: str) -> dict:
    resp = await client.post(
        ANTHROPIC_URL,
        headers=anthropic_headers(settings),
        json=build_anthropic_body(settings, prompt),
    )
    _raise_for_status(resp, "anthropic")
    return parse_anthropic_data(settings, resp.json())


# ---------------------------------------------------------------- xAI Grok

XAI_BASE = "https://api.x.ai/v1"
XAI_URL = f"{XAI_BASE}/responses"


def xai_headers(settings: dict) -> dict:
    return {"Authorization": f"Bearer {settings['xai_api_key']}"}


def build_xai_body(settings: dict, prompt: str) -> dict:
    return {
        "model": settings["xai_model"],
        "input": [{"role": "user", "content": prompt}],
        "tools": [{"type": "web_search"}],
    }


def parse_xai_data(settings: dict, data: dict) -> dict:
    answer_parts: list[str] = []
    urls: list[str] = []
    for block in data.get("output") or []:
        if block.get("type") != "message" or block.get("role") != "assistant":
            continue
        for item in block.get("content") or []:
            if item.get("type") != "output_text":
                continue
            if isinstance(item.get("text"), str):
                answer_parts.append(item["text"])
            for ann in item.get("annotations") or []:
                if ann.get("type") == "url_citation" and ann.get("url"):
                    urls.append(ann["url"])

    answer = "\n\n".join(answer_parts)
    if not answer:
        raise ProviderError("xai: empty answer", detail=data)

    if not urls and isinstance(data.get("citations"), list):
        urls.extend(u for u in data["citations"] if isinstance(u, str))
    if not urls:
        urls = urls_from_text(answer)

    return {"model": settings["xai_model"], "answer": answer, "urls": dedupe(urls)}


async def call_xai(client: httpx.AsyncClient, settings: dict, prompt: str) -> dict:
    resp = await client.post(
        XAI_URL,
        headers=xai_headers(settings),
        json=build_xai_body(settings, prompt),
    )
    _raise_for_status(resp, "xai")
    return parse_xai_data(settings, resp.json())


# ---------------------------------------------------------------- registries

PROVIDERS = {
    "openai": call_openai,
    "gemini": call_gemini,
    "anthropic": call_anthropic,
    "xai": call_xai,
}

BUILDERS = {
    "openai": build_openai_body,
    "gemini": build_gemini_body,
    "anthropic": build_anthropic_body,
    "xai": build_xai_body,
}

PARSERS = {
    "openai": parse_openai_data,
    "gemini": parse_gemini_data,
    "anthropic": parse_anthropic_data,
    "xai": parse_xai_data,
}

PROVIDER_KEY_SETTING = {
    "openai": "openai_api_key",
    "gemini": "gemini_api_key",
    "anthropic": "anthropic_api_key",
    "xai": "xai_api_key",
}
