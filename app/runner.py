"""Run orchestration: fans out prompts x models with real concurrency.

A "run" executes every prompt of a campaign against every enabled provider.
Tasks run concurrently (bounded per provider by a semaphore), each with
retries and per-request timeout, so a full campaign takes minutes instead of
n8n's serial hours — and a single failing prompt never kills the run.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import traceback

import httpx

from . import db
from .providers import (
    PROVIDER_KEY_SETTING,
    PROVIDERS,
    VERTEX_REDIRECT_HOST,
    ProviderError,
    to_domain,
)

# run_id -> asyncio.Task, used for status checks and cancellation
RUNNING: dict[int, asyncio.Task] = {}


class ClientPool:
    """One AsyncClient per proxy URL (httpx binds the proxy at client creation)."""

    def __init__(self, timeout: float):
        self._timeout = timeout
        self._clients: dict[str | None, httpx.AsyncClient] = {}

    def get(self, proxy: str | None) -> httpx.AsyncClient:
        proxy = (proxy or "").strip() or None
        if proxy not in self._clients:
            self._clients[proxy] = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=20),
                proxy=proxy,
                follow_redirects=False,
            )
        return self._clients[proxy]

    async def close(self) -> None:
        for client in self._clients.values():
            await client.aclose()


async def resolve_redirect(client: httpx.AsyncClient, url: str, timeout: float) -> str:
    """Follow redirects to the final URL (Gemini returns vertexaisearch links)."""
    try:
        resp = await client.get(
            url, follow_redirects=True, timeout=httpx.Timeout(timeout, connect=5)
        )
        return str(resp.url)
    except Exception:
        # HEAD/GET failed: try to read the Location header of the first hop.
        try:
            resp = await client.get(
                url, follow_redirects=False, timeout=httpx.Timeout(timeout, connect=5)
            )
            loc = resp.headers.get("location")
            if loc:
                return loc
        except Exception:
            pass
        return url


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


async def _run_task(
    *,
    run_id: int,
    campaign_id: int,
    provider: str,
    prompt_row: dict,
    settings: dict,
    pool: ClientPool,
    sem: asyncio.Semaphore,
    resolver_client: httpx.AsyncClient,
) -> bool:
    """Execute one prompt on one provider. Returns True on success."""
    prompt_text = prompt_row["prompt"]
    max_retries = int(settings.get("max_retries", "2"))
    call = PROVIDERS[provider]
    client = pool.get(prompt_row.get("proxy"))

    async with sem:
        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                result = await call(client, settings, prompt_text)
                break
            except ProviderError as exc:
                last_error = exc
                if not exc.retryable or attempt == max_retries:
                    db.log_event(
                        campaign_id, run_id, "error", provider,
                        f"{exc} — prompt: {prompt_text[:120]}", exc.detail,
                    )
                    return False
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt == max_retries:
                    db.log_event(
                        campaign_id, run_id, "error", provider,
                        f"network error: {exc!r} — prompt: {prompt_text[:120]}",
                    )
                    return False
            except Exception as exc:  # unexpected parse errors etc.
                db.log_event(
                    campaign_id, run_id, "error", provider,
                    f"unexpected error: {exc!r} — prompt: {prompt_text[:120]}",
                    traceback.format_exc(),
                )
                return False
            await asyncio.sleep(5 * (attempt + 1))
        else:  # pragma: no cover - loop always breaks or returns
            db.log_event(campaign_id, run_id, "error", provider, repr(last_error))
            return False

    # Resolve Gemini's vertexaisearch redirect URLs to the real source URLs.
    urls = result["urls"]
    resolve_timeout = float(settings.get("resolve_timeout", "8"))
    resolved: list[tuple[str, str]] = []  # (final_url, original_url)
    to_resolve = [u for u in urls if VERTEX_REDIRECT_HOST in u]
    resolved_map: dict[str, str] = {}
    if to_resolve:
        outcomes = await asyncio.gather(
            *(resolve_redirect(resolver_client, u, resolve_timeout) for u in to_resolve),
            return_exceptions=True,
        )
        for original, final in zip(to_resolve, outcomes):
            resolved_map[original] = original if isinstance(final, Exception) else final
    for u in urls:
        resolved.append((resolved_map.get(u, u), u))

    date = _now_iso()
    base = {
        "run_id": run_id,
        "campaign_id": campaign_id,
        "date": date,
        "modele": result["model"],
        "prompt": prompt_text,
        "prompt_categorie": prompt_row.get("categorie"),
        "langue": prompt_row.get("langue"),
        "reponse": result["answer"],
    }
    rows = [
        {**base, "url": final, "url_originale": original, "domaine": to_domain(final)}
        for final, original in resolved
    ] or [{**base, "url": "", "url_originale": "", "domaine": ""}]
    db.insert_results(rows)
    return True


async def execute_run(run_id: int, campaign_id: int) -> None:
    settings = db.get_settings()
    with db.connect() as conn:
        campaign = conn.execute(
            "SELECT * FROM campaigns WHERE id=?", (campaign_id,)
        ).fetchone()
        prompts = db.rows_to_dicts(
            conn.execute(
                "SELECT * FROM prompts WHERE campaign_id=?", (campaign_id,)
            ).fetchall()
        )
    if campaign is None:
        return

    import json

    models = [m for m in json.loads(campaign["models"]) if m in PROVIDERS]
    missing = [m for m in models if not settings.get(PROVIDER_KEY_SETTING[m], "").strip()]
    for m in missing:
        db.log_event(campaign_id, run_id, "error", m, "API key not configured — provider skipped")
    models = [m for m in models if m not in missing]

    total = len(prompts) * len(models)
    with db.connect() as conn:
        conn.execute("UPDATE runs SET total_tasks=? WHERE id=?", (total, run_id))

    if total == 0:
        with db.connect() as conn:
            conn.execute(
                "UPDATE runs SET status='failed', finished_at=datetime('now') WHERE id=?",
                (run_id,),
            )
        db.log_event(campaign_id, run_id, "error", "runner",
                     "Nothing to run (no prompts, or no provider with a configured key)")
        return

    concurrency = max(1, int(settings.get("concurrency", "4")))
    timeout = float(settings.get("request_timeout", "180"))
    pool = ClientPool(timeout)
    resolver_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(float(settings.get("resolve_timeout", "8")), connect=5),
        headers={"User-Agent": "Mozilla/5.0 (compatible; VeilleIA/1.0)"},
    )
    semaphores = {m: asyncio.Semaphore(concurrency) for m in models}

    db.log_event(campaign_id, run_id, "info", "runner",
                 f"Run started: {len(prompts)} prompts x {len(models)} models = {total} tasks")

    ok = err = 0
    try:
        tasks = [
            _run_task(
                run_id=run_id,
                campaign_id=campaign_id,
                provider=model,
                prompt_row=prompt,
                settings=settings,
                pool=pool,
                sem=semaphores[model],
                resolver_client=resolver_client,
            )
            for prompt in prompts
            for model in models
        ]
        for coro in asyncio.as_completed(tasks):
            success = await coro
            ok += 1 if success else 0
            err += 0 if success else 1
            with db.connect() as conn:
                conn.execute(
                    "UPDATE runs SET ok_tasks=?, err_tasks=? WHERE id=?",
                    (ok, err, run_id),
                )
        status = "done"
    except asyncio.CancelledError:
        status = "cancelled"
        db.log_event(campaign_id, run_id, "warning", "runner", "Run cancelled by user")
        raise
    except Exception as exc:
        status = "failed"
        db.log_event(campaign_id, run_id, "error", "runner",
                     f"Run crashed: {exc!r}", traceback.format_exc())
    finally:
        with db.connect() as conn:
            conn.execute(
                "UPDATE runs SET status=?, ok_tasks=?, err_tasks=?, "
                "finished_at=datetime('now') WHERE id=?",
                (status if status != "running" else "done", ok, err, run_id),
            )
        await pool.close()
        await resolver_client.aclose()
        RUNNING.pop(run_id, None)
        db.log_event(campaign_id, run_id, "info", "runner",
                     f"Run finished ({status}): {ok} ok, {err} errors")


def start_run(campaign_id: int, trigger: str = "manual") -> int:
    """Create a run row and launch execution in the running event loop."""
    with db.connect() as conn:
        # Refuse to double-run a campaign.
        active = conn.execute(
            "SELECT id FROM runs WHERE campaign_id=? AND status='running'",
            (campaign_id,),
        ).fetchone()
        if active and active["id"] in RUNNING:
            raise RuntimeError(f"campaign {campaign_id} already has a running run")
        cursor = conn.execute(
            "INSERT INTO runs(campaign_id, trigger) VALUES (?, ?)",
            (campaign_id, trigger),
        )
        run_id = cursor.lastrowid

    # Must be called from the event loop thread (async endpoint or scheduler job).
    task = asyncio.get_running_loop().create_task(execute_run(run_id, campaign_id))
    RUNNING[run_id] = task
    return run_id


def cancel_run(run_id: int) -> bool:
    task = RUNNING.get(run_id)
    if task is None:
        return False
    task.cancel()
    return True
