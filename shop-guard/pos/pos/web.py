"""POS web app: till screen for cashiers, dashboard for the owner (over Tailscale)."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from .ai import AIError, PosAI
from .bridge import Bridge
from .db import Database
from .money import fmt, parse_taka
from .printer import Printer, build_receipt
from .service import Forbidden, Pos, PosError, User

log = logging.getLogger(__name__)
ENV = os.environ.get
SHOP_NAME = ENV("SHOP_NAME", "SARKER MEDICAL HALL")
TZ = ENV("POS_TIMEZONE", "Asia/Dhaka")
SENTINEL_URL = ENV("SENTINEL_URL", "")  # e.g. http://sentinel:8080, for the shift log in analyses

printer = Printer(ENV("PRINTER_HOST", ""), int(ENV("PRINTER_PORT", "9100")))
bridge = Bridge(printer)
pos = Pos(Database(Path(ENV("POS_DB", "pos.db"))), tz=TZ, events=bridge.on_pos_event)
bridge.pos = pos
ai = PosAI(ENV("CLAUDE_MODEL", "claude-opus-5"))

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["taka"] = fmt
templates.env.filters["local"] = lambda ts: dt.datetime.fromtimestamp(ts, pos.tz).strftime("%d %b %H:%M") if ts else "-"

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not pos.users() and ENV("POS_OWNER_NAME") and ENV("POS_OWNER_PIN"):
        pos.create_user(ENV("POS_OWNER_NAME"), "owner", ENV("POS_OWNER_PIN"))
        log.info("created owner account %s", ENV("POS_OWNER_NAME"))
    if ENV("MQTT_HOST"):
        bridge.start(ENV("MQTT_HOST"), int(ENV("MQTT_PORT", "1883")))
    if ENV("POS_BACKUP_DIR"):
        threading.Thread(target=_backup_loop, args=(Path(ENV("POS_BACKUP_DIR")),), daemon=True).start()
    yield


def _backup_loop(folder: Path) -> None:
    """Hourly snapshot into the evidence tree; the off-site service copies it out.
    One file per day, overwritten hourly; B2 versioning keeps every hourly version."""
    while True:
        try:
            pos.db.backup(folder / f"pos-{dt.datetime.now(pos.tz):%Y-%m-%d}.db")
        except Exception:
            log.exception("POS backup failed")
        time.sleep(3600)


app = FastAPI(title="Shop Guard POS", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=ENV("POS_SECRET") or secrets.token_hex(32),
                   max_age=12 * 3600, same_site="strict", https_only=False)


@app.exception_handler(PosError)
def pos_error(request: Request, exc: PosError):
    return JSONResponse({"detail": str(exc)}, status_code=403 if isinstance(exc, Forbidden) else 400)


# ---- auth -------------------------------------------------------------------

def current_user(request: Request) -> User:
    uid = request.session.get("uid")
    user = pos.user(uid) if uid else None
    if not user:
        raise HTTPException(303, headers={"Location": "/login"})
    return user


def owner(user: User = Depends(current_user)) -> User:
    if not user.is_owner:
        raise HTTPException(403, "owner only")
    return user


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    names = [u["name"] for u in pos.users() if u["active"]]
    return templates.TemplateResponse(request, "login.html", {"names": names, "error": error})


@app.post("/login")
def login(request: Request, name: str = Form(...), pin: str = Form(...)):
    try:
        user = pos.login(name, pin)
    except Forbidden as exc:
        return RedirectResponse(f"/login?error={exc}", status_code=303)
    request.session.clear()
    request.session["uid"] = user.id
    return RedirectResponse("/owner" if user.is_owner else "/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---- till -------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def till(request: Request, user: User = Depends(current_user), closed: int = 0):
    return templates.TemplateResponse(request, "till.html", {
        "user": user, "shift": pos.current_shift(user), "closed": closed, "shop": SHOP_NAME})


@app.post("/shift/open")
def shift_open(opening_float: str = Form(...), user: User = Depends(current_user)):
    pos.open_shift(user, parse_taka(opening_float))
    return RedirectResponse("/", status_code=303)


@app.post("/shift/close")
def shift_close(request: Request, counted_cash: str = Form(...), user: User = Depends(current_user)):
    pos.close_shift(user, parse_taka(counted_cash))
    request.session.clear()
    return RedirectResponse("/login?error=Shift closed. Thank you.", status_code=303)


@app.get("/api/search")
def search(q: str, user: User = Depends(current_user)):
    return [{k: p[k] for k in ("id", "name", "generic", "strength", "price", "stock", "barcode")}
            for p in pos.search(q)]


class SaleIn(BaseModel):
    items: list[dict]
    method: str
    tendered: str | None = None


@app.post("/api/sale")
def sale(body: SaleIn, user: User = Depends(current_user)):
    tendered = parse_taka(body.tendered) if body.tendered not in (None, "") else None
    s = pos.sell(user, body.items, body.method, tendered)
    printed = printer.receipt(build_receipt(s, SHOP_NAME, pos.tz), kick=body.method == "cash")
    return {**s, "printed": printed}


@app.get("/api/my-sales")
def my_sales(user: User = Depends(current_user)):
    shift = pos.current_shift(user)
    if not shift:
        return []
    return pos.db.all("SELECT id, receipt_no, ts, total, method, status FROM sales WHERE shift_id = ? "
                      "ORDER BY ts DESC LIMIT 20", (shift["id"],))


class VoidIn(BaseModel):
    sale_id: int
    reason: str


@app.post("/api/void")
def void(body: VoidIn, user: User = Depends(current_user)):
    return {"status": pos.void(user, body.sale_id, body.reason)}


class PayoutIn(BaseModel):
    amount: str
    reason: str


@app.post("/api/payout")
def payout(body: PayoutIn, user: User = Depends(current_user)):
    pos.payout(user, parse_taka(body.amount), body.reason)
    return {"ok": True}


# ---- owner ------------------------------------------------------------------

def _period(days: int) -> tuple[float, float]:
    today = dt.datetime.now(pos.tz).date()
    start, _ = pos.day_bounds(today - dt.timedelta(days=days - 1))
    return start, time.time() + 1


@app.get("/owner", response_class=HTMLResponse)
def dashboard(request: Request, days: int = 1, user: User = Depends(owner)):
    since, until = _period(days)
    audit_ok, audit_problems = pos.db.verify_audit()
    return templates.TemplateResponse(request, "owner.html", {
        "user": user, "days": days, "r": pos.report(since, until), "pending": pos.pending_voids(),
        "low": pos.low_stock(), "audit_ok": audit_ok, "audit_problems": audit_problems,
        "users": pos.users(), "drawer_state": bridge.monitor.state})


@app.post("/owner/void/{sale_id}")
def owner_void(sale_id: int, approve: int = Form(...), user: User = Depends(owner)):
    pos.approve_void(user, sale_id, bool(approve))
    return RedirectResponse("/owner", status_code=303)


@app.post("/owner/users")
def owner_add_user(name: str = Form(...), pin: str = Form(...), role: str = Form("cashier"),
                   user: User = Depends(owner)):
    pos.create_user(name, role, pin, actor=user.name)
    return RedirectResponse("/owner", status_code=303)


@app.post("/owner/no-sale")
def owner_no_sale(reason: str = Form(...), user: User = Depends(owner)):
    pos.no_sale_open(user, reason)
    return RedirectResponse("/owner", status_code=303)


@app.get("/owner/products", response_class=HTMLResponse)
def products(request: Request, q: str = "", user: User = Depends(owner)):
    rows = pos.search(q, limit=500) if q else pos.db.all("SELECT * FROM products ORDER BY name LIMIT 500")
    return templates.TemplateResponse(request, "products.html", {"user": user, "rows": rows, "q": q})


@app.post("/owner/products")
def add_product(name: str = Form(...), price: str = Form(...), cost: str = Form("0"), barcode: str = Form(""),
                generic: str = Form(""), strength: str = Form(""), form: str = Form(""),
                manufacturer: str = Form(""), reorder_level: int = Form(0), opening_stock: int = Form(0),
                user: User = Depends(owner)):
    pid = pos.add_product(user, name=name, price=parse_taka(price), cost=parse_taka(cost or "0"),
                          barcode=barcode.strip() or None, generic=generic, strength=strength, form=form,
                          manufacturer=manufacturer, reorder_level=reorder_level)
    if opening_stock > 0:
        pos.receive(user, pid, opening_stock, "opening stock")
    return RedirectResponse("/owner/products", status_code=303)


@app.post("/owner/products/{pid}/price")
def change_price(pid: int, price: str = Form(...), user: User = Depends(owner)):
    pos.set_price(user, pid, parse_taka(price))
    return RedirectResponse("/owner/products", status_code=303)


@app.post("/owner/products/{pid}/receive")
def receive(pid: int, qty: int = Form(...), ref: str = Form(...), user: User = Depends(owner)):
    pos.receive(user, pid, qty, ref)
    return RedirectResponse("/owner/products", status_code=303)


@app.post("/owner/products/from-photo")
async def from_photo(photo: UploadFile = File(...), user: User = Depends(owner)):
    data = await photo.read()
    if len(data) > 8_000_000:
        raise HTTPException(413, "photo too large (max 8 MB)")
    media = photo.content_type if photo.content_type in ("image/jpeg", "image/png", "image/webp") else "image/jpeg"
    try:
        return ai.product_from_photo(data, media)
    except AIError as exc:
        raise HTTPException(502, str(exc))


@app.get("/owner/count", response_class=HTMLResponse)
def count_page(request: Request, user: User = Depends(owner)):
    rows = pos.db.all("SELECT id, name, stock, cost FROM products WHERE active = 1 ORDER BY cost * stock DESC LIMIT 200")
    return templates.TemplateResponse(request, "count.html", {"user": user, "rows": rows, "result": None})


@app.post("/owner/count", response_class=HTMLResponse)
async def count_post(request: Request, user: User = Depends(owner)):
    form = await request.form()
    counts = {int(k[2:]): int(v) for k, v in form.items() if k.startswith("p_") and str(v).strip() != ""}
    result = pos.record_count(user, counts)
    return templates.TemplateResponse(request, "count.html", {"user": user, "rows": [], "result": result})


@app.post("/owner/analyse")
def analyse(days: int = Form(7), user: User = Depends(owner)):
    since, until = _period(days)
    sightings = []
    if SENTINEL_URL:
        try:
            r = httpx.get(f"{SENTINEL_URL}/api/sightings", params={"since": since, "until": until}, timeout=10,
                          auth=(ENV("DASHBOARD_USER", "owner"), ENV("DASHBOARD_PASSWORD", "")))
            sightings = r.json() if r.status_code == 200 else []
        except httpx.HTTPError:
            pass
    try:
        text = ai.loss_analysis(pos.report(since, until), f"last {days} days", sightings)
    except AIError as exc:
        raise HTTPException(502, str(exc))
    return {"analysis": text}


@app.get("/api/report")
def report(since: float, until: float, user: User = Depends(owner)):
    return json.loads(pos.export_json(since, until))


@app.get("/api/audit/verify")
def audit_verify(user: User = Depends(owner)):
    ok, problems = pos.db.verify_audit()
    return {"intact": ok, "problems": problems}
