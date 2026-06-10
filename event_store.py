"""JSON-backed event storage for news_nlp_tw."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta

from config import DATA_DIR

logger = logging.getLogger(__name__)
_STORE_PATH = os.path.join(DATA_DIR, "events.json")


def _load() -> dict:
    """Load events as ticker-keyed dict {bare_ticker: [event, ...]}."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.exists(_STORE_PATH):
            with open(_STORE_PATH) as f:
                data = json.load(f)
            # Migrate legacy list format to dict
            if isinstance(data, list):
                migrated: dict = {}
                for ev in data:
                    bare = ev.get("ticker", "").replace(".TW", "").replace(".TWO", "")
                    migrated.setdefault(bare, []).append(ev)
                return migrated
            return data
    except Exception:
        pass
    return {}


def _save(store: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = _STORE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _STORE_PATH)
    except Exception as e:
        logger.warning("event_store save failed: %s", e)


def make_event_id(ticker: str, event_type: str, date_str: str) -> str:
    raw = f"{ticker}:{event_type}:{date_str}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def store_event(event: dict) -> bool:
    """Store event keyed by bare ticker. Returns False if duplicate."""
    store = _load()
    eid = make_event_id(
        event.get("ticker", ""),
        event.get("event_type", ""),
        event.get("timestamp", "")[:10],
    )
    bare = event.get("ticker", "").replace(".TW", "").replace(".TWO", "")
    ticker_events = store.get(bare, [])
    if any(e.get("event_id") == eid for e in ticker_events):
        return False
    event["event_id"] = eid
    event["stored_at"] = datetime.now(timezone.utc).isoformat()
    # Add epoch ts for benzema216 decay calculation (scanner.py uses float(ts))
    event["ts"] = time.time()
    ticker_events.append(event)
    # Keep last 200 events per ticker
    if len(ticker_events) > 200:
        ticker_events = ticker_events[-200:]
    store[bare] = ticker_events
    _save(store)
    return True


def get_recent_events(days: int = 1) -> dict:
    """Return ticker-keyed dict of events within the last N days."""
    store = _load()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()[:10]
    result: dict = {}
    for ticker, events in store.items():
        recent = [e for e in events if e.get("timestamp", "")[:10] >= cutoff]
        if recent:
            result[ticker] = recent
    return result
