"""
ReviewLedger · Core Data Models
All entities scraped, classified, and stored by the system.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from enum import Enum


# ── ENUMS ────────────────────────────────────────────────────────────────────

class Sentiment(str, Enum):
    PAIN    = "pain"      # complaint / negative
    PRAISE  = "praise"    # positive / compliment
    NEUTRAL = "neutral"   # mixed or no signal
    FEATURE = "feature"   # feature request


class SignalType(str, Enum):
    PAIN      = "pain"      # competitor weakness → your opportunity
    PRAISE    = "praise"    # competitor strength → your benchmark
    TREND     = "trend"     # pattern shift → early warning
    ALERT     = "alert"     # sudden spike or drop → act now


class TopicCluster(str, Enum):
    SUPPORT      = "support"
    PRICING      = "pricing"
    FEATURES     = "features"
    ONBOARDING   = "onboarding"
    RELIABILITY  = "reliability"
    PERFORMANCE  = "performance"
    INTEGRATION  = "integration"
    DOCUMENTATION= "documentation"
    UX           = "ux"
    SECURITY     = "security"
    OTHER        = "other"


class Platform(str, Enum):
    G2          = "g2"
    CAPTERRA    = "capterra"
    TRUSTPILOT  = "trustpilot"
    GOOGLE      = "google"
    APPSTORE    = "appstore"
    PLAYSTORE   = "playstore"
    REDDIT      = "reddit"
    YELP        = "yelp"
    TRIPADVISOR = "tripadvisor"
    GLASSDOOR   = "glassdoor"
    PRODUCTHUNT = "producthunt"


# ── CORE DATA MODELS ─────────────────────────────────────────────────────────

@dataclass
class RawReview:
    """A review exactly as scraped — before any AI processing."""
    review_id:       str
    platform:        Platform
    competitor_name: str
    competitor_slug: str       # normalized e.g. "acme-corp"
    rating:          float     # 1.0 – 5.0
    title:           str
    body:            str
    author:          str
    author_role:     Optional[str]   # e.g. "VP of Sales"
    author_company:  Optional[str]
    author_company_size: Optional[str]  # e.g. "51-200 employees"
    review_date:     datetime
    platform_url:    str
    scraped_at:      datetime = field(default_factory=datetime.utcnow)
    raw_html_hash:   str = ""  # for dedup


@dataclass
class ClassifiedReview:
    """A review after AI classification — ready for intelligence surfacing."""
    review_id:       str
    platform:        Platform
    competitor_name: str
    competitor_slug: str
    rating:          float
    title:           str
    body:            str
    author:          str
    author_role:     Optional[str]
    author_company:  Optional[str]
    author_company_size: Optional[str]
    review_date:     datetime
    platform_url:    str
    scraped_at:      datetime

    # AI classification outputs
    sentiment:       Sentiment
    topics:          list[TopicCluster]      # can be multiple
    intensity_score: float                   # 0.0 – 1.0 (how strong the signal)
    feature_requests: list[str]              # extracted feature asks
    key_phrases:     list[str]               # notable quoted phrases
    summary:         str                     # 1-sentence AI summary
    classified_at:   datetime = field(default_factory=datetime.utcnow)


@dataclass
class Signal:
    """A processed intelligence signal surfaced to the customer dashboard."""
    signal_id:       str
    signal_type:     SignalType
    competitor_slug: str
    competitor_name: str
    topic:           TopicCluster
    headline:        str             # e.g. "Onboarding complaints up 64% in 60 days"
    body:            str             # full narrative
    evidence:        list[str]       # supporting review excerpts (paraphrased)
    intensity:       float           # 0.0 – 1.0
    review_count:    int             # reviews that drove this signal
    period_days:     int             # lookback window
    generated_at:    datetime = field(default_factory=datetime.utcnow)
    is_alert:        bool = False    # triggers immediate notification


@dataclass
class CompetitorProfile:
    """A tracked competitor with aggregated stats."""
    slug:            str
    name:            str
    platforms:       list[Platform]
    platform_urls:   dict            # platform -> url
    overall_rating:  float
    total_reviews:   int
    review_velocity: float           # reviews per week (30-day avg)
    top_complaints:  list[str]       # top 3 complaint categories
    top_praises:     list[str]       # top 3 praise categories
    sentiment_score: float           # -1.0 to 1.0
    last_scraped:    Optional[datetime]
    created_at:      datetime = field(default_factory=datetime.utcnow)


@dataclass
class ScrapeJob:
    """A single scheduled scrape task."""
    job_id:          str
    platform:        Platform
    competitor_slug: str
    target_url:      str
    status:          str = "pending"   # pending / running / done / failed
    reviews_found:   int = 0
    reviews_new:     int = 0
    error_message:   Optional[str] = None
    started_at:      Optional[datetime] = None
    completed_at:    Optional[datetime] = None
