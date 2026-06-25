"""
ReviewLedger · Industry-Specific Source Discovery

Uses AI to:
1. Detect the industry from company name + aliases
2. Return the most relevant review platforms for that industry
3. Provide search URL templates for each platform

Covers 20+ industries with platform-specific scrapers where possible,
falling back to Playwright-based generic scraping for niche platforms.
"""

import os
import json
import logging
import re
import time
import random
from typing import Optional
from urllib.parse import quote_plus

import requests

logger = logging.getLogger(__name__)


# ── INDUSTRY → PLATFORM MAPPING ──────────────────────────────────────────────
# Each entry: list of (platform_id, platform_name, url_template, scrape_type)
# scrape_type: "generic_playwright" | "custom" | "skip"

INDUSTRY_PLATFORMS = {

    "financial_services": [
        ("trustpilot",   "Trustpilot",          "https://www.trustpilot.com/review/{domain}",        "trustpilot"),
        ("bbb",          "BBB",                  "https://www.bbb.org/search?find_text={name}",        "bbb"),
        ("google",       "Google Reviews",       "https://maps.google.com/?q={name}+reviews",          "google"),
        ("reddit",       "Reddit",               "{name}",                                              "reddit"),
        ("glassdoor",    "Glassdoor",            "https://www.glassdoor.com/Reviews/{name}-reviews",   "generic"),
    ],

    "banking_mortgage": [
        ("trustpilot",   "Trustpilot",           "https://www.trustpilot.com/review/{domain}",        "trustpilot"),
        ("bbb",          "BBB",                  "https://www.bbb.org/search?find_text={name}",        "bbb"),
        ("google",       "Google Reviews",       "https://maps.google.com/?q={name}+reviews",          "google"),
        ("reddit",       "Reddit",               "{name}",                                              "reddit"),
        ("ratehub",      "RateHub",              "https://www.ratehub.ca/mortgages/lenders/{slug}",    "generic"),
    ],

    "saas_software": [
        ("g2",           "G2",                   "https://www.g2.com/products/{slug}/reviews",         "g2"),
        ("capterra",     "Capterra",             "https://www.capterra.com/p/{slug}/",                 "generic"),
        ("trustpilot",   "Trustpilot",           "https://www.trustpilot.com/review/{domain}",        "trustpilot"),
        ("reddit",       "Reddit",               "{name}",                                              "reddit"),
        ("producthunt",  "Product Hunt",         "https://www.producthunt.com/search?q={name}",        "generic"),
        ("appstore",     "App Store",            "https://apps.apple.com/search?term={name}",          "generic"),
        ("playstore",    "Google Play",          "https://play.google.com/store/search?q={name}",      "generic"),
    ],

    "ecommerce_retail": [
        ("trustpilot",   "Trustpilot",           "https://www.trustpilot.com/review/{domain}",        "trustpilot"),
        ("google",       "Google Reviews",       "https://maps.google.com/?q={name}+reviews",          "google"),
        ("bbb",          "BBB",                  "https://www.bbb.org/search?find_text={name}",        "bbb"),
        ("reddit",       "Reddit",               "{name}",                                              "reddit"),
        ("sitejabber",   "SiteJabber",           "https://www.sitejabber.com/reviews/{domain}",        "generic"),
    ],

    "restaurant_food": [
        ("trustpilot",   "Trustpilot",           "https://www.trustpilot.com/review/{domain}",         "trustpilot"),
        ("reddit",       "Reddit",               "{name}",                                              "reddit"),
        ("yelp",         "Yelp",                 "https://www.yelp.ca/search?find_desc={name}",        "generic"),
        ("tripadvisor",  "TripAdvisor",          "https://www.tripadvisor.ca/Search?q={name}",         "generic"),
        ("bbb",          "BBB",                  "https://www.bbb.org/search?find_text={name}",        "bbb"),
        ("google",       "Google Reviews",       "https://maps.google.com/?q={name}+reviews",          "google"),
    ],

    "healthcare_medical": [
        ("google",       "Google Reviews",       "https://maps.google.com/?q={name}+reviews",          "google"),
        ("healthgrades",  "Healthgrades",        "https://www.healthgrades.com/search#what={name}",    "generic"),
        ("ratemds",      "RateMDs",              "https://www.ratemds.com/search/?q={name}",           "generic"),
        ("bbb",          "BBB",                  "https://www.bbb.org/search?find_text={name}",        "bbb"),
        ("yelp",         "Yelp",                 "https://www.yelp.ca/search?find_desc={name}+medical","generic"),
    ],

    "real_estate": [
        ("google",       "Google Reviews",       "https://maps.google.com/?q={name}+reviews",          "google"),
        ("trustpilot",   "Trustpilot",           "https://www.trustpilot.com/review/{domain}",        "trustpilot"),
        ("bbb",          "BBB",                  "https://www.bbb.org/search?find_text={name}",        "bbb"),
        ("reddit",       "Reddit",               "{name}",                                              "reddit"),
        ("realtor",      "Realtor Reviews",      "https://www.realtor.ca/agent/search#{name}",         "generic"),
        ("zillow",       "Zillow",               "https://www.zillow.com/professionals/search#{name}", "generic"),
    ],

    "home_services": [
        ("google",       "Google Reviews",       "https://maps.google.com/?q={name}+reviews",          "google"),
        ("houzz",        "Houzz",                "https://www.houzz.com/professionals/{slug}",          "generic"),
        ("bbb",          "BBB",                  "https://www.bbb.org/search?find_text={name}",        "bbb"),
        ("yelp",         "Yelp",                 "https://www.yelp.ca/search?find_desc={name}",        "generic"),
        ("homeadvisor",  "HomeAdvisor",          "https://www.homeadvisor.com/rated.{slug}.html",      "generic"),
        ("trustpilot",   "Trustpilot",           "https://www.trustpilot.com/review/{domain}",        "trustpilot"),
    ],

    "insurance": [
        ("trustpilot",   "Trustpilot",           "https://www.trustpilot.com/review/{domain}",        "trustpilot"),
        ("bbb",          "BBB",                  "https://www.bbb.org/search?find_text={name}",        "bbb"),
        ("google",       "Google Reviews",       "https://maps.google.com/?q={name}+reviews",          "google"),
        ("reddit",       "Reddit",               "{name}+insurance",                                    "reddit"),
        ("insureye",     "InsureEye",            "https://www.insureye.com/search/{slug}",              "generic"),
    ],

    "automotive": [
        ("google",       "Google Reviews",       "https://maps.google.com/?q={name}+reviews",          "google"),
        ("dealerrater",  "DealerRater",          "https://www.dealerrater.com/dealer/{slug}",           "generic"),
        ("bbb",          "BBB",                  "https://www.bbb.org/search?find_text={name}",        "bbb"),
        ("trustpilot",   "Trustpilot",           "https://www.trustpilot.com/review/{domain}",        "trustpilot"),
        ("cargurus",     "CarGurus",             "https://www.cargurus.com/Cars/dealerships#{name}",   "generic"),
    ],

    "hospitality_travel": [
        ("tripadvisor",  "TripAdvisor",          "https://www.tripadvisor.ca/Search?q={name}",         "generic"),
        ("google",       "Google Reviews",       "https://maps.google.com/?q={name}+reviews",          "google"),
        ("booking",      "Booking.com",          "https://www.booking.com/searchresults.html?ss={name}","generic"),
        ("yelp",         "Yelp",                 "https://www.yelp.ca/search?find_desc={name}",        "generic"),
        ("trustpilot",   "Trustpilot",           "https://www.trustpilot.com/review/{domain}",        "trustpilot"),
    ],

    "legal_professional": [
        ("google",       "Google Reviews",       "https://maps.google.com/?q={name}+reviews",          "google"),
        ("bbb",          "BBB",                  "https://www.bbb.org/search?find_text={name}",        "bbb"),
        ("trustpilot",   "Trustpilot",           "https://www.trustpilot.com/review/{domain}",        "trustpilot"),
        ("avvo",         "Avvo",                 "https://www.avvo.com/search/lawyer_search.aspx?q={name}", "generic"),
        ("reddit",       "Reddit",               "{name}",                                              "reddit"),
    ],

    "education": [
        ("google",       "Google Reviews",       "https://maps.google.com/?q={name}+reviews",          "google"),
        ("trustpilot",   "Trustpilot",           "https://www.trustpilot.com/review/{domain}",        "trustpilot"),
        ("reddit",       "Reddit",               "{name}",                                              "reddit"),
        ("glassdoor",    "Glassdoor",            "https://www.glassdoor.com/Reviews/{slug}-reviews",   "generic"),
        ("coursereport", "Course Report",        "https://www.coursereport.com/schools/{slug}",         "generic"),
    ],

    "telecom": [
        ("trustpilot",   "Trustpilot",           "https://www.trustpilot.com/review/{domain}",        "trustpilot"),
        ("bbb",          "BBB",                  "https://www.bbb.org/search?find_text={name}",        "bbb"),
        ("reddit",       "Reddit",               "{name}",                                              "reddit"),
        ("google",       "Google Reviews",       "https://maps.google.com/?q={name}+reviews",          "google"),
        ("sitejabber",   "SiteJabber",           "https://www.sitejabber.com/reviews/{domain}",        "generic"),
    ],

    "gaming": [
        ("trustpilot",   "Trustpilot",           "https://www.trustpilot.com/review/{domain}",        "trustpilot"),
        ("reddit",       "Reddit",               "{name}",                                              "reddit"),
        ("google",       "Google Reviews",       "https://maps.google.com/?q={name}+reviews",          "google"),
        ("bbb",          "BBB",                  "https://www.bbb.org/search?find_text={name}",        "bbb"),
        ("sitejabber",   "SiteJabber",           "https://www.sitejabber.com/reviews/{domain}",        "generic"),
    ],

    # Default fallback for any industry
    "general": [
        ("trustpilot",   "Trustpilot",           "https://www.trustpilot.com/review/{domain}",        "trustpilot"),
        ("google",       "Google Reviews",       "https://maps.google.com/?q={name}+reviews",          "google"),
        ("bbb",          "BBB",                  "https://www.bbb.org/search?find_text={name}",        "bbb"),
        ("reddit",       "Reddit",               "{name}",                                              "reddit"),
        ("glassdoor",    "Glassdoor",            "https://www.glassdoor.com/Reviews/{slug}-reviews",   "generic"),
    ],
}

# Industry keyword hints for fallback classification
INDUSTRY_KEYWORDS = {
    "banking_mortgage":   ["bank","banking","mortgage","lender","lending","credit union","trust company","mic","financial","capital"],
    "saas_software":      ["software","saas","app","platform","tech","technology","digital","cloud","api","crm","erp"],
    "restaurant_food":    ["restaurant","food","cafe","coffee","bakery","pizza","sushi","catering","bistro","diner"],
    "healthcare_medical": ["health","medical","clinic","hospital","dental","pharmacy","physio","doctor","therapy","care"],
    "real_estate":        ["real estate","realty","realtor","property","homes","housing","brokerage","mls"],
    "home_services":      ["plumbing","electrical","hvac","roofing","contractor","renovation","landscaping","cleaning","repair"],
    "insurance":          ["insurance","insurer","underwriter","coverage","policy","claims"],
    "automotive":         ["auto","car","vehicle","dealership","dealer","mechanic","repair","tire"],
    "hospitality_travel": ["hotel","resort","travel","airline","airbnb","vacation","tourism","lodge"],
    "legal_professional": ["law","legal","lawyer","attorney","accounting","consulting","advisory","firm"],
    "education":          ["school","university","college","education","training","bootcamp","course","academy"],
    "telecom":            ["telecom","wireless","internet","cable","mobile","phone","network","isp"],
    "gaming":             ["game","games","gaming","studio","entertainment","esports","console","playstation","xbox","nintendo","steam","indie","blizzard","activision","ubisoft","rockstar","valve","riot","bungie","bethesda"],
    "ecommerce_retail":   ["store","shop","retail","ecommerce","marketplace","boutique","fashion","clothing"],
}


def detect_industry(company_name: str, aliases: list = None) -> str:
    """
    Use AI to detect the industry of a company.
    Falls back to keyword matching if no API key.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    combined = " ".join([company_name] + (aliases or [])).lower()

    if api_key:
        return _ai_detect_industry(company_name, aliases or [], api_key)
    else:
        return _keyword_detect_industry(combined)


def _ai_detect_industry(company_name: str, aliases: list, api_key: str) -> str:
    """Ask Claude to classify the industry."""
    industries = list(INDUSTRY_PLATFORMS.keys())
    prompt = f"""Classify this company into exactly one industry category.

Company: {company_name}
Also known as: {', '.join(aliases) if aliases else 'N/A'}

Industry categories (return exactly one):
{chr(10).join(f'- {i}' for i in industries)}

Return ONLY the category name, nothing else."""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 30,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=15
        )
        if resp.status_code == 200:
            result = resp.json()["content"][0]["text"].strip().lower().replace(" ", "_")
            if result in INDUSTRY_PLATFORMS:
                logger.info("[Industry] AI classified '%s' as: %s", company_name, result)
                return result
    except Exception as e:
        logger.debug("[Industry] AI detection error: %s", e)

    # Fallback to keyword
    combined = " ".join([company_name] + aliases).lower()
    return _keyword_detect_industry(combined)


def _keyword_detect_industry(text: str) -> str:
    """Keyword-based industry detection fallback."""
    text = text.lower()
    best_industry, best_score = "general", 0

    for industry, keywords in INDUSTRY_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > best_score:
            best_score, best_industry = score, industry

    logger.info("[Industry] Keyword classified as: %s (score %d)", best_industry, best_score)
    return best_industry


def get_platforms_for_industry(industry: str) -> list:
    """Return the platform list for a given industry."""
    return INDUSTRY_PLATFORMS.get(industry, INDUSTRY_PLATFORMS["general"])


def build_platform_urls(
    platform_list: list,
    company_name: str,
    domain: Optional[str],
    trustpilot_url: Optional[str],
) -> dict:
    """
    Build a dict of {platform_id: url} for a company,
    substituting name/domain/slug into templates.
    """
    slug = re.sub(r'[^a-z0-9]', '-', company_name.lower()).strip('-')
    slug_clean = re.sub(r'[^a-z0-9]', '', company_name.lower())
    domain_clean = domain or slug_clean + ".com"

    result = {}
    for (pid, pname, url_template, scrape_type) in platform_list:
        if scrape_type == "skip":
            continue

        # For Trustpilot, use the discovered URL if available
        if pid == "trustpilot" and trustpilot_url:
            result[pid] = trustpilot_url
            continue

        url = (
            url_template
            .replace("{name}", quote_plus(company_name))
            .replace("{slug}", slug)
            .replace("{domain}", domain_clean)
        )
        result[pid] = url

    return result
