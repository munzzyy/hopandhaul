#!/usr/bin/env python3
"""
flights.py — provider-agnostic live-flight pricing for the map server.

Picks the best available flight source and exposes ONE small interface so server.py never has
to know which provider is live:
  provider_name() -> "duffel" | "amadeus" | None
  have_keys()     -> bool
  open_session()  -> opaque session dict (does any auth up front), or None
  search_cheapest(session, origin_iata, dest_iata, date, **opts) -> normalized dict | None

Preference order: Duffel (Cole's key) first, then Amadeus if those env vars happen to be set,
else None (server falls back to distance estimates). Normalized result shape:
  {"price": <usd float>, "hours": float, "stops": int, "carrier": str,
   "currency": str, "converted": bool, "source": "duffel"|"amadeus",
   "checked_bags_included": int|None, "refundable": bool, "changeable": bool,
   "native_price": float|None, "segments": list[dict]}
Baggage/fare-condition fields are Duffel-only (None/False on Amadeus, which doesn't return them
via this call) — never silently invented, always present so callers don't need a hasattr check.
"segments" (also Duffel-only; always [] on Amadeus) is the real per-hop schedule — departure/
arrival clock, carrier, flight number — itinerary.py uses to show a live leg's actual timeline
instead of a synthetic example one.
"""
from __future__ import annotations

import os

try:
    from . import duffel
except Exception:  # pragma: no cover
    duffel = None
try:
    from . import providers  # Amadeus adapter (optional legacy)
except Exception:  # pragma: no cover
    providers = None


def provider_name() -> str | None:
    if duffel and duffel.have_keys():
        return "duffel"
    if providers and providers.have_keys():
        return "amadeus"
    return None


def have_keys() -> bool:
    return provider_name() is not None


def open_session() -> dict | None:
    """Do any up-front auth (Amadeus token). Returns a session or None."""
    name = provider_name()
    if name == "duffel":
        return {"provider": "duffel"}
    if name == "amadeus":
        base = (providers.PROD_BASE if os.environ.get("AMADEUS_ENV", "").lower() == "prod"
                else providers.TEST_BASE)
        return {"provider": "amadeus", "base": base, "token": providers.get_token(base)}
    return None


def search_cheapest(session, origin, dest, date, nonstop=False, cabin="economy",
                    adults=1, return_date=None) -> dict | None:
    """Cheapest normalized offer via the session's provider, or None.

    Result adds: "rt": True when the price already covers a priced return slice
    (Duffel only). Price covers all `adults`. Amadeus (legacy) prices 1 adult
    one-way and is scaled ×adults here; its rt is always False so the caller
    applies its own round-trip approximation.
    """
    if not session:
        return None
    if session["provider"] == "duffel":
        r = duffel.search_cheapest(origin, dest, date, adults=adults, nonstop=nonstop,
                                   cabin=cabin, return_date=return_date)
        if not r:
            return None
        return {"price": r["price"], "hours": r["hours"], "stops": r["stops"],
                "carrier": r["carrier"], "currency": r["currency"],
                "converted": r["converted"], "source": "duffel", "rt": r.get("rt", False),
                "checked_bags_included": r.get("checked_bags_included"),
                "refundable": r.get("refundable", False), "changeable": r.get("changeable", False),
                "native_price": r.get("native_price"), "segments": r.get("segments", [])}
    if session["provider"] == "amadeus":
        r = providers.search_cheapest(session["base"], session["token"], origin, dest, date,
                                      nonstop=nonstop)
        if not r:
            return None
        return {"price": round(r["price"] * max(1, adults), 2), "hours": r["hours"],
                "stops": r.get("stops", 0), "carrier": r.get("carrier", "?"),
                "currency": "USD", "converted": False, "source": "amadeus", "rt": False,
                "checked_bags_included": None, "refundable": False, "changeable": False,
                "native_price": None, "segments": []}
    return None
