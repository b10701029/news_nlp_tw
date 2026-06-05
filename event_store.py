"""JSON-backed event storage for news_nlp_tw."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone

from config import DATA_DIR

logger = logging.getLogger(__name__)
_STORE_PATH = os.path.join(DATA_DIR, "events.json")


def _load() -> list[dict]:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.exists(_STORE_PATH):
            with open(_STORE_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save(events: list[dict]) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = _STORE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(events, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _STORE_PATH)
    except Exception as e:
        logger.warning("event_store save failed: %s", e)


def make_event_id(ticker: str, event_type: str, date_str: str) -> str:
    raw = f"{ticker}:{event_type}:{date_str}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def store_event(event: dict) -> bool:
    """Store event, return False if duplicate."""
    events = _load()
    eid = make_event_id(
        event.get("ticker", ""),
        event.get("event_type", ""),
        event.get("timestamp", "")[:10],
    )
    if any(e.get("event_id") == eid for e in events):
        return False
    event["event_id"] = eid
    event["stored_at"] = datetime.now(timezone.utc).isoformat()
    events.append(event)
    # Keep last 1000 events
    if len(events) > 1000:
        events = events[-1000:]
    _save(events)
    return True


def get_recent_events(days: int = 1) -> list[dict]:
    events = _load()
    cutoff = datetime.now(timezone.utc).isoformat()[:10]
    return [e for e in events if e.get("timestamp", "")[:10] >= cutoff]
