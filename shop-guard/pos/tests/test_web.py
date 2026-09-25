import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_DB", str(tmp_path / "pos.db"))
    monkeypatch.setenv("PRINTER_HOST", f"file:{tmp_path / 'printer.bin'}")
    monkeypatch.setenv("POS_OWNER_NAME", "Owner")
    monkeypatch.setenv("POS_OWNER_PIN", "246810")
    monkeypatch.setenv("POS_SECRET", "test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.delenv("MQTT_HOST", raising=False)
    import pos.web as web
    web = importlib.reload(web)
    return web, tmp_path


def login(client, name, pin):
    return client.post("/login", data={"name": name, "pin": pin}, follow_redirects=False)


def test_full_shift_flow(app):
    web, tmp = app
    with TestClient(web.app) as owner, TestClient(web.app) as till:
        assert owner.get("/owner", follow_redirects=False).status_code == 303   # not logged in
        assert login(owner, "Owner", "246810").headers["location"] == "/owner"
        owner.post("/owner/users", data={"name": "Ravi", "pin": "1234", "role": "cashier"})
        owner.post("/owner/products", data={"name": "Napa 500mg", "price": "12", "cost": "9",
                                            "barcode": "8901", "opening_stock": "100"})

        assert login(till, "Ravi", "1111").headers["location"].startswith("/login?error=")
        assert login(till, "Ravi", "1234").headers["location"] == "/"
        assert till.get("/owner").status_code == 403                            # cashier blocked
        till.post("/shift/open", data={"opening_float": "500"})
        [napa] = till.get("/api/search", params={"q": "8901"}).json()

        r = till.post("/api/sale", json={"items": [{"product_id": napa["id"], "qty": 3}], "method": "cash", "tendered": "50"})
        assert r.status_code == 200 and r.json()["total"] == 3600 and r.json()["change"] == 1400 and r.json()["printed"]
        data = (tmp / "printer.bin").read_bytes()
        assert b"\x1bp\x00" in data and b"TOTAL" in data                        # kick + receipt

        r = till.post("/api/sale", json={"items": [{"product_id": napa["id"], "qty": 1}], "method": "cash", "tendered": "5"})
        assert r.status_code == 400 and "less than" in r.json()["detail"]

        sale2 = till.post("/api/sale", json={"items": [{"product_id": napa["id"], "qty": 1}], "method": "bkash"}).json()
        assert till.post("/api/void", json={"sale_id": sale2["id"], "reason": "wrong"}).json()["status"] == "voided"
        till.post("/api/payout", json={"amount": "10", "reason": "tea"})
        assert len(till.get("/api/my-sales").json()) == 2

        r = till.post("/shift/close", data={"counted_cash": "520"}, follow_redirects=False)
        assert "Thank" in r.headers["location"]                                  # blind: no expected figure shown
        assert till.get("/", follow_redirects=False).status_code == 303          # logged out

        page = owner.get("/owner").text
        assert "Ravi" in page and "-৳6.00" in page                               # expected 526, counted 520
        assert owner.get("/api/audit/verify").json()["intact"] is True


def test_photo_and_count(app, monkeypatch):
    web, _ = app
    monkeypatch.setattr(web.ai, "product_from_photo", lambda data, media: {"name": "Seclo 20", "mrp_taka": 7.5})
    with TestClient(web.app) as owner:
        login(owner, "Owner", "246810")
        r = owner.post("/owner/products/from-photo", files={"photo": ("box.jpg", b"\xff\xd8fake", "image/jpeg")})
        assert r.json()["name"] == "Seclo 20"
        owner.post("/owner/products", data={"name": "Seclo 20", "price": "7.50", "cost": "5", "opening_stock": "50"})
        pid = web.pos.search("Seclo")[0]["id"]
        page = owner.post("/owner/count", data={f"p_{pid}": "44"}).text
        assert "-৳30.00" in page                                                  # 6 missing x ৳5 cost
        assert "Seclo 20" in owner.get("/owner/products").text
        assert "Stock-count losses" in owner.get("/owner?days=1").text
