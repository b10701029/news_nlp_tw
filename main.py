"""news_nlp_tw — Taiwan MOPS event extraction and notification.

Fetches daily MOPS major announcements, extracts events via Claude API,
and notifies Telegram for high-magnitude events.

Usage:
  python3 main.py [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
import requests
from datetime import datetime, timezone

from config import TG_BOT_TOKEN, TG_CHAT_ID, NOTIFY_MAGNITUDE_THRESHOLD, DATA_DIR
from mops_fetcher import fetch_mops_announcements
from extractor import extract_event, sentiment_to_score
from event_store import store_event

_SHARED_BUS = "/home/ubuntu/shared/events.jsonl"


def _bus_publish(event_type, payload, source):
    try:
        os.makedirs("/home/ubuntu/shared", exist_ok=True)
        event = {"id": f"{event_type}_{int(time.time())}", "ts": __import__("datetime").datetime.utcnow().isoformat()+"Z", "type": event_type, "source": source, "payload": payload, "consumed_by": []}
        with open(_SHARED_BUS, "a") as f:
            f.write(json.dumps(event)+"\n")
    except Exception:
        pass


_SCORES_LOG = os.path.join(DATA_DIR, "scores.jsonl")


def _append_score_log(ticker: str, score: float) -> None:
    """Append a signed sentiment score to scores.jsonl for Spearman IC tracking."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        rec = {
            "ticker": ticker,
            "ts": time.time(),
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "score": score,
        }
        with open(_SCORES_LOG, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def send_telegram(text: str) -> bool:
    if not TG_BOT_TOKEN:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text},
            timeout=10,
        )
        return r.ok
    except Exception:
        return False


def run(date_str: str | None = None) -> None:
    if not date_str:
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")

    log.info("Fetching MOPS announcements for %s", date_str)
    announcements = fetch_mops_announcements(date_str)
    log.info("Found %d announcements", len(announcements))

    high_magnitude = []
    for ann in announcements:
        ticker = ann.get("ticker", "")
        title = ann.get("announcement_title", "")
        body = ""
        if ann.get("content_url"):
            try:
                from mops_fetcher import fetch_announcement_content
                body = fetch_announcement_content(ann["content_url"])
            except Exception:
                pass
        content = (title + " " + body).strip()
        if not content:
            continue

        event = extract_event(ticker, content)
        if not event:
            continue

        event["timestamp"] = ann.get("announcement_time", date_str)
        is_new = store_event(event)

        if is_new:
            _append_score_log(ticker, sentiment_to_score(event))

        if is_new and event.get("magnitude", 0) >= NOTIFY_MAGNITUDE_THRESHOLD:
            event["company_name"] = ann.get("company_name", ticker)
            high_magnitude.append(event)
            log.info("High-magnitude event: %s %s %s", ticker, event.get("company_name"), event.get("summary_zh"))

    if high_magnitude:
        lines = ["📰 台股重大訊息摘要", "━━━━━━━━━━━━━━━━━━"]
        for e in high_magnitude[:5]:
            icon = "🟢" if e["sentiment"] == "positive" else "🔴" if e["sentiment"] == "negative" else "⚪"
            co = e.get("company_name", e["ticker"])
            label = f"{e['ticker']} {co}" if co != e["ticker"] else e["ticker"]
            lines.append(f"{icon} {label} [{e['event_type']}] 強度:{e['magnitude']}")
            lines.append(f"   {e.get('summary_zh', '')}")
        send_telegram("\n".join(lines))

        for event in high_magnitude:
            if event.get("magnitude", 0) >= 6:
                _bus_publish("mops_event_high", {
                    "ticker": event.get("ticker") or event.get("stock_code"),
                    "event_type": event.get("event_type", "unknown"),
                    "sentiment": event.get("sentiment", "neutral"),
                    "magnitude": event.get("magnitude", 0),
                    "summary": event.get("summary_zh", "")[:100]
                }, "news_nlp_tw")

    log.info("Done. %d high-magnitude events notified.", len(high_magnitude))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Date YYYYMMDD (default: today)")
    args = parser.parse_args()
    run(args.date)


if __name__ == "__main__":
    main()
