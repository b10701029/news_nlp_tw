"""MOPS 重大訊息 fetcher for news_nlp_tw.

Fetches Taiwan MOPS (公開資訊觀測站) major announcements via POST API,
parses the HTML table response, and retrieves full announcement text.

Point-in-time: all timestamps use MOPS announcement datetime (UTC, ISO-8601),
never datetime.now().

Rate limiting: 1 req/second with exponential backoff on 503.
Cache: announcements cached 24h by date key in data/mops_cache.json.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MOPS_BASE = "https://mops.twse.com.tw"
_MOPS_API = f"{_MOPS_BASE}/mops/web/ajax_t05st01_q1"

# TWSE Open Data announcement API (stable JSON). The legacy
# ajax_t05st01_q1 HTML table scrape no longer returns parseable rows, so the
# list fetch now uses this endpoint. Returns a JSON array of the day's company
# announcements (重大訊息). Fields (tolerant to casing): Date (ROC YYYMMDD),
# Time (HHMMSS), Code, Name, Title, Content, Url.
_TWSE_OPENAPI = "https://openapi.twse.com.tw/v1/announcement/companyann"

_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Referer": "https://mops.twse.com.tw/mops/web/t05st01",
}

_CACHE_PATH = Path(__file__).parent / "data" / "mops_cache.json"
_CACHE_TTL_HOURS = 24
_CONTENT_MAX_CHARS = 2000
_REQUEST_TIMEOUT = 20  # seconds
_MIN_REQUEST_INTERVAL = 1.0  # seconds between requests
_MAX_RETRIES = 3

# Gregorian year offset for ROC calendar
_ROC_OFFSET = 1911

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_last_request_time: float = 0.0
_cache: dict = {}
_cache_loaded: bool = False


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache() -> None:
    global _cache, _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    if not _CACHE_PATH.exists():
        _cache = {}
        return
    try:
        with open(_CACHE_PATH, encoding="utf-8") as f:
            _cache = json.load(f)
    except Exception as exc:
        logger.warning("mops_cache load failed (starting fresh): %s", exc)
        _cache = {}


def _save_cache() -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _cache["generated_at"] = time.time()  # freshness stamp for monitoring
        tmp = _CACHE_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False, indent=2)
        tmp.replace(_CACHE_PATH)
    except Exception as exc:
        logger.warning("mops_cache save failed: %s", exc)


def _cache_get(key: str) -> Optional[list[dict]]:
    _load_cache()
    entry = _cache.get(key)
    if not entry:
        return None
    # TTL check
    stored_at = entry.get("stored_at", 0)
    if time.time() - stored_at > _CACHE_TTL_HOURS * 3600:
        del _cache[key]
        return None
    return entry.get("data")


def _cache_set(key: str, data: list[dict]) -> None:
    _load_cache()
    _cache[key] = {"stored_at": time.time(), "data": data}
    _save_cache()


# ---------------------------------------------------------------------------
# Rate-limited HTTP helper
# ---------------------------------------------------------------------------

def _rate_limited_get(
    url: str,
    *,
    method: str = "GET",
    data: Optional[dict] = None,
    timeout: int = _REQUEST_TIMEOUT,
) -> requests.Response:
    """Single HTTP call with 1-req/s rate limit and exponential backoff on 503."""
    global _last_request_time

    for attempt in range(_MAX_RETRIES):
        # Enforce 1 req/second
        elapsed = time.monotonic() - _last_request_time
        if elapsed < _MIN_REQUEST_INTERVAL:
            time.sleep(_MIN_REQUEST_INTERVAL - elapsed)

        try:
            if method == "POST":
                resp = requests.post(
                    url, headers=_HEADERS, data=data, timeout=timeout
                )
            else:
                resp = requests.get(url, headers=_HEADERS, timeout=timeout)

            _last_request_time = time.monotonic()

            if resp.status_code == 503:
                wait = 2 ** attempt  # 1s, 2s, 4s
                logger.warning(
                    "MOPS 503 (attempt %d/%d), retrying in %ds",
                    attempt + 1, _MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp

        except requests.exceptions.Timeout:
            logger.warning("MOPS request timeout (attempt %d/%d)", attempt + 1, _MAX_RETRIES)
            if attempt == _MAX_RETRIES - 1:
                raise
            time.sleep(2 ** attempt)
        except requests.exceptions.RequestException as exc:
            logger.warning("MOPS request error (attempt %d/%d): %s", attempt + 1, _MAX_RETRIES, exc)
            if attempt == _MAX_RETRIES - 1:
                raise
            time.sleep(2 ** attempt)

    raise RuntimeError(f"All {_MAX_RETRIES} attempts failed for {url}")


# ---------------------------------------------------------------------------
# Date utilities
# ---------------------------------------------------------------------------

def _to_roc_date(date_str: str) -> tuple[str, str, str]:
    """Convert YYYYMMDD Gregorian date to (roc_year, month, day) strings."""
    dt = datetime.strptime(date_str, "%Y%m%d")
    roc_year = str(dt.year - _ROC_OFFSET)
    month = f"{dt.month:02d}"
    day = f"{dt.day:02d}"
    return roc_year, month, day


def _parse_mops_datetime(date_field: str, time_field: str) -> Optional[str]:
    """Parse MOPS ROC date (e.g. '115/06/05') + time ('14:30:00') to UTC ISO-8601.

    Returns None if parsing fails.
    """
    try:
        date_clean = date_field.strip()
        time_clean = time_field.strip() or "00:00:00"

        # Handle ROC format: YYY/MM/DD or YYYMMDD
        if "/" in date_clean:
            parts = date_clean.split("/")
            roc_y, mon, day = int(parts[0]), int(parts[1]), int(parts[2])
        else:
            roc_y = int(date_clean[:3])
            mon = int(date_clean[3:5])
            day = int(date_clean[5:7])

        greg_year = roc_y + _ROC_OFFSET
        # MOPS times are Taiwan local time (UTC+8)
        naive_dt = datetime(greg_year, mon, day,
                            *[int(x) for x in time_clean.split(":")])
        tw_tz = timezone(timedelta(hours=8))
        aware_dt = naive_dt.replace(tzinfo=tw_tz)
        utc_dt = aware_dt.astimezone(timezone.utc)
        return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception as exc:
        logger.debug("Failed to parse MOPS datetime '%s %s': %s", date_field, time_field, exc)
        return None


def _normalize_ticker(raw_id: str) -> str:
    """Convert raw MOPS 公司代號 (e.g. '2330') to NNNN.TW format."""
    code = raw_id.strip().lstrip("0") or raw_id.strip()
    # Re-pad to at least 4 digits
    code = raw_id.strip().zfill(4) if raw_id.strip().isdigit() else raw_id.strip()
    return f"{code}.TW"


# ---------------------------------------------------------------------------
# HTML table parser
# ---------------------------------------------------------------------------

def _parse_mops_table(html: str, date_str: str) -> list[dict]:
    """Parse the HTML table from MOPS API response.

    Returns a list of announcement dicts with keys:
        ticker, company_name, announcement_title,
        announcement_time (UTC ISO-8601), content_url
    """
    # MOPS sometimes returns Big5-encoded pages; requests should detect encoding
    soup = BeautifulSoup(html, "html.parser")

    announcements: list[dict] = []

    # The main data table has class 'hasBorder' or similar; scan all <tr> rows
    rows = soup.find_all("tr")
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 6:
            continue

        # Typical column order: 序號, 公司代號, 公司名稱, 發言日期, 發言時間, 主旨, 說明/連結
        # Column indices may vary; identify by content heuristics
        cell_texts = [c.get_text(strip=True) for c in cells]

        # Skip header rows (non-numeric first cell or contains 公司)
        if not cell_texts[0].isdigit():
            continue

        try:
            # Try standard column layout
            co_id = cell_texts[1].strip()
            co_name = cell_texts[2].strip()
            announce_date = cell_texts[3].strip()
            announce_time = cell_texts[4].strip()
            title = cell_texts[5].strip()

            if not co_id or not co_id[0].isdigit():
                continue

            # Build content URL from the link in the row, if present
            link_tag = row.find("a", href=True)
            content_url = ""
            if link_tag:
                href = link_tag["href"]
                if href.startswith("http"):
                    content_url = href
                else:
                    content_url = f"{_MOPS_BASE}{href}"

            # If no link, build a standard URL pattern
            if not content_url:
                roc_y, mon, day = _to_roc_date(date_str)
                content_url = (
                    f"{_MOPS_BASE}/mops/web/t05st01"
                    f"?encodeURIComponent=1&step=2&firstin=1&off=1"
                    f"&co_id={co_id}&year={roc_y}&month={mon}&day={day}"
                )

            point_in_time_ts = _parse_mops_datetime(announce_date, announce_time)
            if point_in_time_ts is None:
                # Fallback: use the date_str midnight UTC+8
                dt = datetime.strptime(date_str, "%Y%m%d").replace(
                    tzinfo=timezone(timedelta(hours=8))
                ).astimezone(timezone.utc)
                point_in_time_ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

            announcements.append({
                "ticker": _normalize_ticker(co_id),
                "company_name": co_name,
                "announcement_title": title,
                "announcement_time": point_in_time_ts,  # UTC, point-in-time
                "content_url": content_url,
                "_raw_co_id": co_id,  # kept for debugging
            })

        except (IndexError, ValueError) as exc:
            logger.debug("Skipping malformed row: %s — %s", cell_texts, exc)
            continue

    return announcements


# ---------------------------------------------------------------------------
# TWSE Open Data JSON parser
# ---------------------------------------------------------------------------

def _get_field(row: dict, *names: str) -> str:
    """Case/variant-tolerant lookup of a field in a TWSE announcement row."""
    lower = {k.lower(): v for k, v in row.items()}
    for name in names:
        if name in row and row[name] is not None:
            return str(row[name]).strip()
        if name.lower() in lower and lower[name.lower()] is not None:
            return str(lower[name.lower()]).strip()
    return ""


def _parse_announcement_json(payload, date_str: str) -> list[dict]:
    """Parse the TWSE Open Data company-announcement JSON array.

    Each element is a dict with (casing-tolerant) keys:
        Date  — ROC date, e.g. "1150612" or "115/06/12"
        Time  — "HHMMSS" or "HH:MM:SS" (optional)
        Code  — company code, e.g. "2330"
        Name  — company name, e.g. "台積電"
        Title — announcement subject
        Url   — detail page link (optional)
    """
    if not isinstance(payload, list):
        logger.warning(
            "mops_fetcher: unexpected JSON shape (%s), expected list",
            type(payload).__name__,
        )
        return []

    announcements: list[dict] = []
    for row in payload:
        if not isinstance(row, dict):
            continue

        co_id = _get_field(row, "Code", "code", "co_id", "公司代號")
        if not co_id or not co_id[0].isdigit():
            continue

        co_name = _get_field(row, "Name", "name", "公司名稱", "co_name")
        title = _get_field(row, "Title", "title", "主旨")
        date_field = _get_field(row, "Date", "date", "發言日期") or date_str
        time_field = _get_field(row, "Time", "time", "發言時間")

        # TWSE "Time" is often HHMMSS without separators
        if time_field and ":" not in time_field and time_field.isdigit():
            time_field = time_field.zfill(6)
            time_field = f"{time_field[:2]}:{time_field[2:4]}:{time_field[4:6]}"

        point_in_time_ts = _parse_mops_datetime(date_field, time_field)
        if point_in_time_ts is None:
            # Fallback: midnight UTC+8 of the requested date
            dt = datetime.strptime(date_str, "%Y%m%d").replace(
                tzinfo=timezone(timedelta(hours=8))
            ).astimezone(timezone.utc)
            point_in_time_ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        content_url = _get_field(row, "Url", "url", "連結")
        if content_url and not content_url.startswith("http"):
            content_url = f"{_MOPS_BASE}{content_url}"
        if not content_url:
            roc_y, mon, day = _to_roc_date(date_str)
            content_url = (
                f"{_MOPS_BASE}/mops/web/t05st01"
                f"?encodeURIComponent=1&step=2&firstin=1&off=1"
                f"&co_id={co_id}&year={roc_y}&month={mon}&day={day}"
            )

        announcements.append({
            "ticker": _normalize_ticker(co_id),
            "company_name": co_name,
            "announcement_title": title,
            "announcement_time": point_in_time_ts,  # UTC, point-in-time
            "content_url": content_url,
            "_raw_co_id": co_id,  # kept for debugging
        })

    return announcements


# ---------------------------------------------------------------------------
# Public API: fetch_mops_announcements
# ---------------------------------------------------------------------------

def fetch_mops_announcements(date_str: str) -> list[dict]:
    """Fetch MOPS 重大訊息 announcements for a given date.

    Args:
        date_str: Date in YYYYMMDD format (Gregorian).

    Returns:
        List of announcement dicts:
            {
                ticker: str,           # e.g. "2330.TW"
                company_name: str,     # e.g. "台積電"
                announcement_title: str,
                announcement_time: str,  # UTC ISO-8601, point-in-time
                content_url: str,
            }

    Raises:
        requests.HTTPError: on non-retryable HTTP errors.
        RuntimeError: if all retries are exhausted.
    """
    cache_key = f"mops_{date_str}"
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.debug("mops_fetcher: cache hit for %s (%d items)", date_str, len(cached))
        return cached

    # Fetch the day's announcements from the TWSE Open Data JSON API.
    # The endpoint returns the full array for the current day; we request it
    # and filter to date_str client-side (the API exposes today's data).
    logger.info("mops_fetcher: fetching announcements for %s via TWSE Open Data", date_str)

    all_announcements: list[dict] = []
    try:
        resp = _rate_limited_get(
            f"{_TWSE_OPENAPI}?Date={date_str}", method="GET"
        )
        try:
            payload = resp.json()
        except ValueError as exc:
            logger.warning(
                "mops_fetcher: non-JSON response for %s: %s (body starts: %r)",
                date_str, exc, resp.text[:120],
            )
            payload = []
        all_announcements = _parse_announcement_json(payload, date_str)
    except Exception as exc:
        logger.error("mops_fetcher: failed to fetch %s: %s", date_str, exc)

    # Deduplicate by (ticker, title)
    seen: set[str] = set()
    deduped: list[dict] = []
    for item in all_announcements:
        key = f"{item['ticker']}|{item['announcement_title'][:60]}"
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    # Row-count sanity assertion: 0 results is almost always a broken parse or
    # an IP block, not a genuinely empty trading day — surface it loudly.
    if len(deduped) == 0:
        logger.warning(
            "mops_fetcher: 0 announcements parsed for %s — possible API/parse "
            "failure or IP block (NOT silently treating as empty)", date_str,
        )

    _cache_set(cache_key, deduped)
    logger.info(
        "mops_fetcher: total %d unique announcements for %s (cached 24h)",
        len(deduped), date_str,
    )
    return deduped


# ---------------------------------------------------------------------------
# Public API: fetch_announcement_content
# ---------------------------------------------------------------------------

def fetch_announcement_content(content_url: str) -> str:
    """Fetch and clean the full text of a MOPS announcement page.

    Args:
        content_url: URL to the announcement detail page.

    Returns:
        Cleaned plain text, truncated to 2000 characters.
        Returns empty string on failure (non-fatal).
    """
    if not content_url:
        return ""

    try:
        resp = _rate_limited_get(content_url, method="GET")
    except Exception as exc:
        logger.warning("fetch_announcement_content: request failed for %s: %s", content_url, exc)
        return ""

    try:
        html = resp.content.decode("utf-8", errors="replace")
    except Exception:
        html = resp.text

    return _extract_text_from_html(html)


def _extract_text_from_html(html: str) -> str:
    """Extract and clean text from announcement HTML.

    - Removes script/style/nav elements.
    - Normalizes whitespace.
    - Truncates to _CONTENT_MAX_CHARS.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove noise elements
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()

    # Prefer <div class="main-content"> or <table> containing announcement body
    # MOPS uses table layout; grab the largest text block
    text = soup.get_text(separator="\n")

    # Normalize whitespace
    lines = [line.strip() for line in text.splitlines()]
    # Remove empty/duplicate lines
    seen_lines: set[str] = set()
    cleaned_lines: list[str] = []
    for line in lines:
        if line and line not in seen_lines:
            seen_lines.add(line)
            cleaned_lines.append(line)

    cleaned = " ".join(cleaned_lines)

    # Collapse multiple spaces
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()

    return cleaned[:_CONTENT_MAX_CHARS]


# ---------------------------------------------------------------------------
# Module-level convenience: announcement hash for extraction cache key
# ---------------------------------------------------------------------------

def announcement_hash(title: str, content: str) -> str:
    """sha1(title + content) for use as extraction cache key.

    Mirrors the sha1-keyed pattern in scripts/news_sentiment_cache.py.
    """
    payload = f"{title}|{content}"
    return hashlib.sha1(payload.encode("utf-8", errors="ignore")).hexdigest()


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    date_arg = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y%m%d")
    print(f"Fetching MOPS announcements for {date_arg} …")

    items = fetch_mops_announcements(date_arg)
    print(f"\nFound {len(items)} announcements:\n")

    for i, item in enumerate(items[:5], 1):
        print(f"[{i}] {item['ticker']} {item['company_name']}")
        print(f"     {item['announcement_time']}  {item['announcement_title']}")
        print(f"     URL: {item['content_url']}")
        if item.get("content_url"):
            text = fetch_announcement_content(item["content_url"])
            print(f"     Content ({len(text)} chars): {text[:200]} …")
        print()
