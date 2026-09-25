"""Money is stored as integer paisa (1 taka = 100 paisa). Never floats."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def parse_taka(text: str | int | float) -> int:
    try:
        value = Decimal(str(text).replace(",", "").replace("৳", "").replace("Tk", "").strip())
    except InvalidOperation as exc:
        raise ValueError(f"not an amount: {text!r}") from exc
    if value < 0:
        raise ValueError("amount cannot be negative")
    return int((value * 100).quantize(Decimal("1")))


def fmt(paisa: int, symbol: str = "৳") -> str:
    sign = "-" if paisa < 0 else ""
    taka, p = divmod(abs(paisa), 100)
    return f"{sign}{symbol}{taka:,}.{p:02d}"
