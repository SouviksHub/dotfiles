import pytest

from pos.db import Database
from pos.money import fmt, parse_taka
from pos.service import VOID_WINDOW_S, Forbidden, Pos, PosError


@pytest.fixture
def env():
    events = []
    pos = Pos(Database(":memory:"), events=lambda k, p: events.append((k, p)))
    owner = pos.create_user("Owner", "owner", "999999")
    ravi = pos.create_user("Ravi", "cashier", "1234")
    karim = pos.create_user("Karim", "cashier", "5678")
    napa = pos.add_product(owner, name="Napa 500mg", generic="Paracetamol", price=parse_taka("12"), cost=900, barcode="8901")
    seclo = pos.add_product(owner, name="Seclo 20", generic="Omeprazole", price=parse_taka("7.50"), cost=500)
    pos.receive(owner, napa, 100, "INV-1")
    pos.receive(owner, seclo, 50, "INV-1")
    return pos, events, owner, ravi, karim, napa, seclo


def test_money():
    assert parse_taka("1,234.5") == 123450 and parse_taka("৳12") == 1200
    assert fmt(123450) == "৳1,234.50" and fmt(-5, "Tk") == "-Tk0.05"
    with pytest.raises(ValueError):
        parse_taka("-1")


def test_pin_login_and_lockout(env):
    pos, events, *_ = env
    assert pos.login("Ravi", "1234").name == "Ravi"
    for _ in range(5):
        with pytest.raises(Forbidden):
            pos.login("Ravi", "0000", now=1000)
    with pytest.raises(Forbidden, match="too many"):
        pos.login("Ravi", "1234", now=1001)   # correct PIN still refused while locked
    assert pos.login("Ravi", "1234", now=1000 + 301).name == "Ravi"
    assert any(p.get("kind") == "pin_lockout" for k, p in events if k == "alert")


def test_sale_prices_from_catalogue_and_kicks_drawer(env):
    pos, events, owner, ravi, _, napa, seclo = env
    with pytest.raises(PosError, match="open a shift"):
        pos.sell(ravi, [{"product_id": napa, "qty": 1}], "cash", 5000)
    pos.open_shift(ravi, parse_taka("500"))
    sale = pos.sell(ravi, [{"product_id": napa, "qty": 2, "price": 1}, {"product_id": seclo, "qty": 4}], "cash", 5500)
    assert sale["total"] == 2 * 1200 + 4 * 750 and sale["change"] == 5500 - 5400   # client "price" ignored
    assert sale["receipt_no"].endswith("-00001")
    assert pos.db.one("SELECT stock FROM products WHERE id = ?", (napa,))["stock"] == 98
    kinds = [k for k, _ in events]
    assert "sale" in kinds and "drawer_kick" in kinds


def test_card_sale_does_not_open_drawer(env):
    pos, events, owner, ravi, _, napa, _ = env
    pos.open_shift(ravi, 0)
    events.clear()
    pos.sell(ravi, [{"product_id": napa, "qty": 1}], "bkash")
    assert [k for k, _ in events] == ["sale"]


def test_underpayment_and_bad_input_rejected(env):
    pos, _, owner, ravi, _, napa, _ = env
    pos.open_shift(ravi, 0)
    with pytest.raises(PosError, match="less than"):
        pos.sell(ravi, [{"product_id": napa, "qty": 1}], "cash", 100)
    with pytest.raises(PosError):
        pos.sell(ravi, [{"product_id": napa, "qty": -3}], "cash", 100000)
    with pytest.raises(PosError):
        pos.sell(ravi, [{"product_id": 999, "qty": 1}], "cash", 100000)


def test_only_one_open_shift_and_blind_close_variance(env):
    pos, events, owner, ravi, karim, napa, _ = env
    shift = pos.open_shift(ravi, parse_taka("500"))
    with pytest.raises(PosError, match="another shift"):
        pos.open_shift(karim, 0)
    pos.sell(ravi, [{"product_id": napa, "qty": 10}], "cash", parse_taka("120"))   # +120 cash
    pos.sell(ravi, [{"product_id": napa, "qty": 5}], "nagad")                       # not cash
    pos.payout(ravi, parse_taka("20"), "tea for staff")                             # -20
    assert pos.expected_cash(shift) == parse_taka("600")
    assert pos.close_shift(ravi, parse_taka("550")) is None                          # returns nothing: blind
    row = pos.report(0, 1e12)["shifts"][0]
    assert row["variance"] == -parse_taka("50") and row["cashier"] == "Ravi"
    assert any(p.get("kind") == "cash_short" for k, p in events if k == "alert")


def test_void_rules(env):
    pos, events, owner, ravi, karim, napa, _ = env
    pos.open_shift(ravi, 0)
    s1 = pos.sell(ravi, [{"product_id": napa, "qty": 3}], "cash", 3600)
    assert pos.void(ravi, s1["id"], "customer changed mind") == "voided"
    assert pos.db.one("SELECT stock FROM products WHERE id = ?", (napa,))["stock"] == 100   # restored
    with pytest.raises(PosError, match="already"):
        pos.void(ravi, s1["id"], "again")

    s2 = pos.sell(ravi, [{"product_id": napa, "qty": 1}], "cash", 1200)
    late = s2["ts"] + VOID_WINDOW_S + 1
    assert pos.void(ravi, s2["id"], "wrong item", now=late) == "void_pending"          # too old
    assert pos.expected_cash(pos.current_shift(ravi)["id"]) == 1200                     # still counted
    with pytest.raises(Forbidden):
        pos.approve_void(ravi, s2["id"], True)
    pos.approve_void(owner, s2["id"], True)
    assert pos.sale(s2["id"])["status"] == "voided"
    assert sum(1 for k, p in events if k == "alert" and p["kind"] == "void") == 2
    with pytest.raises(PosError):
        pos.void(ravi, s2["id"], "")


def test_cashier_cannot_void_someone_elses_sale_directly(env):
    pos, _, owner, ravi, karim, napa, _ = env
    pos.open_shift(ravi, 0)
    s = pos.sell(ravi, [{"product_id": napa, "qty": 1}], "cash", 1200)
    assert pos.void(karim, s["id"], "mistake") == "void_pending"


def test_owner_only_actions(env):
    pos, _, owner, ravi, _, napa, _ = env
    for call in (lambda: pos.set_price(ravi, napa, 1), lambda: pos.no_sale_open(ravi, "x"),
                 lambda: pos.record_count(ravi, {napa: 1}), lambda: pos.add_product(ravi, name="x", price=1)):
        with pytest.raises(Forbidden):
            call()


def test_stock_count_values_loss(env):
    pos, events, owner, _, _, napa, seclo = env
    out = {o["product_id"]: o for o in pos.record_count(owner, {napa: 90, seclo: 50})}
    assert out[napa]["variance"] == -10 and out[napa]["value"] == -9000
    assert out[seclo]["variance"] == 0
    assert any(p.get("kind") == "stock_loss" for k, p in events if k == "alert")


def test_audit_chain_detects_tampering(env):
    pos, *_ = env
    assert pos.db.verify_audit() == (True, [])
    pos.db.conn.execute("UPDATE audit SET data = '{\"price\":1}' WHERE seq = 4")
    ok, problems = pos.db.verify_audit()
    assert not ok and "seq 4: entry modified" in problems
    pos.db.conn.execute("DELETE FROM audit WHERE seq = 2")
    assert any("gap" in p for p in pos.db.verify_audit()[1])


def test_search(env):
    pos, *_ = env
    assert [p["name"] for p in pos.search("8901")] == ["Napa 500mg"]
    assert [p["name"] for p in pos.search("omepra")] == ["Seclo 20"]


def test_owner_pin_must_be_longer(env):
    pos, *_ = env
    with pytest.raises(PosError, match="6-8"):
        pos.create_user("Boss2", "owner", "1234")


def test_backup_is_consistent_copy(env, tmp_path):
    pos, *_ = env
    dst = tmp_path / "bk" / "pos.db"
    pos.db.backup(dst)
    from pos.db import Database
    copy = Database(dst)
    assert copy.one("SELECT COUNT(*) AS n FROM products")["n"] == 2
    assert copy.verify_audit() == (True, [])
