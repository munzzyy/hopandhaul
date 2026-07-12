#!/usr/bin/env python3
"""
flights.py - live-flight pricing interface for the map server.

Exposes ONE small interface so server.py never has to know provider details:
  provider_name() -> "duffel" | None
  have_keys()     -> bool
  open_session()  -> opaque session dict, or None
  search_cheapest(session, origin_iata, dest_iata, date, **opts) -> normalized dict | None

Duffel is the only live provider. There used to be an Amadeus Self-Service fallback here;
Amadeus decommissioned that entire portal (announced Feb 2026, keys die 2026-07-17), so the
fallback was removed rather than left to fail silently in July 2026. No key set -> the server
falls back to the labeled distance estimates, same as always.

Normalized result shape:
  {"price": <usd float>, "hours": float, "stops": int, "carrier": str,
   "currency": str, "converted": bool, "source": "duffel", "rt": bool,
   "checked_bags_included": int|None, "refundable": bool, "changeable": bool,
   "native_price": float|None, "segments": list[dict]}
"segments" is the real per-hop schedule - departure/arrival clock, carrier, flight number - 
itinerary.py uses it to show a live leg's actual timeline instead of a synthetic example one.
"""
from __future__ import annotations

try:
    from . import duffel
except Exception:  # pragma: no cover
    duffel = None


def provider_name() -> str | None:
    if duffel and duffel.have_keys():
        return "duffel"
    return None


def have_keys() -> bool:
    return provider_name() is not None


def open_session() -> dict | None:
    """Returns a session dict for the live provider, or None."""
    if provider_name() == "duffel":
        return {"provider": "duffel"}
    return None


def search_cheapest(session, origin, dest, date, nonstop=False, cabin="economy",
                    adults=1, return_date=None) -> dict | None:
    """Cheapest normalized offer via the session's provider, or None.

    Result adds: "rt": True when the price already covers a priced return slice.
    Price covers all `adults`.
    """
    if not session or session.get("provider") != "duffel":
        return None
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
