"""ESC/POS receipt printing and cash-drawer kick for a LAN thermal printer (port 9100).

The drawer's RJ11 cable plugs into the printer; the printer pulses it only when told
to. Set PRINTER_HOST=file:/path to write the byte stream to a file instead (testing).
"""

from __future__ import annotations

import datetime as dt
import logging
import socket
from zoneinfo import ZoneInfo

from .money import fmt

log = logging.getLogger(__name__)

INIT = b"\x1b@"
KICK = b"\x1bp\x00\x19\xfa"          # pulse pin 2: 25*2ms on, 250*2ms off
CUT = b"\x1dVB\x00"
CENTER, LEFT = b"\x1ba\x01", b"\x1ba\x00"
BOLD_ON, BOLD_OFF = b"\x1bE\x01", b"\x1bE\x00"


def _line(left: str, right: str, width: int) -> str:
    left = left[: max(1, width - len(right) - 1)]
    return left + " " * (width - len(left) - len(right)) + right + "\n"


def build_receipt(sale: dict, shop_name: str, tz: ZoneInfo, width: int = 32) -> bytes:
    """Receipt text is ASCII (Bengali needs raster printing; out of scope for v1)."""
    ts = dt.datetime.fromtimestamp(sale["ts"], tz)
    out = [INIT, CENTER, BOLD_ON, (shop_name[:width] + "\n").encode("ascii", "replace"), BOLD_OFF, LEFT]
    text = f"Receipt {sale['receipt_no']}\n{ts:%d %b %Y %H:%M}   Cashier: {sale['cashier']}\n" + "-" * width + "\n"
    for it in sale["items"]:
        text += (it["name"][:width] + "\n")
        text += _line(f"  {it['qty']} x {fmt(it['price'], 'Tk')}", fmt(it["line_total"], "Tk"), width)
    text += "-" * width + "\n"
    text += _line("TOTAL", fmt(sale["total"], "Tk"), width)
    text += _line(f"Paid ({sale['method']})", fmt(sale["tendered"], "Tk"), width)
    if sale["method"] == "cash":
        text += _line("Change", fmt(sale["change"], "Tk"), width)
    out += [text.encode("ascii", "replace"), CENTER, b"\nAlways ask for your receipt\n\n\n", CUT]
    return b"".join(out)


class Printer:
    def __init__(self, target: str, port: int = 9100, timeout: float = 5.0):
        self.target, self.port, self.timeout = target, port, timeout

    def send(self, data: bytes) -> bool:
        if not self.target:
            log.info("no printer configured; %d bytes dropped", len(data))
            return False
        try:
            if self.target.startswith("file:"):
                with open(self.target[5:], "ab") as fh:
                    fh.write(data)
            else:
                with socket.create_connection((self.target, self.port), timeout=self.timeout) as s:
                    s.sendall(data)
            return True
        except OSError as exc:
            log.error("printer error: %s", exc)
            return False

    def kick(self) -> bool:
        return self.send(INIT + KICK)

    def receipt(self, data: bytes, kick: bool) -> bool:
        return self.send((INIT + KICK if kick else b"") + data)
