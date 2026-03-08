"""Live FX conversion using frankfurter.app (free, no API key)."""
import httpx
from functools import lru_cache

_CACHE: dict[tuple[str, str], float] = {}


async def get_rate(from_currency: str, to_currency: str) -> float:
    """Return live exchange rate from_currency → to_currency."""
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    if from_currency == to_currency:
        return 1.0
    key = (from_currency, to_currency)
    if key in _CACHE:
        return _CACHE[key]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://api.frankfurter.app/latest",
            params={"from": from_currency, "to": to_currency},
        )
        resp.raise_for_status()
        data = resp.json()
        rate = float(data["rates"][to_currency])
    _CACHE[key] = rate
    return rate


async def convert(amount: float, from_currency: str, to_currency: str) -> float:
    """Convert amount from from_currency to to_currency."""
    rate = await get_rate(from_currency, to_currency)
    return round(amount * rate, 2)
