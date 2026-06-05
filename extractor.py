"""Claude API event extraction for news_nlp_tw."""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

_PROMPT = """分析以下台股重大訊息，以JSON格式回覆：
公告內容：{content}

回覆格式（嚴格JSON，不含其他文字）：
{{"event_type": "earnings|guidance|MA|legal|dividend|restructuring|other", "sentiment": "positive|negative|neutral", "magnitude": 0-10, "summary_zh": "一句話摘要（20字以內）"}}"""


def extract_event(ticker: str, content: str, model: str = "claude-haiku-4-5-20251001") -> dict | None:
    """Extract structured event from announcement text via Claude API."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        msg = client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": _PROMPT.format(content=content[:1500])}],
        )
        raw = msg.content[0].text.strip()
        result = json.loads(raw)
        result["ticker"] = ticker
        result["magnitude"] = max(0, min(10, int(result.get("magnitude", 5))))
        return result
    except json.JSONDecodeError:
        logger.warning("JSON parse failed for %s", ticker)
        return None
    except Exception as e:
        logger.warning("extract_event failed for %s: %s", ticker, e)
        return None


def sentiment_to_score(event: dict) -> float:
    """Convert event dict to IC-trackable score ∈ [-1, 1]."""
    sign = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}.get(
        event.get("sentiment", "neutral"), 0.0
    )
    return sign * event.get("magnitude", 5) / 10.0
