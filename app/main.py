"""Veille IA — FastAPI application (API + static frontend)."""
from __future__ import annotations

import base64
import csv
import datetime as dt
import io
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, runner, scheduler
from .providers import PROVIDERS, to_domain

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler.start()
    # Reattach to runs whose Batch API jobs were pending when the server stopped.
    runner.resume_interrupted_runs()
    yield
    scheduler.shutdown()


app = FastAPI(title="Veille IA", lifespan=lifespan)


# ================================================================ basic auth
# Everything requires HTTP Basic auth except the read-only visitor routes
# (/share/... + /api/share/...) and static assets (CSS/JS, not sensitive).

AUTH_EXEMPT_PREFIXES = ("/share/", "/api/share/", "/static/")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    path = request.url.path
    if path.startswith(AUTH_EXEMPT_PREFIXES):
        return await call_next(request)
    settings = db.get_settings()
    expected_user = settings.get("admin_user") or "admin"
    expected_password = settings.get("admin_password") or "admin"
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            if (secrets.compare_digest(user, expected_user)
                    and secrets.compare_digest(password, expected_password)):
                return await call_next(request)
        except Exception:
            pass
    return Response(
        status_code=401,
        content="Authentification requise",
        headers={"WWW-Authenticate": 'Basic realm="Veille IA", charset="UTF-8"'},
    )


# ================================================================ helpers

def _sniff_reader(raw: bytes) -> csv.DictReader:
    text = raw.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,")
        dialect.doublequote = True
    except csv.Error:
        dialect = csv.excel
    return csv.DictReader(io.StringIO(text), dialect=dialect)


def _norm_header(name: str) -> str:
    return (name or "").strip().lower().replace("é", "e").replace("è", "e")


def _pick(row: dict, *names: str) -> str:
    normalized = {_norm_header(k): (v or "").strip() for k, v in row.items() if k}
    for name in names:
        if normalized.get(name):
            return normalized[name]
    return ""


def _campaign_or_404(campaign_id: int):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Campagne introuvable")
    return row


# ================================================================ settings

class SettingsIn(BaseModel):
    values: dict[str, str]


@app.get("/api/settings")
def get_settings():
    settings = db.get_settings()
    out = {}
    for key, value in settings.items():
        if key == "admin_password":  # never hint the password itself
            out[key] = {"set": bool(value.strip()), "hint": ""}
        elif key in db.SECRET_KEYS:
            out[key] = {"set": bool(value.strip()), "hint": value[:8] + "…" if value else ""}
        else:
            out[key] = value
    out["admin_password_is_default"] = settings.get("admin_password") == "admin"
    return out


@app.post("/api/settings")
def save_settings(body: SettingsIn):
    allowed = set(db.DEFAULT_SETTINGS)
    values = {k: v for k, v in body.values.items() if k in allowed}
    # Empty secret = "keep current value" (the UI never echoes stored keys).
    values = {k: v for k, v in values.items()
              if not (k in db.SECRET_KEYS and v.strip() == "")}
    db.set_settings(values)
    return {"ok": True}


# ================================================================ category sets

class CategorySetIn(BaseModel):
    name: str


class MappingIn(BaseModel):
    domaine: str
    categorie: str


@app.get("/api/category_sets")
def list_category_sets():
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT cs.*, COUNT(dc.id) AS n_domains "
            "FROM category_sets cs LEFT JOIN domain_categories dc ON dc.set_id=cs.id "
            "GROUP BY cs.id ORDER BY cs.name"
        ).fetchall()
    return db.rows_to_dicts(rows)


@app.post("/api/category_sets")
def create_category_set(body: CategorySetIn):
    with db.connect() as conn:
        try:
            cursor = conn.execute("INSERT INTO category_sets(name) VALUES (?)", (body.name.strip(),))
        except Exception:
            raise HTTPException(400, "Un jeu de catégories avec ce nom existe déjà")
        return {"id": cursor.lastrowid}


@app.delete("/api/category_sets/{set_id}")
def delete_category_set(set_id: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM category_sets WHERE id=?", (set_id,))
    return {"ok": True}


@app.post("/api/category_sets/{set_id}/clone")
def clone_category_set(set_id: int, body: CategorySetIn):
    with db.connect() as conn:
        try:
            cursor = conn.execute("INSERT INTO category_sets(name) VALUES (?)", (body.name.strip(),))
        except Exception:
            raise HTTPException(400, "Un jeu de catégories avec ce nom existe déjà")
        new_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO domain_categories(set_id, domaine, categorie) "
            "SELECT ?, domaine, categorie FROM domain_categories WHERE set_id=?",
            (new_id, set_id),
        )
    return {"id": new_id}


@app.get("/api/category_sets/{set_id}/mappings")
def list_mappings(set_id: int):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM domain_categories WHERE set_id=? ORDER BY categorie, domaine",
            (set_id,),
        ).fetchall()
    return db.rows_to_dicts(rows)


@app.post("/api/category_sets/{set_id}/mappings")
def upsert_mapping(set_id: int, body: MappingIn):
    domaine = to_domain(body.domaine) or body.domaine.strip().lower().removeprefix("www.")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO domain_categories(set_id, domaine, categorie) VALUES (?, ?, ?) "
            "ON CONFLICT(set_id, domaine) DO UPDATE SET categorie=excluded.categorie",
            (set_id, domaine, body.categorie.strip()),
        )
    return {"ok": True}


@app.delete("/api/category_sets/{set_id}/mappings/{mapping_id}")
def delete_mapping(set_id: int, mapping_id: int):
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM domain_categories WHERE id=? AND set_id=?", (mapping_id, set_id)
        )
    return {"ok": True}


@app.post("/api/category_sets/{set_id}/upload")
async def upload_categories(set_id: int, file: UploadFile):
    reader = _sniff_reader(await file.read())
    count = 0
    with db.connect() as conn:
        for row in reader:
            categorie = _pick(row, "categorie")
            domaine = _pick(row, "domaine", "domain")
            if not categorie or not domaine:
                continue
            domaine = to_domain(domaine) or domaine.lower().removeprefix("www.")
            conn.execute(
                "INSERT INTO domain_categories(set_id, domaine, categorie) VALUES (?, ?, ?) "
                "ON CONFLICT(set_id, domaine) DO UPDATE SET categorie=excluded.categorie",
                (set_id, domaine, categorie),
            )
            count += 1
    return {"imported": count}


# ================================================================ campaigns

class CampaignIn(BaseModel):
    name: str
    models: list[str] = []
    schedule_time: str | None = None
    interval_days: int = 1
    start_date: str | None = None
    end_date: str | None = None
    category_set_id: int | None = None
    status: str | None = None


def _campaign_out(row) -> dict:
    campaign = dict(row)
    campaign["models"] = json.loads(campaign["models"])
    campaign["next_run"] = scheduler.next_run_time(campaign["id"])
    return campaign


@app.get("/api/campaigns")
def list_campaigns():
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT c.*, "
            " (SELECT COUNT(*) FROM prompts p WHERE p.campaign_id=c.id) AS n_prompts, "
            " (SELECT COUNT(*) FROM results r WHERE r.campaign_id=c.id) AS n_results, "
            " (SELECT MAX(started_at) FROM runs x WHERE x.campaign_id=c.id) AS last_run, "
            " (SELECT COUNT(*) FROM runs x WHERE x.campaign_id=c.id AND x.status='running') AS running "
            "FROM campaigns c ORDER BY c.created_at DESC"
        ).fetchall()
    return [_campaign_out(r) for r in rows]


@app.post("/api/campaigns")
def create_campaign(body: CampaignIn):
    invalid = [m for m in body.models if m not in PROVIDERS]
    if invalid:
        raise HTTPException(400, f"Modèles inconnus: {invalid}")
    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO campaigns(name, models, schedule_time, interval_days, "
            "start_date, end_date, category_set_id, share_token) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                body.name.strip(),
                json.dumps(body.models),
                body.schedule_time or None,
                max(1, body.interval_days),
                body.start_date or dt.date.today().isoformat(),
                body.end_date or None,
                body.category_set_id,
                secrets.token_urlsafe(16),
            ),
        )
        campaign_id = cursor.lastrowid
    scheduler.sync_campaign(campaign_id)
    return {"id": campaign_id}


@app.get("/api/campaigns/{campaign_id}")
def get_campaign(campaign_id: int):
    row = _campaign_or_404(campaign_id)
    campaign = _campaign_out(row)
    with db.connect() as conn:
        campaign["n_prompts"] = conn.execute(
            "SELECT COUNT(*) AS n FROM prompts WHERE campaign_id=?", (campaign_id,)
        ).fetchone()["n"]
        campaign["n_results"] = conn.execute(
            "SELECT COUNT(*) AS n FROM results WHERE campaign_id=?", (campaign_id,)
        ).fetchone()["n"]
    return campaign


@app.put("/api/campaigns/{campaign_id}")
def update_campaign(campaign_id: int, body: CampaignIn):
    _campaign_or_404(campaign_id)
    invalid = [m for m in body.models if m not in PROVIDERS]
    if invalid:
        raise HTTPException(400, f"Modèles inconnus: {invalid}")
    with db.connect() as conn:
        conn.execute(
            "UPDATE campaigns SET name=?, models=?, schedule_time=?, interval_days=?, "
            "start_date=?, end_date=?, category_set_id=?, status=COALESCE(?, status) "
            "WHERE id=?",
            (
                body.name.strip(),
                json.dumps(body.models),
                body.schedule_time or None,
                max(1, body.interval_days),
                body.start_date or None,
                body.end_date or None,
                body.category_set_id,
                body.status,
                campaign_id,
            ),
        )
    scheduler.sync_campaign(campaign_id)
    return {"ok": True}


@app.delete("/api/campaigns/{campaign_id}")
def delete_campaign(campaign_id: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))
    scheduler.sync_campaign(campaign_id)
    return {"ok": True}


@app.post("/api/campaigns/{campaign_id}/share/rotate")
def rotate_share_token(campaign_id: int):
    """Invalidate the current visitor link and mint a new one."""
    _campaign_or_404(campaign_id)
    token = secrets.token_urlsafe(16)
    with db.connect() as conn:
        conn.execute("UPDATE campaigns SET share_token=? WHERE id=?", (token, campaign_id))
    return {"share_token": token}


@app.post("/api/campaigns/{campaign_id}/status/{status}")
def set_campaign_status(campaign_id: int, status: str):
    if status not in ("active", "paused", "archived"):
        raise HTTPException(400, "Statut invalide")
    _campaign_or_404(campaign_id)
    with db.connect() as conn:
        conn.execute("UPDATE campaigns SET status=? WHERE id=?", (status, campaign_id))
    scheduler.sync_campaign(campaign_id)
    return {"ok": True}


# ------------------------------------------------ prompts

@app.get("/api/campaigns/{campaign_id}/prompts")
def list_prompts(campaign_id: int):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM prompts WHERE campaign_id=? ORDER BY id", (campaign_id,)
        ).fetchall()
    return db.rows_to_dicts(rows)


@app.post("/api/campaigns/{campaign_id}/prompts/upload")
async def upload_prompts(campaign_id: int, file: UploadFile, replace: bool = True):
    _campaign_or_404(campaign_id)
    reader = _sniff_reader(await file.read())
    rows = []
    for row in reader:
        prompt = _pick(row, "prompt", "prompts")
        if not prompt:
            continue
        rows.append(
            (
                campaign_id,
                _pick(row, "categorie") or None,
                prompt,
                _pick(row, "langue", "language") or None,
                _pick(row, "loc", "proxy") or None,
            )
        )
    if not rows:
        raise HTTPException(400, "Aucun prompt trouvé — le CSV doit avoir une colonne 'Prompt'")
    with db.connect() as conn:
        if replace:
            conn.execute("DELETE FROM prompts WHERE campaign_id=?", (campaign_id,))
        conn.executemany(
            "INSERT INTO prompts(campaign_id, categorie, prompt, langue, proxy) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
    return {"imported": len(rows)}


# ------------------------------------------------ runs

@app.post("/api/campaigns/{campaign_id}/run")
async def run_now(campaign_id: int):
    _campaign_or_404(campaign_id)
    try:
        run_id = runner.start_run(campaign_id, trigger="manual")
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return {"run_id": run_id}


@app.get("/api/campaigns/{campaign_id}/runs")
def list_runs(campaign_id: int):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs WHERE campaign_id=? ORDER BY id DESC LIMIT 100",
            (campaign_id,),
        ).fetchall()
    return db.rows_to_dicts(rows)


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: int):
    if not runner.cancel_run(run_id):
        raise HTTPException(404, "Run introuvable ou déjà terminé")
    return {"ok": True}


# ------------------------------------------------ events (error tracing)

@app.get("/api/campaigns/{campaign_id}/events")
def list_events(campaign_id: int, level: str | None = None, run_id: int | None = None,
                limit: int = 300):
    query = "SELECT * FROM events WHERE campaign_id=?"
    args: list = [campaign_id]
    if level:
        query += " AND level=?"
        args.append(level)
    if run_id:
        query += " AND run_id=?"
        args.append(run_id)
    query += " ORDER BY id DESC LIMIT ?"
    args.append(min(limit, 1000))
    with db.connect() as conn:
        rows = conn.execute(query, args).fetchall()
    return db.rows_to_dicts(rows)


# ------------------------------------------------ results + export

RESULT_FILTERS = """
    AND (:modele IS NULL OR r.modele = :modele)
    AND (:langue IS NULL OR r.langue = :langue)
    AND (:prompt IS NULL OR r.prompt = :prompt)
    AND (:run_id IS NULL OR r.run_id = :run_id)
    AND (:prompt_categorie IS NULL OR COALESCE(r.prompt_categorie, '') = :prompt_categorie)
"""


def _result_query(campaign_id: int, filters: dict) -> tuple[str, dict]:
    campaign = _campaign_or_404(campaign_id)
    params = {
        "campaign_id": campaign_id,
        "set_id": campaign["category_set_id"],
        "modele": filters.get("modele") or None,
        "langue": filters.get("langue") or None,
        "prompt": filters.get("prompt") or None,
        "run_id": filters.get("run_id") or None,
        "categorie": filters.get("categorie") or None,
        "prompt_categorie": filters.get("prompt_categorie") or None,
    }
    query = f"""
        SELECT r.*, COALESCE(dc.categorie, CASE WHEN r.domaine='' THEN '' ELSE 'Non catégorisé' END) AS categorie
        FROM results r
        LEFT JOIN domain_categories dc
               ON dc.set_id = :set_id AND dc.domaine = r.domaine
        WHERE r.campaign_id = :campaign_id
        {RESULT_FILTERS}
        AND (:categorie IS NULL
             OR COALESCE(dc.categorie, CASE WHEN r.domaine='' THEN '' ELSE 'Non catégorisé' END) = :categorie)
    """
    return query, params


@app.get("/api/campaigns/{campaign_id}/results")
def list_results(campaign_id: int, modele: str | None = None, langue: str | None = None,
                 prompt: str | None = None, run_id: int | None = None,
                 categorie: str | None = None, prompt_categorie: str | None = None,
                 offset: int = 0, limit: int = 50):
    query, params = _result_query(campaign_id, locals())
    with db.connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM ({query})", params
        ).fetchone()["n"]
        rows = conn.execute(
            query + " ORDER BY r.id DESC LIMIT :limit OFFSET :offset",
            {**params, "limit": min(limit, 500), "offset": max(0, offset)},
        ).fetchall()
    return {"total": total, "rows": db.rows_to_dicts(rows)}


def _csv_response(filename: str, rows_iter):
    """Stream an iterable of csv rows as a UTF-8-BOM CSV download."""
    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL)
        first = True
        for row in rows_iter:
            writer.writerow(row)
            prefix = "﻿" if first else ""  # BOM so Excel opens UTF-8 correctly
            first = False
            yield prefix + buffer.getvalue()
            buffer.seek(0); buffer.truncate(0)
    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/campaigns/{campaign_id}/export.csv")
def export_results(campaign_id: int, modele: str | None = None, langue: str | None = None,
                   prompt: str | None = None, run_id: int | None = None,
                   categorie: str | None = None, prompt_categorie: str | None = None):
    campaign = _campaign_or_404(campaign_id)
    query, params = _result_query(campaign_id, locals())

    def rows():
        yield ["Date", "Réponse", "Modèle", "Prompt", "Catégorie_Prompt", "Langue",
               "URL", "Domaine", "URL_Originale", "Catégorie"]
        with db.connect() as conn:
            cursor = conn.execute(query + " ORDER BY r.id", params)
            while True:
                batch = cursor.fetchmany(500)
                if not batch:
                    break
                for r in batch:
                    yield [r["date"], r["reponse"], r["modele"], r["prompt"],
                           r["prompt_categorie"], r["langue"], r["url"], r["domaine"],
                           r["url_originale"], r["categorie"]]

    return _csv_response(f"{campaign['name'].replace(' ', '_')}_resultats.csv", rows())


# ------------------------------------------------ dashboard aggregations

@app.get("/api/campaigns/{campaign_id}/dashboard")
def dashboard(campaign_id: int, modele: str | None = None, langue: str | None = None,
              prompt: str | None = None, run_id: int | None = None,
              categorie: str | None = None, prompt_categorie: str | None = None):
    query, params = _result_query(campaign_id, locals())
    base = f"SELECT * FROM ({query}) f WHERE f.url <> ''"
    with db.connect() as conn:
        def agg(select: str, group: str, limit: int = 500):
            rows = conn.execute(
                f"SELECT {select}, COUNT(*) AS n FROM ({base}) GROUP BY {group} "
                f"ORDER BY n DESC LIMIT {limit}",
                params,
            ).fetchall()
            return db.rows_to_dicts(rows)

        by_category = agg("categorie", "categorie")
        by_domain = agg("domaine", "domaine")
        by_url = agg("url, domaine", "url", 300)
        by_model = agg("modele", "modele")
        sunburst = db.rows_to_dicts(conn.execute(
            f"SELECT COALESCE(prompt_categorie, 'Sans catégorie') AS prompt_categorie, "
            f"categorie, domaine, url, COUNT(*) AS n FROM ({base}) "
            "GROUP BY prompt_categorie, categorie, domaine, url ORDER BY n DESC",
            params,
        ).fetchall())
        pivot = db.rows_to_dicts(conn.execute(
            f"SELECT categorie, prompt, COUNT(*) AS n FROM ({base}) "
            "GROUP BY categorie, prompt",
            params,
        ).fetchall())
        # Filter option lists (unfiltered, campaign-wide)
        options = {
            "modeles": [r["modele"] for r in conn.execute(
                "SELECT DISTINCT modele FROM results WHERE campaign_id=? ORDER BY modele",
                (campaign_id,)).fetchall()],
            "langues": [r["langue"] for r in conn.execute(
                "SELECT DISTINCT langue FROM results WHERE campaign_id=? AND langue IS NOT NULL "
                "ORDER BY langue", (campaign_id,)).fetchall()],
            "prompts": [r["prompt"] for r in conn.execute(
                "SELECT DISTINCT prompt FROM results WHERE campaign_id=? ORDER BY prompt",
                (campaign_id,)).fetchall()],
            "prompt_categories": [r["c"] for r in conn.execute(
                "SELECT DISTINCT COALESCE(prompt_categorie, '') AS c FROM results "
                "WHERE campaign_id=? ORDER BY c", (campaign_id,)).fetchall() if r["c"] != ""],
        }
        total = conn.execute(f"SELECT COUNT(*) AS n FROM ({base})", params).fetchone()["n"]

    return {
        "total_sources": total,
        "by_category": by_category,
        "by_domain": by_domain,
        "by_url": by_url,
        "by_model": by_model,
        "sunburst": sunburst,
        "pivot": pivot,
        "options": options,
    }


@app.get("/api/campaigns/{campaign_id}/pivot.csv")
def export_pivot(campaign_id: int, mode: str = "abs", modele: str | None = None,
                 langue: str | None = None, prompt: str | None = None,
                 run_id: int | None = None, categorie: str | None = None,
                 prompt_categorie: str | None = None):
    """TCD Catégorie de source × Prompt — valeurs absolues ou % du total du prompt."""
    if mode not in ("abs", "pct"):
        raise HTTPException(400, "mode doit être 'abs' ou 'pct'")
    campaign = _campaign_or_404(campaign_id)
    query, params = _result_query(campaign_id, locals())
    base = f"SELECT * FROM ({query}) f WHERE f.url <> ''"
    with db.connect() as conn:
        cells = db.rows_to_dicts(conn.execute(
            f"SELECT categorie, prompt, COUNT(*) AS n FROM ({base}) GROUP BY categorie, prompt",
            params,
        ).fetchall())

    prompts = sorted({c["prompt"] for c in cells})
    cats = sorted({c["categorie"] for c in cells})
    value = {(c["categorie"], c["prompt"]): c["n"] for c in cells}
    col_total = {p: sum(v for (cat, pr), v in value.items() if pr == p) for p in prompts}
    cat_total = {cat: sum(v for (c2, pr), v in value.items() if c2 == cat) for cat in cats}
    cats.sort(key=lambda c: -cat_total[c])

    def cell(cat, p):
        n = value.get((cat, p), 0)
        if mode == "abs":
            return n
        total = col_total[p]
        return round(100 * n / total, 1) if total else 0

    def rows():
        yield ["Catégorie"] + prompts + (["Total"] if mode == "abs" else [])
        for cat in cats:
            row = [cat] + [cell(cat, p) for p in prompts]
            if mode == "abs":
                row.append(cat_total[cat])
            yield row
        if mode == "abs":
            yield ["Total"] + [col_total[p] for p in prompts] + [sum(cat_total.values())]
        else:
            yield ["Total"] + [100 if col_total[p] else 0 for p in prompts]

    suffix = "TCD_absolu" if mode == "abs" else "TCD_pourcent"
    return _csv_response(f"{campaign['name'].replace(' ', '_')}_{suffix}.csv", rows())


UNIQUE_PROMPT_SORTS = {"categorie", "prompt", "modele", "langue"}


def _unique_prompts(campaign_id: int, sort: str = "categorie") -> list[dict]:
    """Unique (catégorie, prompt, langue, modèle) combos: executed ones from the
    results, plus imported prompts that have not run yet (modèle vide)."""
    # ORDER BY must use the UNION's output column aliases.
    order = sort if sort in UNIQUE_PROMPT_SORTS else "categorie"
    with db.connect() as conn:
        rows = db.rows_to_dicts(conn.execute(
            f"""
            SELECT COALESCE(prompt_categorie, '') AS categorie, prompt,
                   COALESCE(langue, '') AS langue, modele,
                   COUNT(*) AS citations
            FROM results WHERE campaign_id = :cid
            GROUP BY prompt_categorie, prompt, langue, modele
            UNION ALL
            SELECT COALESCE(p.categorie, ''), p.prompt, COALESCE(p.langue, ''), '', 0
            FROM prompts p
            WHERE p.campaign_id = :cid
              AND NOT EXISTS (SELECT 1 FROM results r
                              WHERE r.campaign_id = :cid AND r.prompt = p.prompt)
            ORDER BY {order} COLLATE NOCASE, prompt COLLATE NOCASE, modele
            """,
            {"cid": campaign_id},
        ).fetchall())
    return rows


@app.get("/api/campaigns/{campaign_id}/unique_prompts")
def unique_prompts(campaign_id: int, sort: str = "categorie"):
    _campaign_or_404(campaign_id)
    return _unique_prompts(campaign_id, sort)


@app.get("/api/campaigns/{campaign_id}/unique_prompts.csv")
def unique_prompts_csv(campaign_id: int, sort: str = "categorie"):
    campaign = _campaign_or_404(campaign_id)
    data = _unique_prompts(campaign_id, sort)

    def rows():
        yield ["Catégorie", "Prompt", "Langue", "Modèle", "Citations"]
        for r in data:
            yield [r["categorie"], r["prompt"], r["langue"], r["modele"], r["citations"]]

    return _csv_response(f"{campaign['name'].replace(' ', '_')}_prompts_uniques.csv", rows())


@app.get("/api/campaigns/{campaign_id}/unique_prompts.txt")
def unique_prompts_txt(campaign_id: int, sort: str = "categorie"):
    campaign = _campaign_or_404(campaign_id)
    data = _unique_prompts(campaign_id, sort)
    seen, lines = set(), []
    for r in data:  # plain text: one unique prompt per line
        if r["prompt"] not in seen:
            seen.add(r["prompt"])
            lines.append(r["prompt"])
    filename = f"{campaign['name'].replace(' ', '_')}_prompts_uniques.txt"
    return StreamingResponse(
        iter(["\n".join(lines)]),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/campaigns/{campaign_id}/uncategorized")
def uncategorized_domains(campaign_id: int, limit: int = 200):
    """Domains present in results but missing from the campaign's category set."""
    campaign = _campaign_or_404(campaign_id)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT r.domaine, COUNT(*) AS n FROM results r "
            "LEFT JOIN domain_categories dc "
            "  ON dc.set_id=? AND dc.domaine=r.domaine "
            "WHERE r.campaign_id=? AND r.domaine<>'' AND dc.id IS NULL "
            "GROUP BY r.domaine ORDER BY n DESC LIMIT ?",
            (campaign["category_set_id"], campaign_id, limit),
        ).fetchall()
    return db.rows_to_dicts(rows)


# ================================================================ visitor (read-only, token URL)

def _campaign_by_token(token: str):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM campaigns WHERE share_token=?", (token,)
        ).fetchone()
    if row is None or not token:
        raise HTTPException(404, "Lien visiteur invalide ou révoqué")
    return row


@app.get("/share/{token}")
def share_page(token: str):
    _campaign_by_token(token)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/share/{token}/info")
def share_info(token: str):
    campaign = _campaign_by_token(token)
    with db.connect() as conn:
        n_results = conn.execute(
            "SELECT COUNT(*) AS n FROM results WHERE campaign_id=?", (campaign["id"],)
        ).fetchone()["n"]
        last_run = conn.execute(
            "SELECT MAX(started_at) AS d FROM runs WHERE campaign_id=? AND status='done'",
            (campaign["id"],),
        ).fetchone()["d"]
    return {"name": campaign["name"], "n_results": n_results, "last_run": last_run}


@app.get("/api/share/{token}/dashboard")
def share_dashboard(token: str, modele: str | None = None, langue: str | None = None,
                    prompt: str | None = None, categorie: str | None = None,
                    prompt_categorie: str | None = None):
    campaign = _campaign_by_token(token)
    return dashboard(campaign["id"], modele, langue, prompt, None, categorie, prompt_categorie)


@app.get("/api/share/{token}/results")
def share_results(token: str, modele: str | None = None, langue: str | None = None,
                  prompt: str | None = None, categorie: str | None = None,
                  prompt_categorie: str | None = None, offset: int = 0, limit: int = 50):
    campaign = _campaign_by_token(token)
    return list_results(campaign["id"], modele, langue, prompt, None, categorie,
                        prompt_categorie, offset, limit)


@app.get("/api/share/{token}/export.csv")
def share_export(token: str, modele: str | None = None, langue: str | None = None,
                 prompt: str | None = None, categorie: str | None = None,
                 prompt_categorie: str | None = None):
    campaign = _campaign_by_token(token)
    return export_results(campaign["id"], modele, langue, prompt, None, categorie,
                          prompt_categorie)


@app.get("/api/share/{token}/pivot.csv")
def share_pivot(token: str, mode: str = "abs", modele: str | None = None,
                langue: str | None = None, prompt: str | None = None,
                categorie: str | None = None, prompt_categorie: str | None = None):
    campaign = _campaign_by_token(token)
    return export_pivot(campaign["id"], mode, modele, langue, prompt, None, categorie,
                        prompt_categorie)


# ================================================================ frontend

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
