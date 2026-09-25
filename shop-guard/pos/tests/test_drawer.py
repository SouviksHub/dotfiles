from zoneinfo import ZoneInfo

from pos.drawer import DrawerMonitor
from pos.printer import KICK, build_receipt


def kinds(alerts):
    return [a.kind for a in alerts]


def test_kick_then_open_is_authorised_once():
    m = DrawerMonitor()
    m.kick(100, 7, "sale", "Ravi")
    assert m.sensor(102, "open") == []
    assert m.sensor(110, "closed") == []
    assert kinds(m.sensor(115, "open")) == ["unauthorized_open"]   # same kick can't be reused


def test_open_without_kick_or_after_window():
    m = DrawerMonitor(kick_window_s=6)
    assert kinds(m.sensor(50, "open")) == ["unauthorized_open"]
    m.sensor(55, "closed")
    m.kick(100, 1, "sale", "Ravi")
    assert kinds(m.sensor(107, "open")) == ["unauthorized_open"]


def test_open_too_long_alerts_once_with_sale():
    m = DrawerMonitor(max_open_s=60)
    m.kick(0, 9, "sale", "Ravi")
    m.sensor(1, "open")
    assert m.tick(30) == []
    a = m.tick(62)
    assert kinds(a) == ["open_too_long"] and a[0].sale_id == 9
    assert m.tick(90) == []
    m.sensor(95, "closed")
    m.kick(100, 10, "sale", "Ravi"); m.sensor(101, "open")
    assert kinds(m.tick(170)) == ["open_too_long"]                # re-arms after closing


def test_sensor_offline():
    m = DrawerMonitor(sensor_timeout_s=90)
    m.heartbeat(0)
    assert m.tick(80) == []
    assert kinds(m.tick(95)) == ["sensor_offline"]
    assert m.tick(200) == []
    m.heartbeat(210)
    assert kinds(m.tick(400)) == ["sensor_offline"]


def test_kick_without_open_means_sensor_bypassed():
    m = DrawerMonitor(expect_open_s=10)
    m.heartbeat(0)
    m.kick(0, 3, "sale", "Karim")
    a = m.tick(11)
    assert kinds(a) == ["kick_without_open"] and "Karim" in a[0].detail and a[0].sale_id == 3


def test_receipt_bytes():
    sale = {"ts": 1_790_000_000, "receipt_no": "260925-00001", "cashier": "Ravi", "method": "cash",
            "total": 5400, "tendered": 6000, "change": 600,
            "items": [{"name": "Napa 500mg", "qty": 2, "price": 1200, "line_total": 2400},
                      {"name": "Seclo 20 with a very very long name", "qty": 4, "price": 750, "line_total": 3000}]}
    data = build_receipt(sale, "SARKER MEDICAL HALL", ZoneInfo("Asia/Dhaka"))
    text = data.decode("ascii", "replace")
    assert "TOTAL" in text and "Tk54.00" in text and "Change" in text and "Tk6.00" in text
    assert KICK not in data                                        # kick is sent separately
    for line in text.split("\n"):
        printable = "".join(ch for ch in line if ch.isprintable())
        assert len(printable) <= 40
