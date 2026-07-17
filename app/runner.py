"""Run orchestration: hybrid direct/batch execution of prompts x models.

A "run" executes every prompt of a campaign against every enabled provider.
Two execution paths coexist:

- Direct (live) path — prompts with a per-prompt proxy (colonne LOC), or all
  prompts when batch mode is off. Requests go out concurrently (bounded per
  provider by a semaphore), each with retries and a per-request timeout, and
  are routed through the prompt's proxy so web searches stay geolocated.

- Batch path — prompts without a proxy, when batch mode is on. They are
  grouped into one Batch API job per provider (50% cheaper on most providers)
  and polled until completion. Batch jobs are persisted in the `batches`
  table so an interrupted run resumes after a server restart. If a batch
  cannot be submitted or fails, its prompts automatically fall back to the
  direct path, so a run never silently loses work.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
import traceback

import httpx

from . import batch, db
from .providers import (
    PROVIDER_KEY_SETTING,
    PROVIDERS,
    VERTEX_REDIRECT_HOST,
    ProviderError,
    to_domain,
)

# run_id -> asyncio.Task, used for status checks and cancellation
RUNNING: dict[int, asyncio.Task] = {}

BATCH_MAX_WAIT = 26 * 3600  # seconds before a still-pending batch is abandoned


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


class _Progress:
    """ok/err counters mirrored to the runs row after every completed task."""

    def __init__(self, run_id: int, ok: int = 0, err: int = 0):
        self.run_id = run_id
        self.ok = ok
        self.err = err

    def bump(self, success: bool, n: int = 1) -> None:
        if success:
            self.ok += n
        else:
            self.err += n
        with db.connect() as conn:
            conn.execute(
                "UPDATE runs SET ok_tasks=?, err_tasks=? WHERE id=?",
                (self.ok, self.err, self.run_id),
            )


async def _finalize_result(
    *,
    run_id: int,
    campaign_id: int,
    prompt_info: dict,
    result: dict,
    resolver_client: httpx.AsyncClient,
    resolve_timeout: float,
) -> None:
    """Resolve Gemini redirect URLs and insert one result row per cited source."""
    urls = result["urls"]
    resolved_map: dict[str, str] = {}
    to_resolve = [u for u in urls if VERTEX_REDIRECT_HOST in u]
    if to_resolve:
        outcomes = await asyncio.gather(
            *(resolve_redirect(resolver_client, u, resolve_timeout) for u in to_resolve),
            return_exceptions=True,
        )
        for original, final in zip(to_resolve, outcomes):
            resolved_map[original] = original if isinstance(final, Exception) else final

    base = {
        "run_id": run_id,
        "campaign_id": campaign_id,
        "date": _now_iso(),
        "modele": result["model"],
        "prompt": prompt_info["prompt"],
        "prompt_categorie": prompt_info.get("categorie"),
        "langue": prompt_info.get("langue"),
        "reponse": result["answer"],
    }
    rows = [
        {
            **base,
            "url": resolved_map.get(u, u),
            "url_originale": u,
            "domaine": to_domain(resolved_map.get(u, u)),
        }
        for u in urls
    ] or [{**base, "url": "", "url_originale": "", "domaine": ""}]
    db.insert_results(rows)


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
    """Execute one prompt on one provider (live path). Returns True on success."""
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

    await _finalize_result(
        run_id=run_id,
        campaign_id=campaign_id,
        prompt_info=prompt_row,
        result=result,
        resolver_client=resolver_client,
        resolve_timeout=float(settings.get("resolve_timeout", "8")),
    )
    return True


# ---------------------------------------------------------------- batch path

def _batch_payload(prompts: list[dict]) -> dict[str, dict]:
    """custom_id -> prompt info, persisted so a restart can still map results."""
    return {
        f"p{p['id']}": {
            "prompt": p["prompt"],
            "categorie": p.get("categorie"),
            "langue": p.get("langue"),
        }
        for p in prompts
    }


async def _process_batch_results(
    *,
    batch_row: dict,
    results: dict,
    settings: dict,
    resolver_client: httpx.AsyncClient,
    progress: _Progress,
) -> None:
    provider = batch_row["provider"]
    run_id, campaign_id = batch_row["run_id"], batch_row["campaign_id"]
    resolve_timeout = float(settings.get("resolve_timeout", "8"))
    for cid, info in batch_row["payload"].items():
        res = results.get(cid)
        if res is None:
            db.log_event(campaign_id, run_id, "error", provider,
                         f"batch: résultat manquant — prompt: {info['prompt'][:120]}")
            progress.bump(False)
        elif res.get("status") == "ok":
            await _finalize_result(
                run_id=run_id, campaign_id=campaign_id, prompt_info=info,
                result=res, resolver_client=resolver_client,
                resolve_timeout=resolve_timeout,
            )
            progress.bump(True)
        else:
            db.log_event(campaign_id, run_id, "error", provider,
                         f"batch: {res.get('message')} — prompt: {info['prompt'][:120]}",
                         res.get("detail"))
            progress.bump(False)


async def _await_batches(
    *,
    settings: dict,
    batch_client: httpx.AsyncClient,
    resolver_client: httpx.AsyncClient,
    pending: list[dict],
    progress: _Progress,
    on_batch_failed=None,
) -> None:
    """Poll pending batches until all are done/failed. Mutates `pending`.

    on_batch_failed(batch_row), when provided, re-runs the batch's prompts on
    the live path; otherwise every prompt of a failed batch counts as an error.
    """
    poll_interval = max(15, int(settings.get("batch_poll_interval", "60") or 60))
    deadline = time.monotonic() + BATCH_MAX_WAIT

    async def fail(batch_row: dict, reason: str, detail=None) -> None:
        db.set_batch_status(batch_row["id"], "failed")
        db.log_event(batch_row["campaign_id"], batch_row["run_id"], "error",
                     batch_row["provider"], f"batch: {reason}", detail)
        if on_batch_failed is not None:
            await on_batch_failed(batch_row)
        else:
            progress.bump(False, n=len(batch_row["payload"]))

    while pending:
        await asyncio.sleep(poll_interval)
        for batch_row in list(pending):
            provider = batch_row["provider"]
            try:
                state = await batch.poll(provider, batch_client, settings, batch_row["ref"])
            except Exception as exc:
                db.log_event(batch_row["campaign_id"], batch_row["run_id"], "warning",
                             provider, f"batch: statut illisible, nouvel essai — {exc!r}")
                continue
            if state == "pending":
                if time.monotonic() > deadline:
                    pending.remove(batch_row)
                    await batch.cancel(provider, batch_client, settings, batch_row["ref"])
                    await fail(batch_row, "délai maximum dépassé (26h), batch abandonné")
                continue
            pending.remove(batch_row)
            if state == "done":
                try:
                    results = await batch.fetch(provider, batch_client, settings,
                                                batch_row["ref"])
                except Exception as exc:
                    await fail(batch_row, f"récupération des résultats impossible — {exc!r}",
                               traceback.format_exc())
                    continue
                await _process_batch_results(
                    batch_row=batch_row, results=results, settings=settings,
                    resolver_client=resolver_client, progress=progress,
                )
                db.set_batch_status(batch_row["id"], "done")
                db.log_event(batch_row["campaign_id"], batch_row["run_id"], "info", provider,
                             f"Batch terminé: {len(batch_row['payload'])} prompts")
            else:  # failed
                await fail(batch_row, "le fournisseur a signalé l'échec du batch")


async def _cancel_batches(settings: dict, batch_client: httpx.AsyncClient,
                          pending: list[dict]) -> None:
    for batch_row in pending:
        await batch.cancel(batch_row["provider"], batch_client, settings, batch_row["ref"])
        db.set_batch_status(batch_row["id"], "cancelled")


def _batch_http_client() -> httpx.AsyncClient:
    # Generous read timeout: result files (JSONL) can be large.
    return httpx.AsyncClient(timeout=httpx.Timeout(300, connect=20))


# ---------------------------------------------------------------- run driver

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

    batch_mode = settings.get("batch_mode", "on") == "on"
    direct_prompts = [p for p in prompts if (p.get("proxy") or "").strip() or not batch_mode]
    batchable = [p for p in prompts if p not in direct_prompts]

    concurrency = max(1, int(settings.get("concurrency", "4")))
    timeout = float(settings.get("request_timeout", "180"))
    pool = ClientPool(timeout)
    batch_client = _batch_http_client()
    resolver_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(float(settings.get("resolve_timeout", "8")), connect=5),
        headers={"User-Agent": "Mozilla/5.0 (compatible; VeilleIA/1.0)"},
    )
    semaphores = {m: asyncio.Semaphore(concurrency) for m in models}
    progress = _Progress(run_id)
    pending_batches: list[dict] = []

    db.log_event(
        campaign_id, run_id, "info", "runner",
        f"Run started: {len(prompts)} prompts x {len(models)} models = {total} tasks "
        f"({len(batchable)} prompts via batch, {len(direct_prompts)} en direct)",
    )

    def direct_task(provider: str, prompt_row: dict):
        return _run_task(
            run_id=run_id, campaign_id=campaign_id, provider=provider,
            prompt_row=prompt_row, settings=settings, pool=pool,
            sem=semaphores[provider], resolver_client=resolver_client,
        )

    async def rerun_direct(provider: str, prompt_infos: list[dict]) -> None:
        tasks = [direct_task(provider, info) for info in prompt_infos]
        for coro in asyncio.as_completed(tasks):
            progress.bump(await coro)

    async def on_batch_failed(batch_row: dict) -> None:
        db.log_event(batch_row["campaign_id"], batch_row["run_id"], "warning",
                     batch_row["provider"],
                     "batch en échec — bascule des prompts sur le mode direct")
        await rerun_direct(batch_row["provider"], list(batch_row["payload"].values()))

    status = "done"
    try:
        direct_tasks = [direct_task(m, p) for p in direct_prompts for m in models]

        # Submit one batch per provider for the proxy-less prompts.
        if batchable:
            payload = _batch_payload(batchable)
            items = [(cid, info["prompt"]) for cid, info in payload.items()]
            for m in models:
                try:
                    ref = await batch.submit(m, batch_client, settings, items)
                except Exception as exc:
                    detail = getattr(exc, "detail", None) or traceback.format_exc()
                    db.log_event(campaign_id, run_id, "warning", m,
                                 f"soumission du batch impossible ({exc}) — "
                                 "bascule sur le mode direct", detail)
                    direct_tasks += [direct_task(m, p) for p in batchable]
                    continue
                batch_id = db.create_batch(run_id, campaign_id, m, ref, payload)
                pending_batches.append({
                    "id": batch_id, "run_id": run_id, "campaign_id": campaign_id,
                    "provider": m, "ref": ref, "payload": payload,
                })
                db.log_event(campaign_id, run_id, "info", m,
                             f"Batch soumis: {len(items)} prompts ({ref})")

        for coro in asyncio.as_completed(direct_tasks):
            progress.bump(await coro)

        await _await_batches(
            settings=settings, batch_client=batch_client,
            resolver_client=resolver_client, pending=pending_batches,
            progress=progress, on_batch_failed=on_batch_failed,
        )
    except asyncio.CancelledError:
        status = "cancelled"
        db.log_event(campaign_id, run_id, "warning", "runner", "Run cancelled by user")
        try:
            await _cancel_batches(settings, batch_client, pending_batches)
        except Exception:
            pass
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
                (status if status != "running" else "done",
                 progress.ok, progress.err, run_id),
            )
        await pool.close()
        await batch_client.aclose()
        await resolver_client.aclose()
        RUNNING.pop(run_id, None)
        db.log_event(campaign_id, run_id, "info", "runner",
                     f"Run finished ({status}): {progress.ok} ok, {progress.err} errors")


# ---------------------------------------------------------------- restart resume

async def _resume_run(run_row: dict, pending: list[dict]) -> None:
    """Finish a run whose batches were still pending when the server stopped."""
    run_id, campaign_id = run_row["id"], run_row["campaign_id"]
    settings = db.get_settings()
    batch_client = _batch_http_client()
    resolver_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(float(settings.get("resolve_timeout", "8")), connect=5),
        headers={"User-Agent": "Mozilla/5.0 (compatible; VeilleIA/1.0)"},
    )
    progress = _Progress(run_id, ok=run_row["ok_tasks"] or 0, err=run_row["err_tasks"] or 0)
    db.log_event(campaign_id, run_id, "info", "runner",
                 f"Reprise après redémarrage: {len(pending)} batch(s) en attente")

    status = "done"
    try:
        # No proxy/live context on resume: failed batches count as errors.
        await _await_batches(
            settings=settings, batch_client=batch_client,
            resolver_client=resolver_client, pending=pending,
            progress=progress, on_batch_failed=None,
        )
        # Direct-path tasks interrupted by the restart are unrecoverable.
        lost = (run_row["total_tasks"] or 0) - progress.ok - progress.err
        if lost > 0:
            db.log_event(campaign_id, run_id, "warning", "runner",
                         f"{lost} tâche(s) directe(s) perdue(s) au redémarrage")
            progress.bump(False, n=lost)
    except asyncio.CancelledError:
        status = "cancelled"
        db.log_event(campaign_id, run_id, "warning", "runner", "Run cancelled by user")
        try:
            await _cancel_batches(settings, batch_client, pending)
        except Exception:
            pass
        raise
    except Exception as exc:
        status = "failed"
        db.log_event(campaign_id, run_id, "error", "runner",
                     f"Resume crashed: {exc!r}", traceback.format_exc())
    finally:
        with db.connect() as conn:
            conn.execute(
                "UPDATE runs SET status=?, ok_tasks=?, err_tasks=?, "
                "finished_at=datetime('now') WHERE id=?",
                (status, progress.ok, progress.err, run_id),
            )
        await batch_client.aclose()
        await resolver_client.aclose()
        RUNNING.pop(run_id, None)
        db.log_event(campaign_id, run_id, "info", "runner",
                     f"Run finished ({status}): {progress.ok} ok, {progress.err} errors")


def resume_interrupted_runs() -> None:
    """Called at startup: reattach to runs left 'running' by a previous process.

    Runs with pending batches are resumed (the provider kept working while we
    were down); runs without are marked failed.
    """
    with db.connect() as conn:
        runs = db.rows_to_dicts(
            conn.execute("SELECT * FROM runs WHERE status='running'").fetchall()
        )
    for run_row in runs:
        pending = db.pending_batches(run_row["id"])
        if pending:
            task = asyncio.get_running_loop().create_task(_resume_run(run_row, pending))
            RUNNING[run_row["id"]] = task
        else:
            with db.connect() as conn:
                conn.execute(
                    "UPDATE runs SET status='failed', finished_at=datetime('now') "
                    "WHERE id=?", (run_row["id"],),
                )
            db.log_event(run_row["campaign_id"], run_row["id"], "error", "runner",
                         "Run interrompu par un redémarrage du serveur")


# ---------------------------------------------------------------- public API

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
