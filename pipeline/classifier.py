"""
ReviewLedger · AI Classification Pipeline

Takes raw scraped reviews and runs them through an LLM classifier to extract:
  - Sentiment (pain / praise / neutral / feature)
  - Topic clusters (support, pricing, features, onboarding, etc.)
  - Intensity score (0.0 – 1.0)
  - Feature requests (extracted asks)
  - Key phrases (notable quotes, paraphrased)
  - One-sentence summary

Calls the Anthropic Claude API (claude-sonnet-4-6).
Processes reviews in batches to minimize API calls and cost.
Includes retry logic and cost tracking.
"""

import json
import logging
import time
import os
import re
import uuid
from datetime import datetime
from typing import Optional

import requests

import sys
sys.path.insert(0, os.path.dirname(__file__))

from models import (
    RawReview, ClassifiedReview,
    Sentiment, TopicCluster, Signal, SignalType
)

logger = logging.getLogger(__name__)


# ── COST TRACKING ─────────────────────────────────────────────────────────────

_api_calls    = 0
_tokens_in    = 0
_tokens_out   = 0
COST_PER_1K_IN  = 0.003   # claude-sonnet-4-6 input $/1K tokens (approx)
COST_PER_1K_OUT = 0.015   # claude-sonnet-4-6 output $/1K tokens (approx)

def get_cost_estimate() -> dict:
    return {
        "api_calls":     _api_calls,
        "tokens_in":     _tokens_in,
        "tokens_out":    _tokens_out,
        "est_cost_usd":  round(
            (_tokens_in / 1000 * COST_PER_1K_IN) +
            (_tokens_out / 1000 * COST_PER_1K_OUT), 4
        )
    }


# ── CLASSIFICATION PROMPT ─────────────────────────────────────────────────────

CLASSIFY_SYSTEM = """You are a competitive intelligence analyst. 
You classify customer reviews of software products and services to extract structured competitive signals.
You always respond with valid JSON only. No preamble, no explanation, no markdown fences."""

CLASSIFY_PROMPT_TEMPLATE = """Classify this customer review and return a JSON object.

REVIEW:
Platform: {platform}
Company reviewed: {competitor_name}
Rating: {rating}/5
Title: {title}
Body: {body}

Return exactly this JSON structure:
{{
  "sentiment": "<pain|praise|neutral|feature>",
  "topics": ["<one or more of: support|pricing|features|onboarding|reliability|performance|integration|documentation|ux|security|other>"],
  "intensity_score": <float 0.0-1.0, how strong/intense the signal is>,
  "feature_requests": ["<extracted feature requests as short phrases, empty array if none>"],
  "key_phrases": ["<2-4 notable phrases from the review that capture the essence, paraphrased not quoted verbatim>"],
  "summary": "<one sentence summarizing the core signal of this review>"
}}

Rules:
- sentiment "pain" = negative experience, complaint, frustration
- sentiment "praise" = positive experience, recommendation, satisfaction  
- sentiment "feature" = primarily requesting missing functionality
- sentiment "neutral" = mixed or factual without strong signal
- intensity_score: 0.1=mild mention, 0.5=moderate, 0.9+=extremely strong
- topics must be from the allowed list only
- key_phrases should paraphrase the reviewer's words, not copy them verbatim
- summary must be one sentence under 20 words"""


BATCH_CLASSIFY_PROMPT = """Classify each of these {n} customer reviews and return a JSON array.

{reviews_block}

Return a JSON array of {n} objects, one per review, in the same order.
Each object must have these exact fields:
{{
  "sentiment": "<pain|praise|neutral|feature>",
  "topics": ["<support|pricing|features|onboarding|reliability|performance|integration|documentation|ux|security|other>"],
  "intensity_score": <float 0.0-1.0>,
  "feature_requests": ["<short phrases>"],
  "key_phrases": ["<2-4 paraphrased phrases>"],
  "summary": "<one sentence under 20 words>"
}}

Rules:
- Return ONLY the JSON array, nothing else
- Exactly {n} objects in the array
- All topics must be from the allowed list"""


# ── SIGNAL GENERATION PROMPT ──────────────────────────────────────────────────

SIGNAL_PROMPT = """You are a competitive intelligence analyst generating strategic signals from review data.

COMPETITOR: {competitor_name}
TOPIC: {topic}
PERIOD: Last {days} days
REVIEW COUNT IN THIS TOPIC: {count}
SENTIMENT BREAKDOWN: {pain_count} pain, {praise_count} praise, {neutral_count} neutral

REPRESENTATIVE REVIEWS:
{review_samples}

Generate a strategic intelligence signal as JSON:
{{
  "signal_type": "<pain|praise|trend|alert>",
  "headline": "<under 15 words — specific, data-driven>",
  "body": "<2-3 sentences explaining what this means and why it matters strategically>",
  "evidence": ["<2-3 paraphrased supporting observations from the reviews>"],
  "intensity": <float 0.0-1.0>,
  "is_alert": <true if this requires immediate attention, false otherwise>
}}

is_alert = true when:
- Pain volume spiked >40% vs previous period
- Rating dropped >0.5 stars in 30 days
- A new complaint cluster emerged that didn't exist before
- A crisis-level event is indicated

Return ONLY the JSON object."""


# ── ANTHROPIC API CALLER ──────────────────────────────────────────────────────

def _call_claude(
    messages: list[dict],
    system: str,
    max_tokens: int = 1000,
    retries: int = 3,
) -> Optional[str]:
    """
    Call the Anthropic Claude API directly via requests.
    No SDK dependency — just the REST API.
    """
    global _api_calls, _tokens_in, _tokens_out

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set — cannot classify")
        return None

    headers = {
        "Content-Type":         "application/json",
        "x-api-key":            api_key,
        "anthropic-version":    "2023-06-01",
    }

    payload = {
        "model":      "claude-sonnet-4-6",
        "max_tokens": max_tokens,
        "system":     system,
        "messages":   messages,
    }

    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
                timeout=30,
            )

            if resp.status_code == 200:
                data = resp.json()
                _api_calls += 1

                usage = data.get("usage", {})
                _tokens_in  += usage.get("input_tokens", 0)
                _tokens_out += usage.get("output_tokens", 0)

                content = data.get("content", [])
                for block in content:
                    if block.get("type") == "text":
                        return block["text"]
                return None

            elif resp.status_code == 429:
                wait = (2 ** attempt) * 10
                logger.warning("Claude API rate limited — waiting %ds", wait)
                time.sleep(wait)

            elif resp.status_code in (400, 401, 403):
                logger.error("Claude API auth/request error %d: %s",
                             resp.status_code, resp.text[:200])
                return None

            else:
                wait = 2 ** attempt
                logger.warning("Claude API HTTP %d (attempt %d) — retrying in %ds",
                               resp.status_code, attempt, wait)
                time.sleep(wait)

        except requests.Timeout:
            logger.warning("Claude API timeout (attempt %d/%d)", attempt, retries)
            time.sleep(5)
        except Exception as e:
            logger.error("Claude API unexpected error: %s", e)
            return None

    return None


def _parse_json_response(text: str) -> Optional[dict]:
    """Safely parse JSON from Claude's response, stripping any markdown."""
    if not text:
        return None
    # Strip markdown code fences
    text = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.MULTILINE)
    text = re.sub(r'\s*```$', '', text.strip(), flags=re.MULTILINE)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("JSON parse error: %s | Raw: %s", e, text[:200])
        return None


# ── SINGLE REVIEW CLASSIFIER ──────────────────────────────────────────────────

def classify_review(raw: RawReview) -> Optional[ClassifiedReview]:
    """Classify a single raw review. Use batch_classify for efficiency."""
    prompt = CLASSIFY_PROMPT_TEMPLATE.format(
        platform       = raw.platform.value,
        competitor_name= raw.competitor_name,
        rating         = raw.rating,
        title          = raw.title or "",
        body           = raw.body[:1500],  # truncate very long reviews
    )

    response = _call_claude(
        messages=[{"role": "user", "content": prompt}],
        system=CLASSIFY_SYSTEM,
        max_tokens=500,
    )

    if not response:
        return None

    data = _parse_json_response(response)
    if not data or not isinstance(data, dict):
        return None

    return _build_classified_review(raw, data)


# ── BATCH CLASSIFIER (preferred — more efficient) ─────────────────────────────

def batch_classify(
    reviews: list[RawReview],
    batch_size: int = 10,
) -> list[ClassifiedReview]:
    """
    Classify a list of reviews in batches.
    Default batch_size=10 balances cost vs latency.
    """
    classified = []

    for i in range(0, len(reviews), batch_size):
        batch = reviews[i:i + batch_size]
        logger.info("Classifying batch %d/%d (%d reviews)",
                    i // batch_size + 1,
                    (len(reviews) + batch_size - 1) // batch_size,
                    len(batch))

        results = _classify_batch(batch)
        classified.extend(results)

        # Small delay between batches
        if i + batch_size < len(reviews):
            time.sleep(1.0)

    logger.info("Batch classification complete: %d/%d classified", len(classified), len(reviews))
    return classified


def _classify_batch(batch: list[RawReview]) -> list[ClassifiedReview]:
    """Classify a batch of reviews in one API call."""
    reviews_block = "\n\n".join([
        f"REVIEW {idx+1}:\n"
        f"Platform: {r.platform.value}\n"
        f"Company: {r.competitor_name}\n"
        f"Rating: {r.rating}/5\n"
        f"Title: {r.title or '(no title)'}\n"
        f"Body: {r.body[:600]}"
        for idx, r in enumerate(batch)
    ])

    prompt = BATCH_CLASSIFY_PROMPT.format(
        n=len(batch),
        reviews_block=reviews_block,
    )

    response = _call_claude(
        messages=[{"role": "user", "content": prompt}],
        system=CLASSIFY_SYSTEM,
        max_tokens=len(batch) * 200,  # Scale with batch size
    )

    if not response:
        logger.warning("Batch classification returned no response — falling back to single")
        return [r for r in [classify_review(rev) for rev in batch] if r]

    data = _parse_json_response(response)
    if not data or not isinstance(data, list):
        logger.warning("Batch response not a list — falling back to single")
        return [r for r in [classify_review(rev) for rev in batch] if r]

    results = []
    for raw, classification in zip(batch, data):
        try:
            classified = _build_classified_review(raw, classification)
            if classified:
                results.append(classified)
        except Exception as e:
            logger.debug("Error building ClassifiedReview: %s", e)

    return results


def _build_classified_review(raw: RawReview, data: dict) -> Optional[ClassifiedReview]:
    """Convert raw review + classification dict → ClassifiedReview."""
    try:
        sentiment_str = data.get("sentiment", "neutral").lower()
        try:
            sentiment = Sentiment(sentiment_str)
        except ValueError:
            sentiment = Sentiment.NEUTRAL

        topics_raw = data.get("topics", ["other"])
        topics = []
        for t in topics_raw:
            try:
                topics.append(TopicCluster(t.lower()))
            except ValueError:
                topics.append(TopicCluster.OTHER)
        if not topics:
            topics = [TopicCluster.OTHER]

        intensity = float(data.get("intensity_score", 0.5))
        intensity = max(0.0, min(1.0, intensity))

        return ClassifiedReview(
            review_id           = raw.review_id,
            platform            = raw.platform,
            competitor_name     = raw.competitor_name,
            competitor_slug     = raw.competitor_slug,
            rating              = raw.rating,
            title               = raw.title,
            body                = raw.body,
            author              = raw.author,
            author_role         = raw.author_role,
            author_company      = raw.author_company,
            author_company_size = raw.author_company_size,
            review_date         = raw.review_date,
            platform_url        = raw.platform_url,
            scraped_at          = raw.scraped_at,
            sentiment           = sentiment,
            topics              = topics,
            intensity_score     = intensity,
            feature_requests    = data.get("feature_requests", []),
            key_phrases         = data.get("key_phrases", []),
            summary             = data.get("summary", ""),
            classified_at       = datetime.utcnow(),
        )
    except Exception as e:
        logger.error("Error building ClassifiedReview: %s", e)
        return None


# ── SIGNAL GENERATOR ──────────────────────────────────────────────────────────

def generate_signal(
    competitor_name: str,
    competitor_slug: str,
    topic: TopicCluster,
    reviews: list[ClassifiedReview],
    days: int = 30,
) -> Optional[Signal]:
    """
    Generate a strategic intelligence signal from a set of classified reviews
    about the same competitor and topic.
    """
    if not reviews:
        return None

    pain_count    = sum(1 for r in reviews if r.sentiment == Sentiment.PAIN)
    praise_count  = sum(1 for r in reviews if r.sentiment == Sentiment.PRAISE)
    neutral_count = len(reviews) - pain_count - praise_count

    # Build representative samples (top 5 by intensity)
    top_reviews = sorted(reviews, key=lambda r: r.intensity_score, reverse=True)[:5]
    review_samples = "\n\n".join([
        f"Rating: {r.rating}/5 | Intensity: {r.intensity_score:.1f}\n"
        f"Summary: {r.summary}\n"
        f"Key phrases: {', '.join(r.key_phrases[:2])}"
        for r in top_reviews
    ])

    prompt = SIGNAL_PROMPT.format(
        competitor_name = competitor_name,
        topic           = topic.value,
        days            = days,
        count           = len(reviews),
        pain_count      = pain_count,
        praise_count    = praise_count,
        neutral_count   = neutral_count,
        review_samples  = review_samples,
    )

    response = _call_claude(
        messages=[{"role": "user", "content": prompt}],
        system=CLASSIFY_SYSTEM,
        max_tokens=400,
    )

    if not response:
        return None

    data = _parse_json_response(response)
    if not data or not isinstance(data, dict):
        return None

    try:
        signal_type_str = data.get("signal_type", "trend").lower()
        try:
            signal_type = SignalType(signal_type_str)
        except ValueError:
            signal_type = SignalType.TREND

        avg_intensity = sum(r.intensity_score for r in reviews) / len(reviews)
        intensity = float(data.get("intensity", avg_intensity))

        return Signal(
            signal_id       = str(uuid.uuid4()),
            signal_type     = signal_type,
            competitor_slug = competitor_slug,
            competitor_name = competitor_name,
            topic           = topic,
            headline        = data.get("headline", f"{topic.value} signal for {competitor_name}"),
            body            = data.get("body", ""),
            evidence        = data.get("evidence", []),
            intensity       = max(0.0, min(1.0, intensity)),
            review_count    = len(reviews),
            period_days     = days,
            generated_at    = datetime.utcnow(),
            is_alert        = bool(data.get("is_alert", False)),
        )
    except Exception as e:
        logger.error("Error building Signal: %s", e)
        return None
