"""Dashboard + JSON API. Runs the pipeline in background threads."""

from __future__ import annotations

import datetime as dt
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .analyzer import Analyzer, AnalysisError
from .config import Settings
from .db import DB
from .evidence import EvidenceLocker
from .frigate import Frigate
from .notify import Telegram
from .pipeline import Pipeline
from .rules import Rules
from .shifts import present_at, shift_log
from .watchdog import Watchdog

log = logging.getLogger(__name__)
settings = Settings.from_env()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
security = HTTPBasic(auto_error=False)


def require_login(creds: HTTPBasicCredentials | None = Depends(security)) -> None:
    if not settings.dashboard_password:
        return  # protected by Tailscale only
    ok = creds is not None and secrets.compare_digest(creds.username, settings.dashboard_user) \
        and secrets.compare_digest(creds.password, settings.dashboard_password)
    if not ok:
        raise HTTPException(401, "login required", headers={"WWW-Authenticate": "Basic"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    rules = Rules.load(settings.rules_path)
    db = DB(settings.db_path)
    frigate = Frigate(settings.frigate_url)
    pipeline = Pipeline(
        rules=rules, db=db, frigate=frigate,
        analyzer=Analyzer(settings.claude_model, settings.claude_effort),
        locker=EvidenceLocker(settings.evidence_dir),
        telegram=Telegram(settings.telegram_token, settings.telegram_chat_id),
        frames_per_event=settings.frames_per_event,
    )
    pipeline.start(settings.mqtt_host, settings.mqtt_port)
    watchdog = Watchdog(
        frigate=frigate, telegram=pipeline.telegram, rules=rules, db=db,
        ref_dir=settings.data_dir / "references", backup_dir=settings.evidence_dir / "db-backups",
        healthcheck_url=settings.healthcheck_url,
    )
    watchdog.start()
    app.state.p = pipeline
    app.state.w = watchdog
    yield
    pipeline.mqtt.loop_stop()


app = FastAPI(title="Shop Guard", lifespan=lifespan, dependencies=[Depends(require_login)])


def _p(request: Request) -> Pipeline:
    return request.app.state.p


def _decorate(p: Pipeline, rows: list[dict]) -> list[dict]:
    for r in rows:
        r["local_time"] = p.rules.local(r["start_ts"]).strftime("%a %d %b %H:%M:%S")
    return rows


@app.get("/", response_class=HTMLResponse)
def index(request: Request, person: str | None = None, min_score: str | None = None, days: int = 7):
    p = _p(request)
    min_score = int(min_score) if min_score and min_score.isdigit() else None  # form sends "" for "any"
    since = (dt.datetime.now(p.rules.tz) - dt.timedelta(days=days)).timestamp()
    rows = _decorate(p, p.db.query(since=since, person=person or None, min_score=min_score))
    return templates.TemplateResponse(request, "index.html", {
        "rows": rows, "people": p.db.people(), "person": person, "min_score": min_score,
        "days": days, "watchlist": sorted(p.rules.watchlist),
    })


@app.get("/incident/{event_id}", response_class=HTMLResponse)
def incident(request: Request, event_id: str):
    p = _p(request)
    row = p.db.get(event_id)
    if not row:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "incident.html", {"r": _decorate(p, [row])[0]})


def _evidence_file(p: Pipeline, event_id: str, name: str) -> Path | None:
    row = p.db.get(event_id)
    for rel in ((row or {}).get("evidence") or {}).get("files", {}):
        if rel.endswith("/" + name):
            return p.locker.root / rel
    return None


@app.get("/media/{event_id}/snapshot.jpg")
def snapshot(request: Request, event_id: str):
    p = _p(request)
    if path := _evidence_file(p, event_id, "snapshot.jpg"):
        return FileResponse(path, media_type="image/jpeg")
    data = p.frigate.snapshot(event_id)
    if not data:
        raise HTTPException(404)
    return Response(data, media_type="image/jpeg")


@app.get("/media/{event_id}/clip.mp4")
def clip(request: Request, event_id: str):
    p = _p(request)
    if path := _evidence_file(p, event_id, "clip.mp4"):
        return FileResponse(path, media_type="video/mp4")
    try:
        return Response(p.frigate.clip(event_id, attempts=1), media_type="video/mp4")
    except RuntimeError:
        raise HTTPException(404, "clip no longer available in Frigate")


class Ask(BaseModel):
    question: str
    days: int = 7


@app.post("/api/ask")
def ask(request: Request, body: Ask):
    p = _p(request)
    since = (dt.datetime.now(p.rules.tz) - dt.timedelta(days=body.days)).timestamp()
    rows = _decorate(p, p.db.query(since=since, limit=500))
    try:
        return {"answer": p.analyzer.answer(body.question, rows, str(p.rules.tz))}
    except AnalysisError as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/incidents")
def incidents(request: Request, person: str | None = None, min_score: int | None = None, days: int = 7):
    p = _p(request)
    since = (dt.datetime.now(p.rules.tz) - dt.timedelta(days=days)).timestamp()
    return _decorate(p, p.db.query(since=since, person=person, min_score=min_score))


@app.get("/api/evidence/verify")
def verify(request: Request):
    ok, problems = _p(request).locker.verify()
    return {"intact": ok, "problems": problems}


@app.post("/api/report")
def report_now(request: Request):
    try:
        return {"report": _p(request).send_daily_report()}
    except AnalysisError as exc:
        raise HTTPException(502, str(exc))


@app.get("/shifts", response_class=HTMLResponse)
def shifts(request: Request, days: int = 14):
    p = _p(request)
    until = time.time()
    rows = shift_log(p.db.sightings(until - days * 86400, until), p.rules.tz)
    return templates.TemplateResponse(request, "shifts.html", {"rows": rows, "days": days})


@app.get("/api/present")
def present(request: Request, at: str):
    """Who was in the shop around a local time, e.g. ?at=2026-02-10T14:30"""
    p = _p(request)
    ts = dt.datetime.fromisoformat(at).replace(tzinfo=p.rules.tz).timestamp()
    return {"at": at, "present": present_at(p.db.sightings(ts - 86400, ts + 86400), ts)}


@app.get("/api/watchdog")
def watchdog_status(request: Request):
    w: Watchdog = request.app.state.w
    return {"problems": w.problems,
            "cameras": {c: {"offline": s.offline.alerted, "covered": s.covered.alerted, "moved": s.moved.alerted}
                        for c, s in w.state.items()}}


@app.post("/api/watchdog/reference/{camera}")
def add_reference(request: Request, camera: str, reset: bool = False):
    """Save the camera's current view as known-good. Use reset=true after re-aiming a camera;
    call again at night so the infrared view is also recognised."""
    w: Watchdog = request.app.state.w
    if reset:
        w.reset_references(camera)
    try:
        return {"camera": camera, "references": w.add_reference(camera)}
    except RuntimeError as exc:
        raise HTTPException(404, str(exc))
