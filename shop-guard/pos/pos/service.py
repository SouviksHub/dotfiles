"""POS business rules. All money is integer paisa; all writes are audited.

Anti-fraud rules enforced here (not in the UI, so they can't be bypassed):
- the server prices every line from the catalogue; the client only sends ids and qty
- a sale needs the cashier's own open shift
- shift close is blind: the cashier submits a count and never sees the expected cash
- cashiers may void only their own sale, only within VOID_WINDOW_S; older voids wait
  for the owner. Every void is flagged for camera review.
- only the owner can open the drawer without a sale, change prices, or adjust stock
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .db import Database

VOID_WINDOW_S = 600
PIN_LOCK_AFTER = 5
PIN_LOCK_S = 300
METHODS = ("cash", "bkash", "nagad", "card")


class PosError(Exception):
    """A rule violation to show to the user (HTTP 400/403)."""


class Forbidden(PosError):
    pass


@dataclass(frozen=True)
class User:
    id: int
    name: str
    role: str

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"


def hash_pin(pin: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}${digest.hex()}"


def check_pin(pin: str, stored: str) -> bool:
    salt_hex, digest_hex = stored.split("$", 1)
    digest = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)


class Pos:
    def __init__(self, db: Database, tz: str = "Asia/Dhaka", events=None):
        """events: optional callable(kind, payload) for MQTT/alert fan-out."""
        self.db = db
        self.tz = ZoneInfo(tz)
        self.events = events or (lambda kind, payload: None)
        self._failures: dict[str, list[float]] = {}

    # ---- users ------------------------------------------------------------

    def create_user(self, name: str, role: str, pin: str, actor: str = "setup") -> User:
        minimum = 6 if role == "owner" else 4
        if not (pin.isdigit() and minimum <= len(pin) <= 8):
            raise PosError(f"PIN must be {minimum}-8 digits")
        with self.db.tx() as c:
            cur = c.execute("INSERT INTO users (name, role, pin_hash) VALUES (?, ?, ?)",
                            (name.strip(), role, hash_pin(pin)))
            self.db.audit(c, actor, "user.create", {"name": name, "role": role})
        return User(cur.lastrowid, name.strip(), role)

    def login(self, name: str, pin: str, now: float | None = None) -> User:
        now = now or time.time()
        recent = [t for t in self._failures.get(name, []) if now - t < PIN_LOCK_S]
        if len(recent) >= PIN_LOCK_AFTER:
            raise Forbidden("too many wrong PINs; try again in 5 minutes")
        row = self.db.one("SELECT * FROM users WHERE name = ? AND active = 1", (name,))
        if not row or not check_pin(pin, row["pin_hash"]):
            self._failures[name] = recent + [now]
            if len(recent) + 1 >= PIN_LOCK_AFTER:
                self.events("alert", {"kind": "pin_lockout", "ts": now, "detail": f"{name}: repeated wrong PINs"})
            raise Forbidden("wrong name or PIN")
        self._failures.pop(name, None)
        return User(row["id"], row["name"], row["role"])

    def user(self, user_id: int) -> User | None:
        row = self.db.one("SELECT * FROM users WHERE id = ? AND active = 1", (user_id,))
        return User(row["id"], row["name"], row["role"]) if row else None

    def users(self) -> list[dict]:
        return self.db.all("SELECT id, name, role, active FROM users ORDER BY name")

    # ---- catalogue --------------------------------------------------------

    def add_product(self, actor: User, *, name: str, price: int, barcode: str | None = None,
                    generic: str = "", strength: str = "", form: str = "", manufacturer: str = "",
                    cost: int = 0, reorder_level: int = 0) -> int:
        if not actor.is_owner:
            raise Forbidden("only the owner can add products or set prices")
        with self.db.tx() as c:
            cur = c.execute(
                """INSERT INTO products (barcode, name, generic, strength, form, manufacturer, price, cost, reorder_level)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (barcode or None, name.strip(), generic, strength, form, manufacturer, price, cost, reorder_level))
            self.db.audit(c, actor.name, "product.add", {"id": cur.lastrowid, "name": name, "price": price, "cost": cost})
        return cur.lastrowid

    def set_price(self, actor: User, product_id: int, price: int) -> None:
        if not actor.is_owner:
            raise Forbidden("only the owner can change prices")
        with self.db.tx() as c:
            old = c.execute("SELECT price FROM products WHERE id = ?", (product_id,)).fetchone()
            if not old:
                raise PosError("no such product")
            c.execute("UPDATE products SET price = ? WHERE id = ?", (price, product_id))
            self.db.audit(c, actor.name, "product.price", {"id": product_id, "from": old["price"], "to": price})

    def search(self, query: str, limit: int = 20) -> list[dict]:
        q = query.strip()
        exact = self.db.all("SELECT * FROM products WHERE barcode = ? AND active = 1", (q,))
        if exact:
            return exact
        like = f"%{q}%"
        return self.db.all(
            """SELECT * FROM products WHERE active = 1 AND (name LIKE ? OR generic LIKE ?)
               ORDER BY name LIMIT ?""", (like, like, limit))

    # ---- shifts -----------------------------------------------------------

    def open_shift(self, user: User, opening_float: int) -> int:
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM shifts WHERE closed_at IS NULL").fetchone():
                raise PosError("another shift is still open; it must be closed (with a cash count) first")
            cur = c.execute("INSERT INTO shifts (user_id, opened_at, opening_float) VALUES (?, ?, ?)",
                            (user.id, time.time(), opening_float))
            self.db.audit(c, user.name, "shift.open", {"shift": cur.lastrowid, "float": opening_float})
        return cur.lastrowid

    def current_shift(self, user: User) -> dict | None:
        return self.db.one("SELECT * FROM shifts WHERE user_id = ? AND closed_at IS NULL", (user.id,))

    def expected_cash(self, shift_id: int, conn=None) -> int:
        conn = conn or self.db.conn
        s = conn.execute("SELECT opening_float FROM shifts WHERE id = ?", (shift_id,)).fetchone()
        sales = conn.execute(
            "SELECT COALESCE(SUM(total), 0) FROM sales WHERE shift_id = ? AND method = 'cash' AND status != 'voided'",
            (shift_id,)).fetchone()[0]
        payouts = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM payouts WHERE shift_id = ?", (shift_id,)).fetchone()[0]
        return s["opening_float"] + sales - payouts

    def close_shift(self, user: User, counted_cash: int) -> None:
        """Blind close: nothing about the expected amount is returned to the cashier."""
        with self.db.tx() as c:
            shift = c.execute("SELECT * FROM shifts WHERE user_id = ? AND closed_at IS NULL", (user.id,)).fetchone()
            if not shift:
                raise PosError("you have no open shift")
            expected = self.expected_cash(shift["id"], c)
            c.execute("UPDATE shifts SET closed_at = ?, counted_cash = ?, expected_cash = ? WHERE id = ?",
                      (time.time(), counted_cash, expected, shift["id"]))
            self.db.audit(c, user.name, "shift.close",
                          {"shift": shift["id"], "counted": counted_cash, "expected": expected})
        variance = counted_cash - expected
        if variance < 0:
            self.events("alert", {"kind": "cash_short", "ts": time.time(), "cashier": user.name,
                                  "detail": f"shift {shift['id']} short by {-variance} paisa"})

    def payout(self, user: User, amount: int, reason: str) -> None:
        """Cash taken out for an expense (e.g. tea, courier). Always visible to the owner."""
        if not reason.strip():
            raise PosError("a payout needs a reason")
        shift = self.current_shift(user)
        if not shift:
            raise PosError("open a shift first")
        with self.db.tx() as c:
            c.execute("INSERT INTO payouts (ts, shift_id, user_id, amount, reason) VALUES (?, ?, ?, ?, ?)",
                      (time.time(), shift["id"], user.id, amount, reason.strip()))
            self.db.audit(c, user.name, "payout", {"shift": shift["id"], "amount": amount, "reason": reason})
        self.events("drawer_kick", {"reason": "payout", "user": user.name})

    # ---- sales ------------------------------------------------------------

    def sell(self, user: User, items: list[dict], method: str, tendered: int | None = None) -> dict:
        """items: [{"product_id": int, "qty": int}]. Prices come from the catalogue."""
        if method not in METHODS:
            raise PosError(f"unknown payment method {method!r}")
        if not items:
            raise PosError("empty sale")
        shift = self.current_shift(user)
        if not shift:
            raise PosError("open a shift before selling")
        now = time.time()
        with self.db.tx() as c:
            lines, total = [], 0
            for it in items:
                qty = int(it["qty"])
                if qty <= 0:
                    raise PosError("quantity must be positive")
                p = c.execute("SELECT * FROM products WHERE id = ? AND active = 1", (int(it["product_id"]),)).fetchone()
                if not p:
                    raise PosError(f"unknown product {it['product_id']}")
                lines.append((p, qty, p["price"] * qty))
                total += p["price"] * qty
            tendered = total if tendered is None else int(tendered)
            if method == "cash" and tendered < total:
                raise PosError("cash tendered is less than the total")
            cur = c.execute(
                "INSERT INTO sales (ts, user_id, shift_id, total, method, tendered) VALUES (?, ?, ?, ?, ?, ?)",
                (now, user.id, shift["id"], total, method, tendered))
            sale_id = cur.lastrowid
            receipt = f"{dt.datetime.fromtimestamp(now, self.tz):%y%m%d}-{sale_id:05d}"
            c.execute("UPDATE sales SET receipt_no = ? WHERE id = ?", (receipt, sale_id))
            for p, qty, line_total in lines:
                c.execute("INSERT INTO sale_items (sale_id, product_id, name, qty, price, line_total) VALUES (?, ?, ?, ?, ?, ?)",
                          (sale_id, p["id"], p["name"], qty, p["price"], line_total))
                c.execute("UPDATE products SET stock = stock - ? WHERE id = ?", (qty, p["id"]))
                c.execute("INSERT INTO stock_moves (ts, product_id, delta, reason, ref, user_id) VALUES (?, ?, ?, 'sale', ?, ?)",
                          (now, p["id"], -qty, receipt, user.id))
            self.db.audit(c, user.name, "sale", {"sale": sale_id, "receipt": receipt, "total": total, "method": method,
                                                 "items": [[p["id"], q, lt] for p, q, lt in lines]})
        sale = self.sale(sale_id)
        self.events("sale", {"sale_id": sale_id, "receipt": receipt, "total": total, "method": method,
                             "cashier": user.name, "ts": now})
        if method == "cash":
            self.events("drawer_kick", {"reason": "sale", "sale_id": sale_id, "user": user.name})
        return sale

    def sale(self, sale_id: int) -> dict:
        s = self.db.one("""SELECT s.*, u.name AS cashier FROM sales s JOIN users u ON u.id = s.user_id
                           WHERE s.id = ?""", (sale_id,))
        if not s:
            raise PosError("no such sale")
        s["items"] = self.db.all("SELECT * FROM sale_items WHERE sale_id = ?", (sale_id,))
        s["change"] = s["tendered"] - s["total"]
        return s

    def void(self, user: User, sale_id: int, reason: str, now: float | None = None) -> str:
        """Returns the resulting status: 'voided' or 'void_pending' (waiting for the owner)."""
        now = now or time.time()
        if not reason.strip():
            raise PosError("a void needs a reason")
        s = self.sale(sale_id)
        if s["status"] != "completed":
            raise PosError(f"sale is already {s['status']}")
        within_window = now - s["ts"] <= VOID_WINDOW_S and s["user_id"] == user.id
        if user.is_owner or within_window:
            self._apply_void(user, s, reason, now)
            status = "voided"
        else:
            with self.db.tx() as c:
                c.execute("UPDATE sales SET status = 'void_pending', void_reason = ? WHERE id = ?", (reason, sale_id))
                self.db.audit(c, user.name, "void.request", {"sale": sale_id, "reason": reason})
            status = "void_pending"
        self.events("alert", {"kind": "void", "ts": now, "cashier": s["cashier"], "sale_id": sale_id,
                              "detail": f"{status}: receipt {s['receipt_no']} {s['total']} paisa by {user.name}: {reason}",
                              "sale_ts": s["ts"]})
        return status

    def approve_void(self, owner: User, sale_id: int, approve: bool) -> None:
        if not owner.is_owner:
            raise Forbidden("only the owner can approve voids")
        s = self.sale(sale_id)
        if s["status"] != "void_pending":
            raise PosError("no pending void for this sale")
        if approve:
            self._apply_void(owner, s, s["void_reason"] or "", time.time())
        else:
            with self.db.tx() as c:
                c.execute("UPDATE sales SET status = 'completed' WHERE id = ?", (sale_id,))
                self.db.audit(c, owner.name, "void.reject", {"sale": sale_id})

    def _apply_void(self, actor: User, s: dict, reason: str, now: float) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE sales SET status = 'voided', void_reason = ?, voided_by = ?, voided_at = ? WHERE id = ?",
                      (reason, actor.id, now, s["id"]))
            for it in s["items"]:
                c.execute("UPDATE products SET stock = stock + ? WHERE id = ?", (it["qty"], it["product_id"]))
                c.execute("INSERT INTO stock_moves (ts, product_id, delta, reason, ref, user_id) VALUES (?, ?, ?, 'void', ?, ?)",
                          (now, it["product_id"], it["qty"], s["receipt_no"], actor.id))
            self.db.audit(c, actor.name, "void", {"sale": s["id"], "reason": reason})

    def no_sale_open(self, owner: User, reason: str) -> None:
        if not owner.is_owner:
            raise Forbidden("only the owner can open the drawer without a sale")
        with self.db.tx() as c:
            self.db.audit(c, owner.name, "drawer.no_sale", {"reason": reason})
        self.events("drawer_kick", {"reason": "no_sale", "user": owner.name})

    # ---- stock ------------------------------------------------------------

    def receive(self, user: User, product_id: int, qty: int, supplier_ref: str) -> None:
        if qty <= 0:
            raise PosError("quantity must be positive")
        if not supplier_ref.strip():
            raise PosError("enter the supplier invoice number")
        with self.db.tx() as c:
            c.execute("UPDATE products SET stock = stock + ? WHERE id = ?", (qty, product_id))
            c.execute("INSERT INTO stock_moves (ts, product_id, delta, reason, ref, user_id) VALUES (?, ?, ?, 'receive', ?, ?)",
                      (time.time(), product_id, qty, supplier_ref.strip(), user.id))
            self.db.audit(c, user.name, "stock.receive", {"product": product_id, "qty": qty, "ref": supplier_ref})

    def record_count(self, owner: User, counts: dict[int, int]) -> list[dict]:
        """Physical stock count. Returns variances (counted - expected), valued at cost."""
        if not owner.is_owner:
            raise Forbidden("only the owner can post a stock count")
        now, out = time.time(), []
        with self.db.tx() as c:
            for pid, counted in counts.items():
                p = c.execute("SELECT * FROM products WHERE id = ?", (int(pid),)).fetchone()
                if not p:
                    raise PosError(f"unknown product {pid}")
                delta = int(counted) - p["stock"]
                if delta:
                    c.execute("UPDATE products SET stock = ? WHERE id = ?", (int(counted), p["id"]))
                    c.execute("INSERT INTO stock_moves (ts, product_id, delta, reason, ref, user_id) VALUES (?, ?, ?, 'count', 'count', ?)",
                              (now, p["id"], delta, owner.id))
                out.append({"product_id": p["id"], "name": p["name"], "expected": p["stock"], "counted": int(counted),
                            "variance": delta, "value": delta * p["cost"]})
            self.db.audit(c, owner.name, "stock.count", {"lines": [[o["product_id"], o["counted"], o["variance"]] for o in out]})
        loss = -sum(o["value"] for o in out if o["value"] < 0)
        if loss:
            self.events("alert", {"kind": "stock_loss", "ts": now, "detail": f"count found stock missing worth {loss} paisa at cost"})
        return out

    # ---- reporting --------------------------------------------------------

    def day_bounds(self, day: dt.date) -> tuple[float, float]:
        start = dt.datetime.combine(day, dt.time.min, self.tz).timestamp()
        return start, start + 86400

    def report(self, since: float, until: float) -> dict:
        by_cashier = self.db.all(
            """SELECT u.name AS cashier, COUNT(*) AS sales, SUM(s.total) AS total,
                      SUM(CASE WHEN s.method = 'cash' THEN s.total ELSE 0 END) AS cash
               FROM sales s JOIN users u ON u.id = s.user_id
               WHERE s.ts >= ? AND s.ts < ? AND s.status != 'voided' GROUP BY u.name ORDER BY total DESC""",
            (since, until))
        voids = self.db.all(
            """SELECT s.id, s.receipt_no, s.ts, s.total, s.status, s.void_reason, u.name AS cashier
               FROM sales s JOIN users u ON u.id = s.user_id
               WHERE s.ts >= ? AND s.ts < ? AND s.status IN ('voided', 'void_pending') ORDER BY s.ts""", (since, until))
        shifts = self.db.all(
            """SELECT sh.*, u.name AS cashier, sh.counted_cash - sh.expected_cash AS variance
               FROM shifts sh JOIN users u ON u.id = sh.user_id
               WHERE sh.opened_at >= ? AND sh.opened_at < ? ORDER BY sh.opened_at""", (since, until))
        payouts = self.db.all(
            """SELECT p.*, u.name AS cashier FROM payouts p JOIN users u ON u.id = p.user_id
               WHERE p.ts >= ? AND p.ts < ? ORDER BY p.ts""", (since, until))
        drawer_alerts = self.db.all(
            "SELECT * FROM drawer_events WHERE kind = 'alert' AND ts >= ? AND ts < ? ORDER BY ts", (since, until))
        count_moves = self.db.all(
            """SELECT m.ts, p.name, m.delta, m.delta * p.cost AS value FROM stock_moves m JOIN products p ON p.id = m.product_id
               WHERE m.reason = 'count' AND m.ts >= ? AND m.ts < ? ORDER BY value""", (since, until))
        return {"by_cashier": by_cashier, "voids": voids, "shifts": shifts, "payouts": payouts,
                "drawer_alerts": drawer_alerts, "stock_variances": count_moves,
                "total": sum(r["total"] or 0 for r in by_cashier)}

    def low_stock(self) -> list[dict]:
        return self.db.all("SELECT * FROM products WHERE active = 1 AND stock <= reorder_level ORDER BY stock")

    def pending_voids(self) -> list[dict]:
        return self.db.all("""SELECT s.*, u.name AS cashier FROM sales s JOIN users u ON u.id = s.user_id
                              WHERE s.status = 'void_pending' ORDER BY s.ts""")

    def record_drawer_event(self, ts: float, kind: str, sale_id: int | None = None, detail: str = "") -> None:
        with self.db.tx() as c:
            c.execute("INSERT INTO drawer_events (ts, kind, sale_id, detail) VALUES (?, ?, ?, ?)",
                      (ts, kind, sale_id, detail))

    def export_json(self, since: float, until: float) -> str:
        return json.dumps(self.report(since, until), default=str)
