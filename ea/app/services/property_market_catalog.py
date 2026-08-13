from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.parse
import unicodedata

from app.property_distance_preferences import (
    normalize_property_distance_importance,
    normalize_property_distance_preference_pairs,
    property_distance_importance_key,
    property_distance_preference_keys,
)


@dataclass(frozen=True)
class PropertyCountrySpec:
    code: str
    label: str
    default_language: str
    currency_code: str
    currency_symbol: str
    location_placeholder: str
    featured_platforms: tuple[str, ...]
    default_timezone: str = "UTC"


@dataclass(frozen=True)
class PropertyProviderSpec:
    key: str
    label: str
    country_code: str
    host_markers: tuple[str, ...]
    listing_path_markers: tuple[str, ...]
    search_urls: dict[str, str]
    description: str
    family: str = "marketplace"
    trust_tier: str = "standard"
    supported_listing_modes: tuple[str, ...] = ("rent", "buy")
    coverage: str = "regional"
    floorplan_reliability: str = "unknown"
    duplicate_rate: str = "unknown"
    tour_availability: str = "unknown"
    scan_reliability: str = "unknown"
    filter_pushdown_strength: str = "partial"
    official_source_quality: str = "provider_only"
    last_verified: str = "2026-06-13"
    search_ready: bool = True
    availability_note: str = ""
    market_readiness: str = "private_beta"
    access_mode: str = "public_web"
    official_api_available: bool = False
    browser_access_allowed: bool = False
    terms_review_status: str = "needs_review"
    robots_review_status: str = "needs_review"
    listing_cache_allowed: bool = False
    cache_ttl_seconds: int = 0
    photo_republication_allowed: bool = False
    floorplan_republication_allowed: bool = False
    public_packet_allowed: bool = False
    customer_packet_allowed: bool = True
    attribution_required: bool = True
    maximum_concurrency: int = 1
    requests_per_hour: int = 60
    operator_owner: str = "property-market-codex"
    last_rights_reviewed_at: str = ""
    supported_region_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PropertyEvidenceSourceSpec:
    key: str
    label: str
    country_code: str
    evidence_family: str
    description: str
    source_type: str = "official_public_data"
    confidence: str = "medium"
    last_verified: str = "2026-06-13"
    license_label: str = "source-specific public-data terms"
    refresh_cadence: str = "source-dependent"
    attribution_required: bool = True
    downstream_use: str = "evidence_only_no_raw_republication"
    geographic_granularity: str = "source_defined"


COUNTRIES: tuple[PropertyCountrySpec, ...] = (
    PropertyCountrySpec(
        "AT",
        "Austria",
        "de",
        "EUR",
        "EUR",
        "Vienna, Graz, Linz",
        (
            "willhaben",
            "immmo",
            "immoscout_at",
            "immobilien_net_at",
            "ohne_makler_at",
            "sreal_at",
            "raiffeisen_immobilien_at",
            "wohnnet_at",
            "keinmakler_at",
            "derstandard_at",
            "public_housing_at",
            "genossenschaften_at",
        ),
        "Europe/Vienna",
    ),
    PropertyCountrySpec("BE", "Belgium", "nl", "EUR", "EUR", "Brussels, Antwerp, Ghent", ("immoweb", "zimmo"), "Europe/Brussels"),
    PropertyCountrySpec("CA", "Canada", "en", "CAD", "CAD", "Toronto, Montreal, Vancouver", ("realtor_ca", "rew_ca", "rentals_ca"), "America/Toronto"),
    PropertyCountrySpec(
        "CR",
        "Costa Rica",
        "es",
        "CRC",
        "CRC",
        "All Costa Rica, Central Valley, Guanacaste, Puntarenas",
        (
            "encuentra24_cr",
            "re_cr_mls",
            "realtor_cr",
            "properstar_cr",
            "coldwellbanker_cr",
            "century21_cr",
            "remax_cr",
            "theagency_cr",
            "krain_cr",
            "desarrollos_cr",
            "propertiesincostarica_cr",
            "twocostaricarealestate_cr",
            "tierraverde_cr",
        ),
        "America/Costa_Rica",
    ),
    PropertyCountrySpec(
        "DE",
        "Germany",
        "de",
        "EUR",
        "EUR",
        "Berlin, Munich, Hamburg",
        (
            "core_portals_de",
            "wohnungsboerse_de",
            "shared_housing_de",
            "corporate_landlords_de",
            "municipal_housing_de",
            "new_build_de",
            "auctions_de",
        ),
        "Europe/Berlin",
    ),
    PropertyCountrySpec("CH", "Switzerland", "de", "CHF", "CHF", "Zurich, Geneva, Basel", ("homegate", "newhome", "immoscout_ch"), "Europe/Zurich"),
    PropertyCountrySpec("IE", "Ireland", "en", "EUR", "EUR", "Dublin, Cork, Galway", ("daft_ie", "myhome_ie"), "Europe/Dublin"),
    PropertyCountrySpec("UK", "United Kingdom", "en", "GBP", "GBP", "London, Manchester, Bristol", ("rightmove", "zoopla", "onthemarket"), "Europe/London"),
    PropertyCountrySpec("AU", "Australia", "en", "AUD", "AUD", "Sydney, Melbourne, Brisbane", ("realestate_au", "domain_au", "flatmates_au"), "Australia/Sydney"),
    PropertyCountrySpec("ES", "Spain", "es", "EUR", "EUR", "Barcelona, Madrid, Valencia", ("idealista_es", "fotocasa", "habitaclia"), "Europe/Madrid"),
    PropertyCountrySpec("IT", "Italy", "it", "EUR", "EUR", "Milan, Rome, Bologna", ("immobiliare", "idealista_it", "casa_it"), "Europe/Rome"),
    PropertyCountrySpec("FR", "France", "fr", "EUR", "EUR", "Paris, Lyon, Marseille", ("seloger", "bienici", "leboncoin_immo"), "Europe/Paris"),
    PropertyCountrySpec("NL", "Netherlands", "nl", "EUR", "EUR", "Amsterdam, Rotterdam, Utrecht", ("funda", "pararius"), "Europe/Amsterdam"),
    PropertyCountrySpec("PT", "Portugal", "pt", "EUR", "EUR", "Lisbon, Porto, Faro", ("idealista_pt", "imovirtual", "casa_sapo"), "Europe/Lisbon"),
    PropertyCountrySpec("PL", "Poland", "pl", "PLN", "PLN", "Warsaw, Krakow, Wroclaw", ("otodom", "olx_pl_nieruchomosci"), "Europe/Warsaw"),
    PropertyCountrySpec("SE", "Sweden", "sv", "SEK", "SEK", "Stockholm, Gothenburg, Malmo", ("hemnet", "booli"), "Europe/Stockholm"),
    PropertyCountrySpec("US", "United States", "en", "USD", "USD", "Brooklyn, Austin, Seattle", ("zillow", "realtor", "apartments"), "America/New_York"),
)


# Austria is the only current closed-test customer market. The full country and
# provider catalog stays available to operator/research tooling, but catalog
# presence must not make a market selectable in the customer product.
CUSTOMER_SEARCH_COUNTRY_ORDER: tuple[str, ...] = ("AT",)
CUSTOMER_SEARCH_COUNTRY_CODES: frozenset[str] = frozenset(CUSTOMER_SEARCH_COUNTRY_ORDER)


LANGUAGES: tuple[tuple[str, str], ...] = (
    ("en", "English"),
    ("de", "Deutsch"),
    ("fr", "Français"),
    ("es", "Español"),
    ("it", "Italiano"),
    ("nl", "Nederlands"),
    ("pt", "Português"),
    ("pl", "Polski"),
    ("sv", "Svenska"),
)


LISTING_MODE_LABELS = {
    "rent": "Rent",
    "buy": "Buy",
}

SEARCH_GOAL_LABELS = {
    "home": "Find a home",
    "investment": "Find an investment",
}

INVESTMENT_STRATEGY_LABELS = {
    "best_overall": "Best overall opportunity",
    "cash_flow": "Cash flow",
    "appreciation": "Appreciation",
    "undervalued": "Undervalued",
    "low_risk": "Low risk",
}


PROPERTY_TYPE_LABELS = {
    "any": "Any type",
    "apartment": "Apartment",
    "house": "House",
    "office": "Office",
    "land": "Building land",
}


ALERT_FREQUENCY_LABELS = {
    "manual": "Manual only",
    "daily": "Daily",
    "weekday": "Weekdays",
    "instant": "Instant",
}


ALERT_CHANNEL_KEYS = ("telegram", "email", "whatsapp")

INVESTMENT_RESEARCH_MODE_LABELS = {
    "off": "Off",
    "auto": "Investment research on buy listings",
}


PROVIDERS: tuple[PropertyProviderSpec, ...] = (
    PropertyProviderSpec(
        key="willhaben",
        label="Willhaben",
        country_code="AT",
        host_markers=("willhaben.at",),
        listing_path_markers=("/iad/immobilien/d/", "/iad/object"),
        search_urls={
            "rent": "https://www.willhaben.at/iad/immobilien/mietwohnungen",
            "buy": "https://www.willhaben.at/iad/immobilien/eigentumswohnung",
        },
        description="Austria broad-market marketplace with dense residential volume.",
    ),
    PropertyProviderSpec(
        key="immmo",
        label="immmo",
        country_code="AT",
        host_markers=("immmo.at",),
        listing_path_markers=("/expose/", "/immobilien/", "/detail/"),
        search_urls={
            "rent": "https://www.immmo.at/suche/miete",
            "buy": "https://www.immmo.at/suche/kauf",
        },
        description="Austria portal with residential search feeds and alert traffic.",
    ),
    PropertyProviderSpec(
        key="immoscout_at",
        label="ImmoScout24 Austria",
        country_code="AT",
        host_markers=("immoscout24.at", "immobilienscout24.at"),
        listing_path_markers=("/expose/", "/detail/", "/objekt/"),
        search_urls={
            "rent": "https://www.immoscout24.at/liste/miete",
            "buy": "https://www.immoscout24.at/liste/kauf",
        },
        description="Austria search portal for rentals and residential purchase.",
    ),
    PropertyProviderSpec(
        key="immobilien_net_at",
        label="immobilien.net",
        country_code="AT",
        host_markers=("immobilien.net", "www.immobilien.net"),
        listing_path_markers=("/expose/", "/immobilien/", "/immobiliensuche/", "/objekt/"),
        search_urls={
            "rent": "https://www.immobilien.net/immobiliensuche/wohnungen/mieten",
            "buy": "https://www.immobilien.net/immobiliensuche/wohnungen/kaufen",
        },
        description="Austria broad-market marketplace for residential rentals and purchases. Plain HTTP probes can be blocked, so this provider must stay under browser-backed repair and weekly health probing.",
        family="marketplace",
        trust_tier="standard",
        coverage="national",
        floorplan_reliability="medium",
        duplicate_rate="medium",
        tour_availability="occasional",
        scan_reliability="browser_required",
        filter_pushdown_strength="partial",
        official_source_quality="provider_only",
        last_verified="2026-06-29",
        access_mode="browser_public_web",
        browser_access_allowed=True,
        maximum_concurrency=1,
        requests_per_hour=30,
    ),
    PropertyProviderSpec(
        key="ohne_makler_at",
        label="ohne-makler.at",
        country_code="AT",
        host_markers=("ohne-makler.at", "www.ohne-makler.at"),
        listing_path_markers=("/immobilien/", "/immobilie/", "/wohnung-mieten/", "/wohnung-kaufen/"),
        search_urls={
            "rent": "https://www.ohne-makler.at/immobilien/",
            "buy": "https://www.ohne-makler.at/immobilien/wohnung-kaufen/",
        },
        description="Austria owner-direct and commission-free property portal for rentals and purchases.",
        family="broker_direct",
        trust_tier="standard",
        coverage="national",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="owner_direct",
        last_verified="2026-06-29",
    ),
    PropertyProviderSpec(
        key="sreal_at",
        label="s REAL",
        country_code="AT",
        host_markers=("sreal.at", "www.sreal.at"),
        listing_path_markers=("/de/immobilien-suche", "/de/wohnungen-miete/", "/de/wohnungen-kauf/", "/immobilien/"),
        search_urls={
            "rent": "https://www.sreal.at/de/immobilien-suche",
            "buy": "https://www.sreal.at/de/immobilien-suche",
        },
        description="Austria Sparkasse and Erste Group broker-direct property lane with national residential inventory.",
        family="broker_direct",
        trust_tier="standard",
        coverage="national",
        floorplan_reliability="medium",
        duplicate_rate="medium",
        tour_availability="occasional",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="broker_primary",
        last_verified="2026-06-29",
    ),
    PropertyProviderSpec(
        key="raiffeisen_immobilien_at",
        label="Raiffeisen Immobilien",
        country_code="AT",
        host_markers=("raiffeisen-immobilien.at", "www.raiffeisen-immobilien.at", "raiffeisen.at"),
        listing_path_markers=("/de/immobilien", "/immobiliensuche", "/immobilien/"),
        search_urls={
            "rent": "https://www.raiffeisen-immobilien.at/de/immobilien",
            "buy": "https://www.raiffeisen-immobilien.at/de/immobilien",
        },
        description="Austria Raiffeisen broker-direct property lane for residential rentals, purchases, and regional office inventory.",
        family="broker_direct",
        trust_tier="standard",
        coverage="national",
        floorplan_reliability="medium",
        duplicate_rate="medium",
        tour_availability="occasional",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="broker_primary",
        last_verified="2026-06-29",
    ),
    PropertyProviderSpec(
        key="wohnnet_at",
        label="Wohnnet Immobilien",
        country_code="AT",
        host_markers=("wohnnet.at", "www.wohnnet.at"),
        listing_path_markers=("/immobilien/", "/immobiliensuche/", "/objekt/", "/detail/"),
        search_urls={
            "rent": "https://www.wohnnet.at/immobilien/",
            "buy": "https://www.wohnnet.at/immobilien/",
        },
        description="Austria residential property and housing portal with search pages and market-facing property content.",
        family="marketplace",
        trust_tier="standard",
        coverage="national",
        floorplan_reliability="low",
        duplicate_rate="medium",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="provider_only",
        last_verified="2026-06-29",
    ),
    PropertyProviderSpec(
        key="keinmakler_at",
        label="keinmakler.at",
        country_code="AT",
        host_markers=("keinmakler.at", "www.keinmakler.at"),
        listing_path_markers=("/immobilien/", "/detail/", "/objekt/", "/wohnung"),
        search_urls={
            "rent": "https://www.keinmakler.at/",
            "buy": "https://www.keinmakler.at/",
        },
        description="Austria commission-free property source for owner-direct rental and purchase leads.",
        family="broker_direct",
        trust_tier="watch",
        coverage="national",
        floorplan_reliability="low",
        duplicate_rate="medium",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="weak",
        official_source_quality="owner_direct",
        last_verified="2026-06-29",
    ),
    PropertyProviderSpec(
        key="immowelt_at",
        label="immowelt Austria",
        country_code="AT",
        host_markers=("immowelt.at",),
        listing_path_markers=("/expose/", "/immobilien/", "/anzeige/"),
        search_urls={
            "rent": "https://www.immowelt.at/suche/wohnungen/mieten",
            "buy": "https://www.immowelt.at/suche/wohnungen/kaufen",
        },
        description="Austria immowelt residential marketplace for rent and purchase inventory.",
    ),
    PropertyProviderSpec(
        key="findmyhome_at",
        label="FindMyHome.at",
        country_code="AT",
        host_markers=("findmyhome.at",),
        listing_path_markers=("/immobilie/", "/objekt/", "/detail/", "entry="),
        search_urls={
            "rent": "https://www.findmyhome.at/immo/wohnung-mieten/wien",
            "buy": "https://www.findmyhome.at/immo/wohnung-kaufen/wien",
        },
        description="Austria quality-oriented property portal with residential rental and purchase listings.",
    ),
    PropertyProviderSpec(
        key="derstandard_at",
        label="DER STANDARD Immobilien",
        country_code="AT",
        host_markers=("immobilien.derstandard.at",),
        listing_path_markers=("/immobiliensuche/detail/", "/immobilien/detail/", "/detail/"),
        search_urls={
            "rent": "https://immobilien.derstandard.at/immobiliensuche/miete",
            "buy": "https://immobilien.derstandard.at/immobiliensuche/kauf",
        },
        description="DER STANDARD Austria real-estate portal for residential rental and purchase inventory.",
    ),
    PropertyProviderSpec(
        key="kalandra",
        label="Kalandra",
        country_code="AT",
        host_markers=("kalandra.at",),
        listing_path_markers=("/objekt/",),
        search_urls={
            "rent": "https://www.kalandra.at/immobiliensuche",
            "buy": "https://www.kalandra.at/immobiliensuche",
        },
        description="Austria brokerage inventory with high-value marketing packets.",
        family="broker_direct",
        trust_tier="standard",
    ),
    PropertyProviderSpec(
        key="remax_at",
        label="RE/MAX Austria",
        country_code="AT",
        host_markers=("remax.at",),
        listing_path_markers=("/en/im/", "/de/im/", "/en/g/", "/de/g/", "/en/ib/", "/de/ib/", "/properties/propertysearch"),
        search_urls={
            "rent": "https://www.remax.at/en/properties/propertysearch",
            "buy": "https://www.remax.at/en/properties/propertysearch",
        },
        description="Austria RE/MAX broker network property search for buy and rent inventory.",
        family="broker_direct",
        trust_tier="standard",
    ),
    PropertyProviderSpec(
        key="glorit_at",
        label="Glorit",
        country_code="AT",
        host_markers=("glorit.at", "www.glorit.at"),
        listing_path_markers=("/wohnung-kaufen/", "/wohnung-mieten/", "/immobilien/"),
        search_urls={
            "rent": "https://glorit.at/wohnung-mieten/",
            "buy": "https://glorit.at/wohnung-kaufen/",
        },
        description="Vienna developer-direct residential provider with first-party new-build purchase listings.",
        family="developer_projects",
        trust_tier="trusted",
        supported_listing_modes=("buy", "rent"),
        coverage="vienna",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="occasional",
        scan_reliability="browser_required",
        filter_pushdown_strength="weak",
        official_source_quality="developer_primary",
        last_verified="2026-07-09",
        access_mode="browser_public_web",
        browser_access_allowed=True,
        maximum_concurrency=1,
        requests_per_hour=20,
        supported_region_codes=("wien", "vienna", "at-9"),
    ),
    PropertyProviderSpec(
        key="genossenschaften_at",
        label="Genossenschaften",
        country_code="AT",
        host_markers=(
            "gesiba.at",
            "siedlungsunion.at",
            "sozialbau.at",
            "angebote.sozialbau.at",
            "wbv-gpa.at",
            "frieden.at",
            "egw.at",
            "oesw.at",
            "familienwohnbau.at",
            "wag.at",
            "heimat-oesterreich.at",
            "bwsg.at",
            "wiensued.at",
            "ebg-wohnen.at",
            "ooewohnbau.at",
            "salzburg-wohnbau.at",
            "oevw.at",
        ),
        listing_path_markers=(
            "/immobilien/wohnungen/objekt",
            "/wohnen/sofort/",
            "/sobitvx/htmlprospect/",
            "/wohnung/",
            "/immobiliensuche/",
            "/suche",
            "/immobilienangebot/",
            "/de/objekt/",
            "/de/immobilien/",
            "/immobilien/",
            "/objekte/",
            "/wohnbau-projekte/",
        ),
        search_urls={
            "rent": "https://www.gesiba.at/immobilien/wohnungen",
            "buy": "https://www.gesiba.at/immobilien/wohnungen",
        },
        description="Austria cooperative housing boards grouped into one crawl lane, including Gesiba, Siedlungsunion, Sozialbau, WBV-GPA, Frieden, EGW, ÖSW, and Familienwohnbau.",
        family="cooperative",
        trust_tier="trusted",
    ),
    PropertyProviderSpec(
        key="wohnberatung_wien",
        label="Wohnberatung Wien",
        country_code="AT",
        host_markers=("wien.gv.at",),
        listing_path_markers=("/wohnen/wohnbaufoerderung/wohnungssuche/", "/wohnungssuche/index.html"),
        search_urls={
            "rent": "https://www.wien.gv.at/wohnen/wohnbaufoerderung/wohnungssuche/index.html",
            "buy": "https://www.wien.gv.at/wohnen/wohnbaufoerderung/wohnungssuche/index.html",
        },
        description="Vienna public gateway for Gemeindewohnungen, subsidized rentals, cooperative housing, and subsidized ownership programs with eligibility rules.",
        family="public_housing",
        trust_tier="trusted",
        coverage="regional",
        floorplan_reliability="low",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="weak",
        official_source_quality="municipal_primary",
        supported_region_codes=("vienna",),
    ),
    PropertyProviderSpec(
        key="wiener_wohnen",
        label="Wiener Wohnen",
        country_code="AT",
        host_markers=("wien.gv.at", "wienerwohnen.at"),
        listing_path_markers=("/wohnen/gemeindewohnungen", "/gemeindewohnungen"),
        search_urls={
            "rent": "https://www.wien.gv.at/wohnen/gemeindewohnungen/",
        },
        description="Vienna municipal housing lane for Gemeindewohnung and Wiener Wohn-Ticket related supply and eligibility flows.",
        family="public_housing",
        trust_tier="trusted",
        supported_listing_modes=("rent",),
        coverage="regional",
        floorplan_reliability="low",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="weak",
        official_source_quality="municipal_primary",
        supported_region_codes=("vienna",),
    ),
    PropertyProviderSpec(
        key="gesiba_at",
        label="GESIBA",
        country_code="AT",
        host_markers=("gesiba.at",),
        listing_path_markers=("/immobilien/wohnungen", "/objekt"),
        search_urls={
            "rent": "https://www.gesiba.at/immobilien/wohnungen",
            "buy": "https://www.gesiba.at/immobilien/wohnungen",
        },
        description="Vienna non-profit builder and cooperative direct lane with project-stage, availability, and document-rich listing pages.",
        family="cooperative",
        trust_tier="trusted",
        coverage="regional",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="cooperative_primary",
        supported_region_codes=("vienna",),
    ),
    PropertyProviderSpec(
        key="oesw_at",
        label="ÖSW",
        country_code="AT",
        host_markers=("oesw.at",),
        listing_path_markers=("/immobilienangebot/", "/sofort-wohnen.html", "/in-bau.html"),
        search_urls={
            "rent": "https://www.oesw.at/immobilienangebot/sofort-wohnen.html",
            "buy": "https://www.oesw.at/immobilienangebot/sofort-wohnen.html",
        },
        description="Austria non-profit builder and subsidized or free-financed housing lane with rent, ownership, and project availability filters.",
        family="cooperative",
        trust_tier="trusted",
        coverage="multi_region",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="cooperative_primary",
        supported_region_codes=("upper_austria", "lower_austria", "salzburg", "styria"),
    ),
    PropertyProviderSpec(
        key="egw_at",
        label="EGW",
        country_code="AT",
        host_markers=("egw.at",),
        listing_path_markers=("/suche", "/projekte", "/wohnung"),
        search_urls={
            "rent": "https://www.egw.at/suche",
            "buy": "https://www.egw.at/suche",
        },
        description="Austria direct cooperative provider for new-build, immediate, allocation, and planning-stage housing with application-specific supply.",
        family="cooperative",
        trust_tier="trusted",
        coverage="regional",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="cooperative_primary",
        supported_region_codes=("vienna",),
    ),
    PropertyProviderSpec(
        key="wag_at",
        label="WAG",
        country_code="AT",
        host_markers=("wag.at",),
        listing_path_markers=("/immobilien/", "/wohngebiete/", "/projekte/"),
        search_urls={
            "rent": "https://www.wag.at/",
            "buy": "https://www.wag.at/",
        },
        description="Austria cooperative and affordable housing provider covering Upper Austria, Lower Austria, Salzburg, and Styria.",
        family="cooperative",
        trust_tier="trusted",
        coverage="multi_region",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="cooperative_primary",
        supported_region_codes=("upper_austria",),
    ),
    PropertyProviderSpec(
        key="heimat_oesterreich_at",
        label="Heimat Österreich",
        country_code="AT",
        host_markers=("heimat-oesterreich.at",),
        listing_path_markers=("/de/", "/wohnen/", "/immobilien/"),
        search_urls={
            "rent": "https://www.heimat-oesterreich.at/de",
            "buy": "https://www.heimat-oesterreich.at/de",
        },
        description="Austria non-profit housing association for subsidized homes in Salzburg, Lower Austria, and Vienna.",
        family="cooperative",
        trust_tier="trusted",
        coverage="multi_region",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="cooperative_primary",
        supported_region_codes=("salzburg",),
    ),
    PropertyProviderSpec(
        key="bwsg_at",
        label="BWSG",
        country_code="AT",
        host_markers=("bwsg.at",),
        listing_path_markers=("/immobilien", "/projekte", "/in-bau"),
        search_urls={
            "rent": "https://www.bwsg.at/",
            "buy": "https://www.bwsg.at/",
        },
        description="Austria housing association with subsidized rentals, smart homes, ownership units, and projects in construction.",
        family="cooperative",
        trust_tier="trusted",
        coverage="national",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="cooperative_primary",
        supported_region_codes=("upper_austria",),
    ),
    PropertyProviderSpec(
        key="wiensued_at",
        label="Wien-Süd",
        country_code="AT",
        host_markers=("wiensued.at",),
        listing_path_markers=("/objekte", "/wohnungen", "/bestandsobjekte"),
        search_urls={
            "rent": "https://www.wiensued.at/",
            "buy": "https://www.wiensued.at/",
        },
        description="Austria cooperative housing provider with immediately available and waitlist-based Vienna-area stock.",
        family="cooperative",
        trust_tier="trusted",
        coverage="regional",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="cooperative_primary",
        supported_region_codes=("salzburg",),
    ),
    PropertyProviderSpec(
        key="ebg_wohnen_at",
        label="EBG Wohnen",
        country_code="AT",
        host_markers=("ebg-wohnen.at",),
        listing_path_markers=("/objekt", "/objekte", "/angebote"),
        search_urls={
            "rent": "https://www.ebg-wohnen.at/",
            "buy": "https://www.ebg-wohnen.at/",
        },
        description="Vienna cooperative housing provider for gemeinnützige single- and multi-family housing stock.",
        family="cooperative",
        trust_tier="trusted",
        coverage="regional",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="cooperative_primary",
    ),
    PropertyProviderSpec(
        key="ooe_wohnbau_at",
        label="OÖ Wohnbau",
        country_code="AT",
        host_markers=("ooewohnbau.at",),
        listing_path_markers=("/immobilien", "/wohnbau", "/angebote"),
        search_urls={
            "rent": "https://ooewohnbau.at/",
            "buy": "https://ooewohnbau.at/",
        },
        description="Upper Austria cooperative housing and ownership provider with subsidized and privately financed inventory.",
        family="cooperative",
        trust_tier="trusted",
        coverage="regional",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="cooperative_primary",
        supported_region_codes=("upper_austria",),
    ),
    PropertyProviderSpec(
        key="salzburg_wohnbau_at",
        label="Salzburg Wohnbau",
        country_code="AT",
        host_markers=("salzburg-wohnbau.at",),
        listing_path_markers=("/wohnbau-projekte/", "/immosuche", "/angebote"),
        search_urls={
            "rent": "https://www.salzburg-wohnbau.at/wohnbau-projekte/",
            "buy": "https://www.salzburg-wohnbau.at/wohnbau-projekte/",
        },
        description="Salzburg Wohnbau project and search lane for subsidized and project-stage Austrian housing.",
        family="cooperative",
        trust_tier="trusted",
        coverage="regional",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="cooperative_primary",
        supported_region_codes=("salzburg",),
    ),
    PropertyProviderSpec(
        key="oevw_at",
        label="ÖVW",
        country_code="AT",
        host_markers=("oevw.at",),
        listing_path_markers=("/de/", "/projekte", "/wohnungen"),
        search_urls={
            "rent": "https://www.oevw.at/",
            "buy": "https://www.oevw.at/",
        },
        description="Österreichisches Volkswohnungswerk lane for modern rental and ownership housing projects.",
        family="cooperative",
        trust_tier="trusted",
        coverage="regional",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="cooperative_primary",
    ),
    PropertyProviderSpec(
        key="broker_direct_at",
        label="Makler Direkt / Kalandra",
        country_code="AT",
        host_markers=("kalandra.at",),
        listing_path_markers=("/objekt/",),
        search_urls={
            "rent": "https://www.kalandra.at/immobiliensuche",
            "buy": "https://www.kalandra.at/immobiliensuche",
        },
        description="Austria broker-direct group scaffold for per-source adapters and source-specific filter contracts.",
        family="broker_direct",
        trust_tier="standard",
    ),
    PropertyProviderSpec(
        key="developer_projects_at",
        label="Bautraeger Projekte",
        country_code="AT",
        host_markers=("sozialbau.at", "angebote.sozialbau.at", "wbv-gpa.at", "arwag.at", "raiffeisen-wohnbau.at", "leitgoeb-wohnbau.at", "viktoria-wohnbau.at"),
        listing_path_markers=("/sobitvx/htmlprospect/", "/angebote/objekte-in-bau/", "/angebote/objekte-in-planung/", "/wohnung/", "/projects/", "/projekte/", "/projekt/"),
        search_urls={
            "rent": "https://angebote.sozialbau.at/sobitvX/htmlprospect/home.xhtml?pq_scope=in_bau",
            "buy": "https://angebote.sozialbau.at/sobitvX/htmlprospect/home.xhtml?pq_scope=in_bau",
        },
        description="Austria developer and project-launch sources for early pipeline and first-occupancy signals.",
        family="developer_projects",
        trust_tier="standard",
    ),
    PropertyProviderSpec(
        key="arwag_at",
        label="ARWAG",
        country_code="AT",
        host_markers=("arwag.at",),
        listing_path_markers=("/projekt", "/projekte", "/wohnungen"),
        search_urls={
            "rent": "https://www.arwag.at/",
            "buy": "https://www.arwag.at/",
        },
        description="Vienna developer project lane for provision-free new-build housing projects.",
        family="developer_projects",
        trust_tier="standard",
        coverage="regional",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="developer_primary",
        search_ready=False,
        availability_note="Coming soon - target recovery canary must pass before this provider is enabled.",
    ),
    PropertyProviderSpec(
        key="raiffeisen_wohnbau_at",
        label="Raiffeisen WohnBau",
        country_code="AT",
        host_markers=("raiffeisen-wohnbau.at",),
        listing_path_markers=("/projects/", "/project/", "/overview/"),
        search_urls={
            "rent": "https://www.raiffeisen-wohnbau.at/en/projects/overview/",
            "buy": "https://www.raiffeisen-wohnbau.at/en/projects/overview/",
        },
        description="Austria developer project pipeline for Vienna and surrounding residential quality-living projects.",
        family="developer_projects",
        trust_tier="standard",
        coverage="regional",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="developer_primary",
        supported_region_codes=("vienna", "lower_austria"),
    ),
    PropertyProviderSpec(
        key="leitgoeb_wohnbau_at",
        label="Leitgöb Wohnbau",
        country_code="AT",
        host_markers=("leitgoeb-wohnbau.at",),
        listing_path_markers=("/neubauprojekte", "/immobilien", "/projekt"),
        search_urls={
            "rent": "https://www.leitgoeb-wohnbau.at/",
            "buy": "https://www.leitgoeb-wohnbau.at/",
        },
        description="Salzburg and Upper Austria new-build developer project and ownership lane.",
        family="developer_projects",
        trust_tier="standard",
        coverage="multi_region",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="developer_primary",
        supported_region_codes=("salzburg", "upper_austria"),
    ),
    PropertyProviderSpec(
        key="viktoria_wohnbau_at",
        label="Viktoria Wohnbau",
        country_code="AT",
        host_markers=("viktoria-wohnbau.at",),
        listing_path_markers=("/projekte", "/projekt", "/immobilien"),
        search_urls={
            "rent": "https://www.viktoria-wohnbau.at/",
            "buy": "https://www.viktoria-wohnbau.at/",
        },
        description="Regional Austrian developer and property-services lane for new-build residential projects.",
        family="developer_projects",
        trust_tier="standard",
        coverage="regional",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="developer_primary",
    ),
    PropertyProviderSpec(
        key="public_housing_at",
        label="Oeffentliche Wohnquellen",
        country_code="AT",
        host_markers=("gesiba.at", "siedlungsunion.at", "sozialbau.at", "angebote.sozialbau.at"),
        listing_path_markers=("/immobilien/wohnungen/objekt", "/wohnen/sofort/", "/sobitvx/htmlprospect/",),
        search_urls={
            "rent": "https://www.gesiba.at/immobilien/wohnungen",
            "buy": "https://www.gesiba.at/immobilien/wohnungen",
        },
        description="Austria public, cooperative, and Wohnservice-like housing sources kept separate from broad commercial marketplaces.",
        family="public_housing",
        trust_tier="trusted",
    ),
    PropertyProviderSpec(
        key="distressed_sales_at",
        label="Notverkauf und Justiz",
        country_code="AT",
        host_markers=("edikte.justiz.gv.at", "edikte2.justiz.gv.at"),
        listing_path_markers=("/edikte/ex/exedi3.nsf/", "/ex/exedi3.nsf/alldoc/", "/alldoc/"),
        search_urls={
            "buy": "https://edikte2.justiz.gv.at/edikte/ex/exedi3.nsf/Suche!OpenForm",
        },
        description="Austria judicial auctions, forced-sale, and distressed-sale lanes from court and insolvency publications.",
        family="distressed_sales",
        trust_tier="standard",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="community_signals_at",
        label="Facebook / Telegram Hinweise",
        country_code="AT",
        host_markers=("flatbee.at", "flatbee.de"),
        listing_path_markers=(
            "/properties/property_search/",
            "/properties/property_detail/",
            "/searchengine_property_detail/",
        ),
        search_urls={
            "rent": "https://www.flatbee.at/properties/property_search",
            "buy": "https://www.flatbee.at/properties/property_search",
        },
        description="Austria Facebook groups, Telegram leads, Flatbee-style community surfaces, and other weakly verified off-market sources that require stronger manual validation.",
        family="community_signals",
        trust_tier="watch",
        search_ready=False,
        availability_note="Coming soon",
    ),
    PropertyProviderSpec(
        key="flatbee",
        label="Flatbee",
        country_code="AT",
        host_markers=("flatbee.at", "flatbee.de"),
        listing_path_markers=(
            "/properties/property_search/",
            "/properties/property_detail/",
            "/searchengine_property_detail/",
        ),
        search_urls={
            "rent": "https://www.flatbee.at/properties/property_search",
            "buy": "https://www.flatbee.at/properties/property_search",
        },
        description="Austria commission-free meta search with broad long-tail coverage, but lower trust quality than the primary AT sources.",
        family="community_meta",
        trust_tier="watch",
    ),
    PropertyProviderSpec(
        key="justiz_edikte_at",
        label="Justiz Edikte",
        country_code="AT",
        host_markers=("edikte.justiz.gv.at", "edikte2.justiz.gv.at"),
        listing_path_markers=(
            "/edikte/ex/exedi3.nsf/",
            "/ex/exedi3.nsf/0/",
            "/edikte/ex/exedi3.nsf/alldoc/",
            "/ex/exedi3.nsf/alldoc/",
            "/alldoc/",
        ),
        search_urls={
            "buy": "https://edikte2.justiz.gv.at/edikte/ex/exedi3.nsf/Suche!OpenForm",
        },
        description="Austria judicial foreclosure and forced-sale publications from the Ediktsdatei.",
        supported_listing_modes=("buy",),
        family="distressed_sales",
        trust_tier="trusted",
        coverage="national",
        floorplan_reliability="low",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="weak",
        official_source_quality="official_primary",
    ),
    PropertyProviderSpec(
        key="zvginfo_at",
        label="ZVGInfo Austria",
        country_code="AT",
        host_markers=("zvginfo.at",),
        listing_path_markers=("/versteigerungen/", "/objekt/", "/detail/"),
        search_urls={
            "buy": "https://www.zvginfo.at/",
        },
        description="Austria secondary auction aggregation lane for quicker discovery, while legal truth remains the Justiz Ediktsdatei.",
        family="distressed_sales",
        trust_tier="watch",
        supported_listing_modes=("buy",),
        coverage="national",
        floorplan_reliability="low",
        duplicate_rate="medium",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="weak",
        official_source_quality="aggregator_watch",
    ),
    PropertyProviderSpec(
        key="immoweb",
        label="Immoweb",
        country_code="BE",
        host_markers=("immoweb.be",),
        listing_path_markers=("/en/classified/", "/nl/zoekertje/", "/fr/annonce/"),
        search_urls={
            "rent": "https://www.immoweb.be/en/search/apartment-and-house/for-rent",
            "buy": "https://www.immoweb.be/en/search/apartment-and-house/for-sale",
        },
        description="Belgium flagship property portal with dense urban inventory.",
    ),
    PropertyProviderSpec(
        key="zimmo",
        label="Zimmo",
        country_code="BE",
        host_markers=("zimmo.be",),
        listing_path_markers=("/en/", "/nl/", "/fr/"),
        search_urls={
            "rent": "https://www.zimmo.be/en/search/for-rent/",
            "buy": "https://www.zimmo.be/en/search/for-sale/",
        },
        description="Belgium residential marketplace with strong Flemish supply.",
    ),
    PropertyProviderSpec(
        key="biddit_be",
        label="Biddit",
        country_code="BE",
        host_markers=("biddit.be",),
        listing_path_markers=("/fr/catalogue/", "/nl/catalogus/", "/en/catalog/", "/detail/"),
        search_urls={
            "buy": "https://www.biddit.be",
        },
        description="Belgium public property auction platform of the Royal Federation of Belgian Notaries.",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="taxsales_ca",
        label="TaxSalesPortal",
        country_code="CA",
        host_markers=("taxsalesportal.ca",),
        listing_path_markers=("/property/", "/foreclosed-properties/", "/tax-sale-property/"),
        search_urls={
            "buy": "https://taxsalesportal.ca/foreclosed-properties/",
        },
        description="Canada distressed property and tax-sale aggregation across provincial auction processes.",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="core_portals_de",
        label="Kernportale Deutschland",
        country_code="DE",
        host_markers=("immobilienscout24.de", "immoscout24.de", "immowelt.de", "immonet.de", "kleinanzeigen.de", "meinestadt.de"),
        listing_path_markers=("/expose/", "/angebot/", "/s-anzeige/"),
        search_urls={
            "rent": "https://www.kleinanzeigen.de/s-wohnung-mieten/c203",
            "buy": "https://www.kleinanzeigen.de/s-wohnung-kaufen/c196",
        },
        description="Germany grouped broad-market portals for residential rent and buy discovery across national and local-market search surfaces.",
        family="core_portal",
        trust_tier="standard",
        coverage="national",
        floorplan_reliability="medium",
        duplicate_rate="high",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="medium",
        official_source_quality="portal_aggregated",
    ),
    PropertyProviderSpec(
        key="shared_housing_de",
        label="WG / Zimmer Deutschland",
        country_code="DE",
        host_markers=("wg-gesucht.de", "wg-gesucht.com", "meinestadt.de"),
        listing_path_markers=("/wg-zimmer-", "/wohnung-mieten", "/zimmer"),
        search_urls={
            "rent": "https://www.wg-gesucht.de/",
        },
        description="Germany grouped shared-housing, room, and student-friendly rental lanes kept separate from standard family-home search.",
        family="shared_housing",
        trust_tier="standard",
        supported_listing_modes=("rent",),
        coverage="national",
        floorplan_reliability="low",
        duplicate_rate="medium",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="medium",
        official_source_quality="platform_specialist",
    ),
    PropertyProviderSpec(
        key="corporate_landlords_de",
        label="Direktvermieter Deutschland",
        country_code="DE",
        host_markers=("vonovia.de", "leg-wohnen.de", "tag-wohnen.de"),
        listing_path_markers=("/mietwohnungen", "/immobilien/detail/", "/wohnung-finden"),
        search_urls={
            "rent": "https://www.vonovia.de/de-de/wohnungssuche",
        },
        description="Germany grouped direct-landlord lane for large housing companies that often publish inventory earlier and with more reliable operating details.",
        family="corporate_landlord",
        trust_tier="trusted",
        supported_listing_modes=("rent",),
        coverage="multi_region",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="strong",
        official_source_quality="landlord_primary",
    ),
    PropertyProviderSpec(
        key="municipal_housing_de",
        label="Kommunale Wohnungsanbieter",
        country_code="DE",
        host_markers=("degewo.de", "gewobag.de", "howoge.de", "saga.hamburg"),
        listing_path_markers=("/wohnungen", "/immobiliensuche", "/angebote"),
        search_urls={
            "rent": "https://www.degewo.de/wohnen/wohnungen-und-gewerbe.html",
            "buy": "https://www.degewo.de/wohnen/wohnungen-und-gewerbe.html",
        },
        description="Germany grouped municipal housing lane for city-owned/public-sector rental inventory and WBS-sensitive supply.",
        family="municipal_housing",
        trust_tier="trusted",
        coverage="multi_region",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="municipal_primary",
    ),
    PropertyProviderSpec(
        key="cooperatives_de",
        label="Genossenschaften Deutschland",
        country_code="DE",
        host_markers=("wohnprojekte-portal.de", "begeno16.de"),
        listing_path_markers=("/projekt", "/projekte", "/wohnen"),
        search_urls={
            "rent": "https://www.wohnprojekte-portal.de/",
            "buy": "https://www.wohnprojekte-portal.de/",
        },
        description="Germany grouped cooperative, social-housing, and gemeinschaftliches Wohnen sources with higher application friction and lower churn.",
        family="cooperative",
        trust_tier="trusted",
        coverage="multi_region",
        floorplan_reliability="low",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="cooperative_primary",
    ),
    PropertyProviderSpec(
        key="new_build_de",
        label="Neubau Projekte Deutschland",
        country_code="DE",
        host_markers=("neubaukompass.com",),
        listing_path_markers=("/property/",),
        search_urls={
            "buy": "https://www.neubaukompass.com/new-build-real-estate/deutschland/",
        },
        description="Germany grouped new-build and developer-project lane for project signals, first-occupancy launches, and developer-direct inventory.",
        family="developer_projects",
        trust_tier="standard",
        supported_listing_modes=("buy",),
        coverage="national",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="developer_primary",
    ),
    PropertyProviderSpec(
        key="auctions_de",
        label="Zwangsversteigerung Deutschland",
        country_code="DE",
        host_markers=("zvg-portal.de", "portal-zvg.de", "zvnow.de"),
        listing_path_markers=("button=showzvg", "/versteigerung/", "/auction/", "/detail/"),
        search_urls={
            "buy": "https://www.zvg-portal.de/",
        },
        description="Germany grouped foreclosure and auction lane for investor and legal-review workflows, kept out of normal family search by default.",
        family="distressed_sales",
        trust_tier="standard",
        supported_listing_modes=("buy",),
        coverage="national",
        floorplan_reliability="low",
        duplicate_rate="medium",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="weak",
        official_source_quality="official_and_aggregated",
    ),
    PropertyProviderSpec(
        key="broker_direct_de",
        label="Makler Direkt Deutschland",
        country_code="DE",
        host_markers=("von-poll.com", "ohne-makler.net"),
        listing_path_markers=("/immobilie/", "/expose/"),
        search_urls={
            "rent": "https://www.ohne-makler.net/immobilien/wohnung-mieten/",
            "buy": "https://www.ohne-makler.net/immobilien/immobilie-kaufen/",
        },
        description="Germany grouped broker-direct and owner-direct lane for regional office networks and direct marketing inventory.",
        family="broker_direct",
        trust_tier="standard",
        coverage="national",
        floorplan_reliability="medium",
        duplicate_rate="medium",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="broker_and_owner_primary",
    ),
    PropertyProviderSpec(
        key="furnished_relocation_de",
        label="Möbliert / Relocation Deutschland",
        country_code="DE",
        host_markers=("wunderflats.com", "housinganywhere.com", "thehomelike.com", "homelike.com"),
        listing_path_markers=("/de/moeblierte-wohnungen/", "/s/", "/accommodation/"),
        search_urls={
            "rent": "https://wunderflats.com/de/moeblierte-wohnungen",
        },
        description="Germany grouped furnished and relocation lane for mid-term, expat, and corporate temporary housing, kept separate from standard home search.",
        family="furnished_relocation",
        trust_tier="watch",
        supported_listing_modes=("rent",),
        coverage="national",
        floorplan_reliability="low",
        duplicate_rate="medium",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="medium",
        official_source_quality="platform_specialist",
    ),
    PropertyProviderSpec(
        key="immoscout_de",
        label="ImmoScout24 Germany",
        country_code="DE",
        host_markers=("immobilienscout24.de", "immoscout24.de"),
        listing_path_markers=("/expose/", "/expose", "/detail/"),
        search_urls={
            "rent": "https://www.immobilienscout24.de/Suche/de/wohnung-mieten",
            "buy": "https://www.immobilienscout24.de/Suche/de/wohnung-kaufen",
        },
        description="Germany flagship portal for rental and purchase search.",
    ),
    PropertyProviderSpec(
        key="immowelt",
        label="Immowelt",
        country_code="DE",
        host_markers=("immowelt.de",),
        listing_path_markers=("/expose/", "/immobilien/"),
        search_urls={
            "rent": "https://www.immowelt.de/suche/mietwohnungen",
            "buy": "https://www.immowelt.de/suche/kaufen/wohnung",
        },
        description="Germany portal with broad inventory and structured listing pages.",
    ),
    PropertyProviderSpec(
        key="immonet",
        label="Immonet",
        country_code="DE",
        host_markers=("immonet.de",),
        listing_path_markers=("/expose/", "/angebot/"),
        search_urls={
            "rent": "https://www.immonet.de/wohnung-mieten.html",
            "buy": "https://www.immonet.de/wohnung-kaufen.html",
        },
        description="Germany search inventory with apartment rent and buy lanes.",
    ),
    PropertyProviderSpec(
        key="wohnungsboerse_de",
        label="Wohnungsboerse.net",
        country_code="DE",
        host_markers=("wohnungsboerse.net", "www.wohnungsboerse.net"),
        listing_path_markers=("/wohnung-mieten", "/wohnung-kaufen", "/immobilien/", "/expose/"),
        search_urls={
            "rent": "https://www.wohnungsboerse.net/wohnung-mieten-provisionsfrei",
            "buy": "https://www.wohnungsboerse.net/wohnung-kaufen",
        },
        description="Germany apartment-focused residential portal for rental and purchase discovery.",
        family="core_portal",
        trust_tier="standard",
        coverage="national",
        floorplan_reliability="medium",
        duplicate_rate="medium",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="medium",
        official_source_quality="provider_only",
        last_verified="2026-06-29",
    ),
    PropertyProviderSpec(
        key="kleinanzeigen_immo",
        label="Kleinanzeigen Immobilien",
        country_code="DE",
        host_markers=("kleinanzeigen.de",),
        listing_path_markers=("/s-anzeige/",),
        search_urls={
            "rent": "https://www.kleinanzeigen.de/s-wohnung-mieten/c203",
            "buy": "https://www.kleinanzeigen.de/s-wohnung-kaufen/c196",
        },
        description="Germany classifieds lane that still surfaces off-market-style inventory.",
        family="classified",
        trust_tier="watch",
        coverage="national",
        floorplan_reliability="medium",
        duplicate_rate="high",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="medium",
        official_source_quality="provider_only",
    ),
    PropertyProviderSpec(
        key="meinestadt_de",
        label="meinestadt.de Immobilien",
        country_code="DE",
        host_markers=("meinestadt.de",),
        listing_path_markers=("/immobilien/", "/wohnungen", "/haus-kaufen"),
        search_urls={
            "rent": "https://www.meinestadt.de/deutschland/immobilien/wohnungen",
            "buy": "https://www.meinestadt.de/deutschland/immobilien/haus-kaufen",
        },
        description="Germany local city-oriented property discovery lane with stronger rental and WBS-adjacent surfaces.",
        family="classified",
        trust_tier="standard",
        coverage="national",
        floorplan_reliability="low",
        duplicate_rate="medium",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="medium",
        official_source_quality="provider_only",
    ),
    PropertyProviderSpec(
        key="zvg_de",
        label="ZVG Portal",
        country_code="DE",
        host_markers=("zvg-portal.de",),
        listing_path_markers=("button=showzvg", "button=show", "/index.php?button=show"),
        search_urls={
            "buy": "https://www.zvg-portal.de/",
        },
        description="Germany official court publication portal for real-estate foreclosure auction dates.",
        family="distressed_sales",
        trust_tier="trusted",
        supported_listing_modes=("buy",),
        coverage="national",
        floorplan_reliability="low",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="weak",
        official_source_quality="official_primary",
    ),
    PropertyProviderSpec(
        key="neubaukompass_de",
        label="neubau kompass Germany",
        country_code="DE",
        host_markers=("neubaukompass.com",),
        listing_path_markers=("/property/",),
        search_urls={
            "buy": "https://www.neubaukompass.com/new-build-real-estate/deutschland/",
        },
        description="Germany specialist portal for new-build apartments and houses direct from developers and marketers.",
        family="developer_projects",
        trust_tier="standard",
        supported_listing_modes=("buy",),
        coverage="national",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="developer_primary",
    ),
    PropertyProviderSpec(
        key="wg_gesucht_de",
        label="WG-Gesucht",
        country_code="DE",
        host_markers=("wg-gesucht.de", "wg-gesucht.com"),
        listing_path_markers=("/wg-zimmer-", "/wohnungen-", "/haeuser-"),
        search_urls={
            "rent": "https://www.wg-gesucht.de/",
        },
        description="Germany specialist platform for rooms, shared apartments, sublets, and short-term rental discovery.",
        family="shared_housing",
        trust_tier="standard",
        supported_listing_modes=("rent",),
        coverage="national",
        floorplan_reliability="low",
        duplicate_rate="medium",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="medium",
        official_source_quality="platform_specialist",
    ),
    PropertyProviderSpec(
        key="vonovia_de",
        label="Vonovia",
        country_code="DE",
        host_markers=("vonovia.de",),
        listing_path_markers=("/de-de/wohnungssuche", "/wohnungen/"),
        search_urls={
            "rent": "https://www.vonovia.de/de-de/wohnungssuche",
        },
        description="Germany direct-landlord national housing lane with strong structured rental search and equipment details.",
        family="corporate_landlord",
        trust_tier="trusted",
        supported_listing_modes=("rent",),
        coverage="national",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="strong",
        official_source_quality="landlord_primary",
    ),
    PropertyProviderSpec(
        key="leg_wohnen_de",
        label="LEG Wohnen",
        country_code="DE",
        host_markers=("leg-wohnen.de",),
        listing_path_markers=("/mietwohnungen/", "/immobilien/detail/"),
        search_urls={
            "rent": "https://www.leg-wohnen.de/mietwohnungen/",
        },
        description="Germany direct landlord with strong NRW-heavy rental inventory and structured object facts.",
        family="corporate_landlord",
        trust_tier="trusted",
        supported_listing_modes=("rent",),
        coverage="multi_region",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="strong",
        official_source_quality="landlord_primary",
    ),
    PropertyProviderSpec(
        key="tag_wohnen_de",
        label="TAG Wohnen",
        country_code="DE",
        host_markers=("tag-wohnen.de",),
        listing_path_markers=("/wohnung-finden", "/wohnungsangebot"),
        search_urls={
            "rent": "https://tag-wohnen.de/",
        },
        description="Germany direct rental-home lane focused on mid-size and eastern markets with landlord-direct availability.",
        family="corporate_landlord",
        trust_tier="trusted",
        supported_listing_modes=("rent",),
        coverage="multi_region",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="medium",
        official_source_quality="landlord_primary",
    ),
    PropertyProviderSpec(
        key="degewo_berlin",
        label="degewo",
        country_code="DE",
        host_markers=("degewo.de",),
        listing_path_markers=("/wohnungen-und-gewerbe", "/mieten/"),
        search_urls={
            "rent": "https://www.degewo.de/wohnen/wohnungen-und-gewerbe.html",
        },
        description="Berlin municipal housing lane for public-sector rental stock and application-driven inventory.",
        family="municipal_housing",
        trust_tier="trusted",
        supported_listing_modes=("rent",),
        coverage="regional",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="municipal_primary",
    ),
    PropertyProviderSpec(
        key="saga_hamburg",
        label="SAGA Hamburg",
        country_code="DE",
        host_markers=("saga.hamburg",),
        listing_path_markers=("/immobiliensuche", "/mieten/"),
        search_urls={
            "rent": "https://www.saga.hamburg/immobiliensuche",
        },
        description="Hamburg municipal housing lane for large-scale public rental stock and subsidized housing discovery.",
        family="municipal_housing",
        trust_tier="trusted",
        supported_listing_modes=("rent",),
        coverage="regional",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="municipal_primary",
    ),
    PropertyProviderSpec(
        key="wohnprojekte_portal_de",
        label="Wohnprojekte-Portal",
        country_code="DE",
        host_markers=("wohnprojekte-portal.de",),
        listing_path_markers=("/projekte/", "/projekt/"),
        search_urls={
            "rent": "https://www.wohnprojekte-portal.de/",
            "buy": "https://www.wohnprojekte-portal.de/",
        },
        description="Germany community and cooperative housing-project directory for gemeinschaftliches Wohnen and lower-churn application-driven supply.",
        family="cooperative",
        trust_tier="standard",
        coverage="national",
        floorplan_reliability="low",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="weak",
        official_source_quality="cooperative_primary",
    ),
    PropertyProviderSpec(
        key="portal_zvg_de",
        label="Portal ZVG",
        country_code="DE",
        host_markers=("portal-zvg.de",),
        listing_path_markers=("/versteigerung/", "/objekt/"),
        search_urls={
            "buy": "https://www.portal-zvg.de/",
        },
        description="Germany foreclosure aggregation lane for secondary auction discovery and investor lead scanning.",
        family="distressed_sales",
        trust_tier="watch",
        supported_listing_modes=("buy",),
        coverage="national",
        floorplan_reliability="low",
        duplicate_rate="medium",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="weak",
        official_source_quality="aggregator_watch",
    ),
    PropertyProviderSpec(
        key="zvnow_de",
        label="ZVnow",
        country_code="DE",
        host_markers=("zvnow.de",),
        listing_path_markers=("/versteigerungen/", "/objekt/", "/detail/"),
        search_urls={
            "buy": "https://www.zvnow.de/",
        },
        description="Germany auction aggregation and analytics lane for foreclosure opportunity scanning and investor workflows.",
        family="distressed_sales",
        trust_tier="watch",
        supported_listing_modes=("buy",),
        coverage="national",
        floorplan_reliability="low",
        duplicate_rate="medium",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="weak",
        official_source_quality="aggregator_watch",
    ),
    PropertyProviderSpec(
        key="ohne_makler_de",
        label="ohne-makler.net",
        country_code="DE",
        host_markers=("ohne-makler.net",),
        listing_path_markers=("/immobilie/",),
        search_urls={
            "rent": "https://www.ohne-makler.net/immobilien/wohnung-mieten/",
            "buy": "https://www.ohne-makler.net/immobilien/immobilie-kaufen/",
        },
        description="Germany private-direct and commission-free property portal for owner and landlord listings without broker fees.",
        family="broker_direct",
        trust_tier="standard",
        coverage="national",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="owner_direct",
    ),
    PropertyProviderSpec(
        key="von_poll_de",
        label="VON POLL IMMOBILIEN",
        country_code="DE",
        host_markers=("von-poll.com",),
        listing_path_markers=("/expose/", "/immobilien/", "/real-estate-agent/"),
        search_urls={
            "rent": "https://www.von-poll.com/de",
            "buy": "https://www.von-poll.com/de",
        },
        description="Germany premium broker-direct network with broad national office coverage and residential buy and rent inventory.",
        family="broker_direct",
        trust_tier="standard",
        coverage="multi_region",
        floorplan_reliability="medium",
        duplicate_rate="low",
        tour_availability="rare",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="broker_primary",
    ),
    PropertyProviderSpec(
        key="homegate",
        label="Homegate",
        country_code="CH",
        host_markers=("homegate.ch",),
        listing_path_markers=("/rent/", "/buy/"),
        search_urls={
            "rent": "https://www.homegate.ch/rent/real-estate/country-switzerland",
            "buy": "https://www.homegate.ch/buy/real-estate/country-switzerland",
        },
        description="Switzerland mainstream residential portal.",
    ),
    PropertyProviderSpec(
        key="newhome",
        label="newhome",
        country_code="CH",
        host_markers=("newhome.ch",),
        listing_path_markers=("/de/", "/fr/", "/it/"),
        search_urls={
            "rent": "https://www.newhome.ch/de/mieten/immobilien",
            "buy": "https://www.newhome.ch/de/kaufen/immobilien",
        },
        description="Switzerland portal with canton-heavy residential coverage.",
    ),
    PropertyProviderSpec(
        key="immoscout_ch",
        label="ImmoScout24 Switzerland",
        country_code="CH",
        host_markers=("immoscout24.ch",),
        listing_path_markers=("/rent/", "/buy/", "/en/"),
        search_urls={
            "rent": "https://www.immoscout24.ch/en/real-estate/rent",
            "buy": "https://www.immoscout24.ch/en/real-estate/buy",
        },
        description="Switzerland ImmoScout variant for multilingual search.",
    ),
    PropertyProviderSpec(
        key="auctionhome_ch",
        label="AuctionHome",
        country_code="CH",
        host_markers=("auctionhome.ch",),
        listing_path_markers=("/objekt/", "/property/", "/auction/"),
        search_urls={
            "buy": "https://www.en.auctionhome.ch/",
        },
        description="Switzerland property foreclosure auction listings sourced from debt collection and bankruptcy offices.",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="daft_ie",
        label="Daft.ie",
        country_code="IE",
        host_markers=("daft.ie",),
        listing_path_markers=("/for-rent/", "/for-sale/"),
        search_urls={
            "rent": "https://www.daft.ie/property-for-rent/ireland",
            "buy": "https://www.daft.ie/property-for-sale/ireland",
        },
        description="Ireland flagship residential portal.",
    ),
    PropertyProviderSpec(
        key="myhome_ie",
        label="MyHome.ie",
        country_code="IE",
        host_markers=("myhome.ie",),
        listing_path_markers=("/residential/",),
        search_urls={
            "rent": "https://www.myhome.ie/rentals",
            "buy": "https://www.myhome.ie/residential",
        },
        description="Ireland portal with agency-led sale and rental inventory.",
    ),
    PropertyProviderSpec(
        key="youbid_ie",
        label="Youbid",
        country_code="IE",
        host_markers=("youbid.ie",),
        listing_path_markers=("/property/", "/details/", "/auction/"),
        search_urls={
            "buy": "https://www.youbid.ie/",
        },
        description="Ireland national online property auction platform used for distressed and receiver-led sales.",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="rightmove",
        label="Rightmove",
        country_code="UK",
        host_markers=("rightmove.co.uk",),
        listing_path_markers=("/properties/",),
        search_urls={
            "rent": "https://www.rightmove.co.uk/property-to-rent.html",
            "buy": "https://www.rightmove.co.uk/property-for-sale.html",
        },
        description="United Kingdom flagship property portal.",
    ),
    PropertyProviderSpec(
        key="zoopla",
        label="Zoopla",
        country_code="UK",
        host_markers=("zoopla.co.uk",),
        listing_path_markers=("/to-rent/details/", "/for-sale/details/"),
        search_urls={
            "rent": "https://www.zoopla.co.uk/to-rent/property/",
            "buy": "https://www.zoopla.co.uk/for-sale/property/",
        },
        description="United Kingdom portal with broad consumer search share.",
    ),
    PropertyProviderSpec(
        key="onthemarket",
        label="OnTheMarket",
        country_code="UK",
        host_markers=("onthemarket.com",),
        listing_path_markers=("/details/",),
        search_urls={
            "rent": "https://www.onthemarket.com/to-rent/",
            "buy": "https://www.onthemarket.com/for-sale/",
        },
        description="United Kingdom portal with agency inventory and structured detail pages.",
    ),
    PropertyProviderSpec(
        key="repolist_uk",
        label="Repolist",
        country_code="UK",
        host_markers=("repolist.co.uk",),
        listing_path_markers=("/property/", "/auction/", "/listing/"),
        search_urls={
            "buy": "https://repolist.co.uk/",
        },
        description="United Kingdom repossessed-property and auction discovery portal.",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="realestate_au",
        label="realestate.com.au",
        country_code="AU",
        host_markers=("realestate.com.au",),
        listing_path_markers=("/property-", "/project/"),
        search_urls={
            "rent": "https://www.realestate.com.au/rent",
            "buy": "https://www.realestate.com.au/buy",
        },
        description="Australia flagship portal for rent and buy search.",
    ),
    PropertyProviderSpec(
        key="domain_au",
        label="Domain",
        country_code="AU",
        host_markers=("domain.com.au",),
        listing_path_markers=("/address-",),
        search_urls={
            "rent": "https://www.domain.com.au/rent/",
            "buy": "https://www.domain.com.au/sale/",
        },
        description="Australia national property portal with structured listing pages.",
    ),
    PropertyProviderSpec(
        key="flatmates_au",
        label="Flatmates",
        country_code="AU",
        host_markers=("flatmates.com.au",),
        listing_path_markers=("/share-house/", "/people/"),
        search_urls={
            "rent": "https://flatmates.com.au/rooms",
            "buy": "https://flatmates.com.au/rooms",
        },
        description="Australia shared-living and room-rental marketplace.",
        supported_listing_modes=("rent",),
    ),
    PropertyProviderSpec(
        key="mortgagee_au",
        label="Mortgagee Sales Australia",
        country_code="AU",
        host_markers=("ozhousehunters.com.au", "lloydsonline.com.au"),
        listing_path_markers=("/mortgagee", "/property/", "/AuctionDetails.aspx"),
        search_urls={
            "buy": "https://www.ozhousehunters.com.au/",
        },
        description="Australia mortgagee-in-possession and distressed property sales feed.",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="idealista_es",
        label="Idealista Spain",
        country_code="ES",
        host_markers=("idealista.com",),
        listing_path_markers=("/inmueble/",),
        search_urls={
            "rent": "https://www.idealista.com/en/alquiler-viviendas/",
            "buy": "https://www.idealista.com/en/venta-viviendas/",
        },
        description="Spain flagship portal for residential discovery.",
    ),
    PropertyProviderSpec(
        key="fotocasa",
        label="Fotocasa",
        country_code="ES",
        host_markers=("fotocasa.es",),
        listing_path_markers=("/es/", "/vivienda/"),
        search_urls={
            "rent": "https://www.fotocasa.es/es/alquiler/viviendas/espana/todas-las-zonas/l",
            "buy": "https://www.fotocasa.es/es/comprar/viviendas/espana/todas-las-zonas/l",
        },
        description="Spain residential search portal.",
    ),
    PropertyProviderSpec(
        key="habitaclia",
        label="Habitaclia",
        country_code="ES",
        host_markers=("habitaclia.com",),
        listing_path_markers=("/comprar-", "/alquiler-"),
        search_urls={
            "rent": "https://www.habitaclia.com/alquiler.htm",
            "buy": "https://www.habitaclia.com/comprar.htm",
        },
        description="Spain portal with stronger Catalonia inventory but useful broader feeds.",
    ),
    PropertyProviderSpec(
        key="boe_subastas_es",
        label="BOE Subastas",
        country_code="ES",
        host_markers=("subastas.boe.es", "sedejudicial.justicia.es"),
        listing_path_markers=("/subastas/", "idSub=", "/buscar.php"),
        search_urls={
            "buy": "https://subastas.boe.es/subastas_ava.php?campo%5B0%5D=SUBASTA.INMUEBLES",
        },
        description="Spain official electronic judicial and administrative auction portal for real estate.",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="immobiliare",
        label="Immobiliare.it",
        country_code="IT",
        host_markers=("immobiliare.it",),
        listing_path_markers=("/annunci/",),
        search_urls={
            "rent": "https://www.immobiliare.it/affitto-case/",
            "buy": "https://www.immobiliare.it/vendita-case/",
        },
        description="Italy flagship residential marketplace.",
    ),
    PropertyProviderSpec(
        key="idealista_it",
        label="Idealista Italy",
        country_code="IT",
        host_markers=("idealista.it",),
        listing_path_markers=("/immobile/",),
        search_urls={
            "rent": "https://www.idealista.it/affitto-case/",
            "buy": "https://www.idealista.it/vendita-case/",
        },
        description="Italy branch of Idealista with broad urban inventory.",
    ),
    PropertyProviderSpec(
        key="casa_it",
        label="Casa.it",
        country_code="IT",
        host_markers=("casa.it",),
        listing_path_markers=("/immobili/",),
        search_urls={
            "rent": "https://www.casa.it/affitto/residenziale/",
            "buy": "https://www.casa.it/vendita/residenziale/",
        },
        description="Italy residential search portal.",
    ),
    PropertyProviderSpec(
        key="aste_giudiziarie_it",
        label="Aste Giudiziarie",
        country_code="IT",
        host_markers=("astegiudiziarie.it",),
        listing_path_markers=("/vendita/", "/asta-giudiziaria/", "/immobili/"),
        search_urls={
            "buy": "https://www.astegiudiziarie.it/",
        },
        description="Italy judicial real-estate auction portal centered on court-published asset sales.",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="seloger",
        label="SeLoger",
        country_code="FR",
        host_markers=("seloger.com",),
        listing_path_markers=("/annonces/",),
        search_urls={
            "rent": "https://www.seloger.com/list.htm?projects=1&types=1",
            "buy": "https://www.seloger.com/list.htm?projects=2&types=1",
        },
        description="France flagship portal with structured listing pages.",
    ),
    PropertyProviderSpec(
        key="bienici",
        label="Bien'ici",
        country_code="FR",
        host_markers=("bienici.com",),
        listing_path_markers=("/annonce/",),
        search_urls={
            "rent": "https://www.bienici.com/recherche/location/france",
            "buy": "https://www.bienici.com/recherche/achat/france",
        },
        description="France map-heavy search portal.",
    ),
    PropertyProviderSpec(
        key="leboncoin_immo",
        label="Leboncoin Immobilier",
        country_code="FR",
        host_markers=("leboncoin.fr",),
        listing_path_markers=("/ad/",),
        search_urls={
            "rent": "https://www.leboncoin.fr/recherche?category=10&real_estate_type=2",
            "buy": "https://www.leboncoin.fr/recherche?category=9&real_estate_type=1",
        },
        description="France classifieds lane with residential supply.",
    ),
    PropertyProviderSpec(
        key="avoventes_fr",
        label="Avoventes",
        country_code="FR",
        host_markers=("avoventes.fr",),
        listing_path_markers=("/annonce/", "/vente-judiciaire/", "/encheres/"),
        search_urls={
            "buy": "https://avoventes.fr/",
        },
        description="France national public auction announcement platform for judicial real-estate sales.",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="funda",
        label="Funda",
        country_code="NL",
        host_markers=("funda.nl",),
        listing_path_markers=("/detail/",),
        search_urls={
            "rent": "https://www.funda.nl/zoeken/huur/",
            "buy": "https://www.funda.nl/zoeken/koop/",
        },
        description="Netherlands flagship portal.",
    ),
    PropertyProviderSpec(
        key="pararius",
        label="Pararius",
        country_code="NL",
        host_markers=("pararius.com", "pararius.nl"),
        listing_path_markers=("/apartment-for-rent/", "/huis-te-huur/"),
        search_urls={
            "rent": "https://www.pararius.com/apartments",
            "buy": "https://www.pararius.com/houses-for-sale",
        },
        description="Netherlands rental-heavy portal.",
    ),
    PropertyProviderSpec(
        key="veilingdeurwaarder_nl",
        label="Veilingdeurwaarder",
        country_code="NL",
        host_markers=("veilingdeurwaarder.nl",),
        listing_path_markers=("/veiling/", "/executieveiling/", "/kavel/"),
        search_urls={
            "buy": "https://www.veilingdeurwaarder.nl/zoeken/",
        },
        description="Netherlands public sale and executieveiling portal tied to judicial officers.",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="idealista_pt",
        label="Idealista Portugal",
        country_code="PT",
        host_markers=("idealista.pt",),
        listing_path_markers=("/imovel/",),
        search_urls={
            "rent": "https://www.idealista.pt/en/arrendar-casas/",
            "buy": "https://www.idealista.pt/en/comprar-casas/",
        },
        description="Portugal branch of Idealista with strong Lisbon and Porto coverage.",
    ),
    PropertyProviderSpec(
        key="imovirtual",
        label="Imovirtual",
        country_code="PT",
        host_markers=("imovirtual.com",),
        listing_path_markers=("/imovel/",),
        search_urls={
            "rent": "https://www.imovirtual.com/arrendar/apartamento/",
            "buy": "https://www.imovirtual.com/comprar/apartamento/",
        },
        description="Portugal residential search portal with broad rental coverage.",
    ),
    PropertyProviderSpec(
        key="casa_sapo",
        label="Casa Sapo",
        country_code="PT",
        host_markers=("casa.sapo.pt",),
        listing_path_markers=("/detalhes/",),
        search_urls={
            "rent": "https://casa.sapo.pt/en-gb/rent-apartments/",
            "buy": "https://casa.sapo.pt/en-gb/buy-apartments/",
        },
        description="Portugal property portal with agency inventory.",
    ),
    PropertyProviderSpec(
        key="citius_exec_pt",
        label="Citius Judicial Sales",
        country_code="PT",
        host_markers=("citius.mj.pt", "portaldasfinancas.gov.pt"),
        listing_path_markers=("/consultasvenda.aspx", "/bens/", "/venda/"),
        search_urls={
            "buy": "https://www.citius.mj.pt/portal/consultas/consultasvenda.aspx/1000",
        },
        description="Portugal public portal for judicial and tax-enforcement sales of seized property.",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="otodom",
        label="Otodom",
        country_code="PL",
        host_markers=("otodom.pl",),
        listing_path_markers=("/pl/oferta/",),
        search_urls={
            "rent": "https://www.otodom.pl/pl/wyniki/wynajem/mieszkanie/cala-polska",
            "buy": "https://www.otodom.pl/pl/wyniki/sprzedaz/mieszkanie/cala-polska",
        },
        description="Poland flagship property portal.",
    ),
    PropertyProviderSpec(
        key="olx_pl_nieruchomosci",
        label="OLX Nieruchomości",
        country_code="PL",
        host_markers=("olx.pl",),
        listing_path_markers=("/d/oferta/",),
        search_urls={
            "rent": "https://www.olx.pl/nieruchomosci/mieszkania/wynajem/",
            "buy": "https://www.olx.pl/nieruchomosci/mieszkania/sprzedaz/",
        },
        description="Poland classifieds lane for residential supply.",
    ),
    PropertyProviderSpec(
        key="komornik_elicytacje_pl",
        label="Komornik e-Licytacje",
        country_code="PL",
        host_markers=("elicytacje.komornik.pl", "ool.komornik.pl"),
        listing_path_markers=("/licytacje/", "/items/", "/obwieszczenia/"),
        search_urls={
            "buy": "https://elicytacje.komornik.pl/",
        },
        description="Poland official bailiff auction portal for court-enforced real-estate sales.",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="hemnet",
        label="Hemnet",
        country_code="SE",
        host_markers=("hemnet.se",),
        listing_path_markers=("/bostad/",),
        search_urls={
            "rent": "https://www.hemnet.se/bostader",
            "buy": "https://www.hemnet.se/bostader",
        },
        description="Sweden flagship property portal focused on sale inventory.",
    ),
    PropertyProviderSpec(
        key="booli",
        label="Booli",
        country_code="SE",
        host_markers=("booli.se",),
        listing_path_markers=("/bostad/",),
        search_urls={
            "rent": "https://www.booli.se/sok/bostad",
            "buy": "https://www.booli.se/sok/till-salu",
        },
        description="Sweden marketplace and valuation surface for home search.",
    ),
    PropertyProviderSpec(
        key="kronofogden_auktionstorget_se",
        label="Kronofogden Auktionstorget",
        country_code="SE",
        host_markers=("auktionstorget.kronofogden.se",),
        listing_path_markers=(".html",),
        search_urls={
            "buy": "https://auktionstorget.kronofogden.se/auktionstorget",
        },
        description="Sweden Enforcement Authority auction market for seized real estate and housing rights.",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="zillow",
        label="Zillow",
        country_code="US",
        host_markers=("zillow.com",),
        listing_path_markers=("/_zpid/",),
        search_urls={
            "rent": "https://www.zillow.com/homes/for_rent/",
            "buy": "https://www.zillow.com/homes/for_sale/",
        },
        description="United States large-scale residential search portal.",
    ),
    PropertyProviderSpec(
        key="realtor",
        label="Realtor.com",
        country_code="US",
        host_markers=("realtor.com",),
        listing_path_markers=("/realestateandhomes-detail/",),
        search_urls={
            "rent": "https://www.realtor.com/apartments",
            "buy": "https://www.realtor.com/realestateandhomes-search",
        },
        description="United States residential marketplace with structured detail pages.",
    ),
    PropertyProviderSpec(
        key="apartments",
        label="Apartments.com",
        country_code="US",
        host_markers=("apartments.com",),
        listing_path_markers=("/apartments/", "/house/", "/condo/"),
        search_urls={
            "rent": "https://www.apartments.com/",
            "buy": "https://www.apartments.com/",
        },
        description="United States rental-heavy apartment portal.",
        supported_listing_modes=("rent",),
    ),
    PropertyProviderSpec(
        key="realtor_ca",
        label="Realtor.ca",
        country_code="CA",
        host_markers=("realtor.ca",),
        listing_path_markers=("/real-estate/",),
        search_urls={
            "rent": "https://www.realtor.ca/on/rent",
            "buy": "https://www.realtor.ca/",
        },
        description="Canada national residential portal.",
    ),
    PropertyProviderSpec(
        key="rew_ca",
        label="REW",
        country_code="CA",
        host_markers=("rew.ca",),
        listing_path_markers=("/properties/",),
        search_urls={
            "rent": "https://www.rew.ca/rentals",
            "buy": "https://www.rew.ca/properties",
        },
        description="Canada residential search portal with stronger western market coverage.",
    ),
    PropertyProviderSpec(
        key="rentals_ca",
        label="Rentals.ca",
        country_code="CA",
        host_markers=("rentals.ca",),
        listing_path_markers=("/city/", "/property/"),
        search_urls={
            "rent": "https://rentals.ca/",
            "buy": "https://rentals.ca/",
        },
        description="Canada rental-focused apartment portal.",
        supported_listing_modes=("rent",),
    ),
    PropertyProviderSpec(
        key="encuentra24_cr",
        label="Encuentra24 Costa Rica",
        country_code="CR",
        host_markers=("encuentra24.com",),
        listing_path_markers=("/costa-rica-en/real-estate", "/costa-rica-es/bienes-raices"),
        search_urls={
            "rent": "https://www.encuentra24.com/costa-rica-en/real-estate-for-rent",
            "buy": "https://www.encuentra24.com/costa-rica-en/real-estate-for-sale",
        },
        description="Costa Rica broad-market classifieds portal with large residential rent and sale inventory.",
    ),
    PropertyProviderSpec(
        key="re_cr_mls",
        label="RE.cr Costa Rica MLS",
        country_code="CR",
        host_markers=("re.cr",),
        listing_path_markers=("/en/costa-rica-real-estate/", "/en/real-estate/", "/property/"),
        search_urls={
            "rent": "https://www.re.cr/en/costa-rica-real-estate",
            "buy": "https://www.re.cr/en/costa-rica-real-estate",
        },
        description="Costa Rica independent MLS network for residential sales, rentals, land, and investment opportunities.",
        family="mls",
        trust_tier="standard",
    ),
    PropertyProviderSpec(
        key="realtor_cr",
        label="Realtor.com International Costa Rica",
        country_code="CR",
        host_markers=("realtor.com",),
        listing_path_markers=("/international/cr/", "/international/costa-rica/"),
        search_urls={
            "buy": "https://www.realtor.com/international/cr/",
        },
        description="Realtor.com international Costa Rica sale inventory, useful as a broad English-language buyer lane.",
        family="marketplace",
        trust_tier="standard",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="properstar_cr",
        label="Properstar Costa Rica",
        country_code="CR",
        host_markers=("properstar.com", "www.properstar.com"),
        listing_path_markers=("/costa-rica", "/buy", "/rent", "/property/"),
        search_urls={
            "rent": "https://www.properstar.com/costa-rica/rent/apartment-house",
            "buy": "https://www.properstar.com/costa-rica/buy",
        },
        description="Costa Rica international marketplace lane for residential sale and rental discovery. Plain HTTP probes are blocked, so it must use the browser-backed provider path.",
        family="marketplace",
        trust_tier="standard",
        coverage="national",
        floorplan_reliability="low",
        duplicate_rate="medium",
        tour_availability="rare",
        scan_reliability="browser_required",
        filter_pushdown_strength="partial",
        official_source_quality="provider_only",
        last_verified="2026-06-29",
        access_mode="browser_public_web",
        browser_access_allowed=True,
        maximum_concurrency=1,
        requests_per_hour=20,
    ),
    PropertyProviderSpec(
        key="coldwellbanker_cr",
        label="Coldwell Banker Costa Rica",
        country_code="CR",
        host_markers=("coldwellbankercostarica.com",),
        listing_path_markers=("/property/", "/properties/", "/listing/"),
        search_urls={
            "buy": "https://www.coldwellbankercostarica.com/",
        },
        description="Costa Rica national Coldwell Banker broker network for residential, beachfront, land, and luxury sale inventory.",
        family="broker_direct",
        trust_tier="standard",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="century21_cr",
        label="Century 21 Costa Rica",
        country_code="CR",
        host_markers=("century21costarica.com", "www.century21costarica.com"),
        listing_path_markers=("/en/", "/property/", "/properties/", "/real-estate/"),
        search_urls={
            "buy": "https://www.century21costarica.com/en/",
        },
        description="Costa Rica Century 21 broker network lane for residential sale inventory and regional broker offices.",
        family="broker_direct",
        trust_tier="standard",
        supported_listing_modes=("buy",),
        coverage="national",
        floorplan_reliability="low",
        duplicate_rate="medium",
        tour_availability="occasional",
        scan_reliability="standard",
        filter_pushdown_strength="weak",
        official_source_quality="broker_primary",
        last_verified="2026-06-29",
    ),
    PropertyProviderSpec(
        key="remax_cr",
        label="RE/MAX Costa Rica",
        country_code="CR",
        host_markers=("remax-costa-rica.com", "www.remax-costa-rica.com"),
        listing_path_markers=("/Properties-Propiedades/", "/property/", "/properties/", "/listing/"),
        search_urls={
            "buy": "https://www.remax-costa-rica.com/Properties-Propiedades/properties-for-sale-orotina-ruta-34-costanera/",
        },
        description="Costa Rica RE/MAX broker network lane for national residential and investment sale inventory.",
        family="broker_direct",
        trust_tier="standard",
        supported_listing_modes=("buy",),
        coverage="national",
        floorplan_reliability="low",
        duplicate_rate="medium",
        tour_availability="occasional",
        scan_reliability="standard",
        filter_pushdown_strength="partial",
        official_source_quality="broker_primary",
        last_verified="2026-06-29",
    ),
    PropertyProviderSpec(
        key="theagency_cr",
        label="The Agency Costa Rica",
        country_code="CR",
        host_markers=("ta.cr",),
        listing_path_markers=("/properties/", "/property/", "/listings/"),
        search_urls={
            "rent": "https://ta.cr/",
            "buy": "https://ta.cr/",
        },
        description="Costa Rica broker-direct inventory for beach, city, and mountain property, with both sale and rent search lanes.",
        family="broker_direct",
        trust_tier="standard",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="krain_cr",
        label="KRAIN Costa Rica",
        country_code="CR",
        host_markers=("kraincostarica.com",),
        listing_path_markers=("/en/", "/property/", "/listings/"),
        search_urls={
            "buy": "https://kraincostarica.com/en",
        },
        description="Costa Rica broker-direct search focused on Guanacaste, Central Valley, Caribbean, and coastal residential and land inventory.",
        family="broker_direct",
        trust_tier="standard",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="desarrollos_cr",
        label="Desarrollos Costa Rica",
        country_code="CR",
        host_markers=("desarrollos.cr",),
        listing_path_markers=("/en/", "/es/", "/development/", "/property/"),
        search_urls={
            "rent": "https://www.desarrollos.cr/en",
            "buy": "https://www.desarrollos.cr/en",
        },
        description="Costa Rica developments portal covering apartments, condos, duplexes, home sites, and development parcels.",
        family="developer_projects",
        trust_tier="standard",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="propertiesincostarica_cr",
        label="Properties in Costa Rica",
        country_code="CR",
        host_markers=("propertiesincostarica.com",),
        listing_path_markers=("/properties/", "/property/", "/real-estate/"),
        search_urls={
            "buy": "https://www.propertiesincostarica.com/properties/",
        },
        description="Costa Rica broker-direct portal covering San Jose, Uvita, Tamarindo, luxury, beach, investment, farms, and mountain properties.",
        family="broker_direct",
        trust_tier="standard",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="tierraverde_cr",
        label="Tierra Verde",
        country_code="CR",
        host_markers=("tierraverde.cr",),
        listing_path_markers=("/projects", "/project/", "/property/"),
        search_urls={
            "buy": "https://www.tierraverde.cr/",
        },
        description="Costa Rica sustainable residential developer project lane, currently centered on Santa Teresa developments.",
        family="developer_projects",
        trust_tier="standard",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="costaricarealestateservice_cr",
        label="Costa Rica Real Estate Service",
        country_code="CR",
        host_markers=("costaricarealestateservice.com",),
        listing_path_markers=("/property/", "/properties/", "/listings/"),
        search_urls={
            "buy": "https://costaricarealestateservice.com/properties/",
        },
        description="Dominical and South Pacific Costa Rica broker-direct inventory, including Uvita, Ojochal, Quepos, and specialty land/waterfall properties.",
        family="broker_direct",
        trust_tier="restricted",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="twocostaricarealestate_cr",
        label="2CostaRicaRealEstate",
        country_code="CR",
        host_markers=("2costaricarealestate.com",),
        listing_path_markers=("/property/", "/properties/", "/real-estate/"),
        search_urls={
            "buy": "https://www.2costaricarealestate.com/",
        },
        description="Costa Rica broker portal with beach, city, Tamarindo, Dominical, Jaco, Manuel Antonio, and Central Valley inventory.",
        family="broker_direct",
        trust_tier="standard",
        supported_listing_modes=("buy",),
    ),
    PropertyProviderSpec(
        key="treasury_real_property_us",
        label="Treasury Real Property Auctions",
        country_code="US",
        host_markers=("treasury.gov",),
        listing_path_markers=("/auctions/treasury/rp/",),
        search_urls={
            "buy": "https://www.treasury.gov/auctions/treasury/rp/index.shtml",
        },
        description="United States federal seized-real-property auction listings open to the public.",
        supported_listing_modes=("buy",),
    ),
)


EVIDENCE_SOURCES: tuple[PropertyEvidenceSourceSpec, ...] = (
    PropertyEvidenceSourceSpec(
        key="basemap_at",
        label="basemap.at",
        country_code="AT",
        evidence_family="base_map",
        description="Austria official basemap and administrative-geodata layer for mapping, parcel context, and location verification.",
        source_type="official_public_data",
        confidence="high",
    ),
    PropertyEvidenceSourceSpec(
        key="gip_at",
        label="GIP Austria",
        country_code="AT",
        evidence_family="transport_graph",
        description="Austria official transport graph for walking, cycling, transit, and driving reachability research.",
        source_type="official_public_data",
        confidence="high",
    ),
    PropertyEvidenceSourceSpec(
        key="wienerlinien_ogd_at",
        label="Wiener Linien Open Data",
        country_code="AT",
        evidence_family="transit",
        description="Vienna transit evidence for stop access, departures, and mobility context on Wiener Linien served routes.",
        source_type="official_public_data",
        confidence="high",
    ),
    PropertyEvidenceSourceSpec(
        key="statatlas_schulen_at",
        label="STATatlas Schulen",
        country_code="AT",
        evidence_family="school_evidence",
        description="Austria school evidence layer for school types, regional school presence, and school-stage match without claiming a synthetic score.",
        source_type="official_public_data",
        confidence="high",
    ),
    PropertyEvidenceSourceSpec(
        key="wien_schulen_ogd_at",
        label="Wien Schulen OGD",
        country_code="AT",
        evidence_family="school_evidence",
        description="Vienna school-location evidence for Volksschule, AHS, and other school types with official city geodata.",
        source_type="official_public_data",
        confidence="high",
    ),
    PropertyEvidenceSourceSpec(
        key="wien_kindergarten_ogd_at",
        label="Wien Kindergarten OGD",
        country_code="AT",
        evidence_family="childcare",
        description="Vienna childcare evidence for kindergarten and childcare proximity, operator, and care-form context.",
        source_type="official_public_data",
        confidence="medium",
    ),
    PropertyEvidenceSourceSpec(
        key="laerminfo_at",
        label="Lärminfo Austria",
        country_code="AT",
        evidence_family="noise",
        description="Austria strategic noise-mapping evidence for road, rail, airport, and metropolitan environmental noise exposure.",
        source_type="official_public_data",
        confidence="high",
    ),
    PropertyEvidenceSourceSpec(
        key="hora_at",
        label="HORA",
        country_code="AT",
        evidence_family="natural_hazards",
        description="Austria natural-hazard overview for flood, runoff, storm, hail, snow, and related exposure checks.",
        source_type="official_public_data",
        confidence="high",
    ),
    PropertyEvidenceSourceSpec(
        key="geosphere_at",
        label="GeoSphere Austria",
        country_code="AT",
        evidence_family="climate",
        description="Austria weather and climate evidence for heat, precipitation, climate normals, and environmental context.",
        source_type="official_public_data",
        confidence="high",
    ),
    PropertyEvidenceSourceSpec(
        key="wien_klimaanalyse_ogd_at",
        label="Wien Klimaanalysekarte OGD",
        country_code="AT",
        evidence_family="climate",
        description="Vienna open-data climate-analysis evidence for urban heat, cool-air context, and heat-resilience checks.",
        source_type="official_public_data",
        confidence="high",
    ),
    PropertyEvidenceSourceSpec(
        key="uba_luft_at",
        label="Umweltbundesamt Luft",
        country_code="AT",
        evidence_family="air_quality",
        description="Austria air-quality evidence from national and provincial measuring stations for PM, NO2, ozone, and station-backed exposure context.",
        source_type="official_public_data",
        confidence="high",
    ),
    PropertyEvidenceSourceSpec(
        key="breitbandatlas_at",
        label="Breitbandatlas Austria",
        country_code="AT",
        evidence_family="broadband",
        description="Austria broadband-availability evidence for fixed and mobile infrastructure, home-office confidence, and rollout context.",
        source_type="official_public_data",
        confidence="high",
    ),
    PropertyEvidenceSourceSpec(
        key="edikte_justiz_at",
        label="Ediktsdatei Justiz",
        country_code="AT",
        evidence_family="auction_legal",
        description="Austria official judicial publication evidence for real-estate auctions, appraisals, floorplans, and legal sale context.",
        source_type="official_public_data",
        confidence="high",
    ),
    PropertyEvidenceSourceSpec(
        key="osm_overpass_de",
        label="OpenStreetMap / Overpass",
        country_code="DE",
        evidence_family="poi_and_walkability",
        description="Germany point-of-interest and local proximity evidence for schools, parks, pharmacies, and transit stops.",
        source_type="public_open_data",
        confidence="medium",
        license_label="Open Database License / OpenStreetMap attribution",
        refresh_cadence="community-edited; snapshot before use",
        downstream_use="derived_proximity_evidence_with_attribution",
        geographic_granularity="poi_or_way",
    ),
    PropertyEvidenceSourceSpec(
        key="mobilithek_gtfs_de",
        label="Mobilithek / GTFS",
        country_code="DE",
        evidence_family="transit",
        description="Germany mobility and timetable evidence for public transport reachability and commute research.",
        source_type="official_public_data",
        confidence="high",
    ),
    PropertyEvidenceSourceSpec(
        key="uba_air_de",
        label="UBA Air Quality",
        country_code="DE",
        evidence_family="air_quality",
        description="Germany Umweltbundesamt air-quality evidence for PM, NO2, and long-term exposure context.",
        source_type="official_public_data",
        confidence="high",
    ),
    PropertyEvidenceSourceSpec(
        key="uba_noise_de",
        label="UBA Noise Maps",
        country_code="DE",
        evidence_family="noise",
        description="Germany official environmental noise evidence from road, rail, airport, and agglomeration noise mapping.",
        source_type="official_public_data",
        confidence="high",
    ),
    PropertyEvidenceSourceSpec(
        key="dwd_climate_de",
        label="DWD Climate Data",
        country_code="DE",
        evidence_family="climate",
        description="Germany weather and climate evidence for historical temperature, rainfall, and climate-stress context.",
        source_type="official_public_data",
        confidence="high",
    ),
    PropertyEvidenceSourceSpec(
        key="bnetza_breitband_de",
        label="Bundesnetzagentur Breitbandatlas",
        country_code="DE",
        evidence_family="broadband",
        description="Germany broadband and mobile-coverage evidence for home-office and infrastructure confidence.",
        source_type="official_public_data",
        confidence="high",
    ),
    PropertyEvidenceSourceSpec(
        key="destatis_regionalatlas_de",
        label="Destatis Regionalatlas",
        country_code="DE",
        evidence_family="regional_context",
        description="Germany regional socioeconomic and education-context evidence for district-level housing and population context.",
        source_type="official_public_data",
        confidence="high",
    ),
    PropertyEvidenceSourceSpec(
        key="bbsr_inkar_de",
        label="BBSR INKAR",
        country_code="DE",
        evidence_family="regional_context",
        description="Germany regional indicator evidence for demography, labour, housing pressure, and accessibility context.",
        source_type="official_public_data",
        confidence="high",
    ),
    PropertyEvidenceSourceSpec(
        key="bkg_geodata_de",
        label="BKG Geodatenzentrum",
        country_code="DE",
        evidence_family="geodata",
        description="Germany official geodata and boundary evidence for municipality, district, and mapping verification.",
        source_type="official_public_data",
        confidence="high",
    ),
    PropertyEvidenceSourceSpec(
        key="school_directories_de",
        label="State School Directories",
        country_code="DE",
        evidence_family="school_evidence",
        description="Germany school evidence layer for school type, school-stage match, and distance without collapsing federal school data into one synthetic rating.",
        source_type="official_public_data",
        confidence="medium",
    ),
    PropertyEvidenceSourceSpec(
        key="kita_directories_de",
        label="City Kita Directories",
        country_code="DE",
        evidence_family="childcare",
        description="Germany childcare evidence layer for Kita proximity and public/private childcare availability context.",
        source_type="official_public_data",
        confidence="medium",
    ),
    PropertyEvidenceSourceSpec(
        key="flood_maps_de",
        label="State Flood Maps",
        country_code="DE",
        evidence_family="flood_risk",
        description="Germany state flood-risk evidence for flood hazard, nearby water exposure, and climate-resilience review.",
        source_type="official_public_data",
        confidence="high",
    ),
    PropertyEvidenceSourceSpec(
        key="xplanung_bplan_de",
        label="City Planning / XPlanung",
        country_code="DE",
        evidence_family="future_change",
        description="Germany planning evidence for zoning, nearby construction, and future-change risk signals.",
        source_type="official_public_data",
        confidence="medium",
    ),
)


_COUNTRY_INDEX = {row.code: row for row in COUNTRIES}
_COUNTRY_ALIAS_INDEX: dict[str, str] = {}


def _country_alias_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


for _country in COUNTRIES:
    _COUNTRY_ALIAS_INDEX[_country_alias_key(_country.code)] = _country.code
    _COUNTRY_ALIAS_INDEX[_country_alias_key(_country.label)] = _country.code

_COUNTRY_ALIAS_INDEX.update(
    {
        "austria": "AT",
        "belgium": "BE",
        "canada": "CA",
        "costarica": "CR",
        "costarican": "CR",
        "cr": "CR",
        "germany": "DE",
        "switzerland": "CH",
        "ireland": "IE",
        "unitedkingdom": "UK",
        "gb": "UK",
        "greatbritain": "UK",
        "britain": "UK",
        "england": "UK",
        "australia": "AU",
        "spain": "ES",
        "italy": "IT",
        "france": "FR",
        "netherlands": "NL",
        "holland": "NL",
        "portugal": "PT",
        "poland": "PL",
        "sweden": "SE",
        "unitedstates": "US",
        "unitedstatesofamerica": "US",
        "usa": "US",
    }
)
_PROVIDER_INDEX = {row.key: row for row in PROVIDERS}
_EVIDENCE_SOURCE_INDEX = {row.key: row for row in EVIDENCE_SOURCES}
_LANGUAGE_INDEX = {code: label for code, label in LANGUAGES}


PROPERTY_PLATFORM_ALIAS_MAP: dict[str, str] = {
    "willhaben": "willhaben",
    "immmo": "immmo",
    "kalandra": "kalandra",
    "remax": "remax_at",
    "remaxat": "remax_at",
    "remax_at": "remax_at",
    "remaxaustria": "remax_at",
    "glorit": "glorit_at",
    "gloritat": "glorit_at",
    "glorit_at": "glorit_at",
    "genossenschaften": "genossenschaften_at",
    "genossenschaft": "genossenschaften_at",
    "cooperatives": "genossenschaften_at",
    "immoscout": "immoscout_at",
    "immoscout24": "immoscout_at",
    "immoscoutat": "immoscout_at",
    "immoweltat": "immowelt_at",
    "immowelt_at": "immowelt_at",
    "immobiliennet": "immobilien_net_at",
    "immobiliennetat": "immobilien_net_at",
    "ohnemaklerat": "ohne_makler_at",
    "ohne_makler_at": "ohne_makler_at",
    "sreal": "sreal_at",
    "srealat": "sreal_at",
    "raiffeisenimmobilien": "raiffeisen_immobilien_at",
    "raiffeisenimmobilienat": "raiffeisen_immobilien_at",
    "wohnnet": "wohnnet_at",
    "wohnnetat": "wohnnet_at",
    "keinmakler": "keinmakler_at",
    "keinmaklerat": "keinmakler_at",
    "findmyhome": "findmyhome_at",
    "findmyhomeat": "findmyhome_at",
    "findmyhome_at": "findmyhome_at",
    "wag": "wag_at",
    "wohnberatungwien": "wohnberatung_wien",
    "wienerwohnen": "wiener_wohnen",
    "gesiba": "gesiba_at",
    "oesw": "oesw_at",
    "egw": "egw_at",
    "heimatoesterreich": "heimat_oesterreich_at",
    "heimatosterreich": "heimat_oesterreich_at",
    "bwsg": "bwsg_at",
    "wiensued": "wiensued_at",
    "ebg": "ebg_wohnen_at",
    "ebgwohnen": "ebg_wohnen_at",
    "ooewohnbau": "ooe_wohnbau_at",
    "salzburgwohnbau": "salzburg_wohnbau_at",
    "oevw": "oevw_at",
    "derstandard": "derstandard_at",
    "standard": "derstandard_at",
    "derstandardat": "derstandard_at",
    "standardat": "derstandard_at",
    "immobilienderstandard": "derstandard_at",
    "immobilienderstandardat": "derstandard_at",
    "theagencycr": "theagency_cr",
    "agencycr": "theagency_cr",
    "properstarcr": "properstar_cr",
    "century21cr": "century21_cr",
    "remaxcr": "remax_cr",
    "krain": "krain_cr",
    "kraincr": "krain_cr",
    "desarrollos": "desarrollos_cr",
    "desarrolloscr": "desarrollos_cr",
    "tierraverde": "tierraverde_cr",
    "tierraverdecr": "tierraverde_cr",
    "justizedikte": "justiz_edikte_at",
    "edikte": "justiz_edikte_at",
    "zvginfo": "zvginfo_at",
    "immobilienscout": "immoscout_de",
    "immobilienscout24": "immoscout_de",
    "immobilienscout24de": "immoscout_de",
    "immoscoutde": "immoscout_de",
    "coreportalsde": "core_portals_de",
    "core_portals_de": "core_portals_de",
    "sharedhousingde": "shared_housing_de",
    "shared_housing_de": "shared_housing_de",
    "corporatelandlordsde": "corporate_landlords_de",
    "corporate_landlords_de": "corporate_landlords_de",
    "municipalhousingde": "municipal_housing_de",
    "municipal_housing_de": "municipal_housing_de",
    "cooperativesde": "cooperatives_de",
    "cooperatives_de": "cooperatives_de",
    "newbuildde": "new_build_de",
    "new_build_de": "new_build_de",
    "auctionsde": "auctions_de",
    "auctions_de": "auctions_de",
    "brokerdirectde": "broker_direct_de",
    "broker_direct_de": "broker_direct_de",
    "furnishedrelocationde": "furnished_relocation_de",
    "furnished_relocation_de": "furnished_relocation_de",
    "immoscoutch": "immoscout_ch",
    "immowelt": "immowelt",
    "meinestadt": "meinestadt_de",
    "meinestadtde": "meinestadt_de",
    "immonet": "immonet",
    "kleinanzeigen": "kleinanzeigen_immo",
    "kleinanzeigenimmo": "kleinanzeigen_immo",
    "wohnungsboerse": "wohnungsboerse_de",
    "wohnungsboersede": "wohnungsboerse_de",
    "wohnungsboerse_de": "wohnungsboerse_de",
    "neubaukompass": "neubaukompass_de",
    "neubaukompassde": "neubaukompass_de",
    "wggesucht": "wg_gesucht_de",
    "wg-gesucht": "wg_gesucht_de",
    "vonovia": "vonovia_de",
    "legwohnen": "leg_wohnen_de",
    "tagwohnen": "tag_wohnen_de",
    "degewo": "degewo_berlin",
    "saga": "saga_hamburg",
    "wohnprojekteportal": "wohnprojekte_portal_de",
    "portalzvg": "portal_zvg_de",
    "zvnow": "zvnow_de",
    "ohnemakler": "ohne_makler_de",
    "ohnemaklerde": "ohne_makler_de",
    "ohne-makler": "ohne_makler_de",
    "vonpoll": "von_poll_de",
    "vonpollde": "von_poll_de",
    "vonpollimmobilien": "von_poll_de",
    "homegate": "homegate",
    "newhome": "newhome",
    "immoweb": "immoweb",
    "zimmo": "zimmo",
    "biddit": "biddit_be",
    "taxsalesportal": "taxsales_ca",
    "daft": "daft_ie",
    "daftie": "daft_ie",
    "myhome": "myhome_ie",
    "myhomeie": "myhome_ie",
    "youbid": "youbid_ie",
    "rightmove": "rightmove",
    "zoopla": "zoopla",
    "onthemarket": "onthemarket",
    "repolist": "repolist_uk",
    "realestateau": "realestate_au",
    "realestatecomau": "realestate_au",
    "domain": "domain_au",
    "flatmates": "flatmates_au",
    "mortgageeau": "mortgagee_au",
    "idealista": "idealista_es",
    "idealistaes": "idealista_es",
    "idealistait": "idealista_it",
    "idealistapt": "idealista_pt",
    "fotocasa": "fotocasa",
    "habitaclia": "habitaclia",
    "boesubastas": "boe_subastas_es",
    "immobiliare": "immobiliare",
    "astegiudiziarie": "aste_giudiziarie_it",
    "casait": "casa_it",
    "casa": "casa_it",
    "seloger": "seloger",
    "bienici": "bienici",
    "leboncoin": "leboncoin_immo",
    "leboncoinimmo": "leboncoin_immo",
    "avoventes": "avoventes_fr",
    "funda": "funda",
    "pararius": "pararius",
    "veilingdeurwaarder": "veilingdeurwaarder_nl",
    "imovirtual": "imovirtual",
    "casasapo": "casa_sapo",
    "citiusexec": "citius_exec_pt",
    "otodom": "otodom",
    "olxpl": "olx_pl_nieruchomosci",
    "olxnieruchomosci": "olx_pl_nieruchomosci",
    "komornik": "komornik_elicytacje_pl",
    "hemnet": "hemnet",
    "booli": "booli",
    "kronofogden": "kronofogden_auktionstorget_se",
    "zillow": "zillow",
    "realtor": "realtor",
    "apartments": "apartments",
    "realtorca": "realtor_ca",
    "rew": "rew_ca",
    "rentalsca": "rentals_ca",
    "encuentra24": "encuentra24_cr",
    "encuentra24cr": "encuentra24_cr",
    "encuentra24_cr": "encuentra24_cr",
    "recr": "re_cr_mls",
    "re_cr": "re_cr_mls",
    "recrmls": "re_cr_mls",
    "costaricamls": "re_cr_mls",
    "realtorcr": "realtor_cr",
    "realtorcomcr": "realtor_cr",
    "realtor_cr": "realtor_cr",
    "coldwellbankercr": "coldwellbanker_cr",
    "coldwellbanker_cr": "coldwellbanker_cr",
    "coldwellbankercostarica": "coldwellbanker_cr",
    "propertiesincostarica": "propertiesincostarica_cr",
    "propertiesincostarica_cr": "propertiesincostarica_cr",
    "costaricarealestateservice": "costaricarealestateservice_cr",
    "costaricarealestateservice_cr": "costaricarealestateservice_cr",
    "2costaricarealestate": "twocostaricarealestate_cr",
    "twocostaricarealestate": "twocostaricarealestate_cr",
    "arwag": "arwag_at",
    "raiffeisenwohnbau": "raiffeisen_wohnbau_at",
    "leitgoebwohnbau": "leitgoeb_wohnbau_at",
    "leitgobwohnbau": "leitgoeb_wohnbau_at",
    "viktoriawohnbau": "viktoria_wohnbau_at",
    "twocostaricarealestate_cr": "twocostaricarealestate_cr",
    "treasuryrealproperty": "treasury_real_property_us",
    "zvg": "zvg_de",
    "auctionhome": "auctionhome_ch",
    "all": "all",
}


GROUPED_PROVIDER_SOURCE_MAP: dict[str, tuple[dict[str, object], ...]] = {
    "genossenschaften_at": (
        {
            "label": "GESIBA Wohnungen",
            "rent_url": "https://www.gesiba.at/immobilien/wohnungen",
            "buy_url": "https://www.gesiba.at/immobilien/wohnungen",
        },
        {
            "label": "Siedlungsunion Sofort",
            "rent_url": "https://www.siedlungsunion.at/wohnen/sofort",
            "buy_url": "https://www.siedlungsunion.at/wohnen/sofort",
        },
        {
            "label": "Sozialbau Projekte in Bau",
            "rent_url": "https://angebote.sozialbau.at/sobitvX/htmlprospect/home.xhtml?pq_scope=in_bau",
            "buy_url": "https://angebote.sozialbau.at/sobitvX/htmlprospect/home.xhtml?pq_scope=in_bau",
        },
        {
            "label": "Sozialbau Projekte in Planung",
            "rent_url": "https://angebote.sozialbau.at/sobitvX/htmlprospect/home.xhtml?pq_scope=in_planung",
            "buy_url": "https://angebote.sozialbau.at/sobitvX/htmlprospect/home.xhtml?pq_scope=in_planung",
        },
        {
            "label": "WBV-GPA Wohnungen",
            "rent_url": "https://www.wbv-gpa.at/wohnungen/",
            "buy_url": "https://www.wbv-gpa.at/wohnungen/",
        },
        {
            "label": "Frieden Immobiliensuche",
            "rent_url": "https://www.frieden.at/immobiliensuche",
            "buy_url": "https://www.frieden.at/immobiliensuche",
        },
        {
            "label": "EGW Immobiliensuche",
            "rent_url": "https://www.egw.at/suche",
            "buy_url": "https://www.egw.at/suche",
        },
        {
            "label": "ÖSW Sofort verfügbar",
            "rent_url": "https://www.oesw.at/immobilienangebot/sofort-wohnen.html",
            "buy_url": "https://www.oesw.at/immobilienangebot/sofort-wohnen.html",
        },
        {
            "label": "ÖSW In Bau",
            "rent_url": "https://www.oesw.at/immobilienangebot/in-bau.html",
            "buy_url": "https://www.oesw.at/immobilienangebot/in-bau.html",
        },
        {
            "label": "Familienwohnbau Angebote",
            "rent_url": "https://familienwohnbau.at/de/",
            "buy_url": "https://familienwohnbau.at/de/",
        },
        {
            "label": "WAG Wohngebiete",
            "rent_url": "https://www.wag.at/",
            "buy_url": "https://www.wag.at/",
            "supported_region_codes": ("upper_austria", "lower_austria", "salzburg", "styria"),
        },
        {
            "label": "Heimat Österreich",
            "rent_url": "https://www.heimat-oesterreich.at/de",
            "buy_url": "https://www.heimat-oesterreich.at/de",
        },
        {
            "label": "BWSG Immobilien",
            "rent_url": "https://www.bwsg.at/",
            "buy_url": "https://www.bwsg.at/",
        },
        {
            "label": "Wien-Süd Aktuelle Objekte",
            "rent_url": "https://www.wiensued.at/",
            "buy_url": "https://www.wiensued.at/",
        },
        {
            "label": "EBG Wohnen",
            "rent_url": "https://www.ebg-wohnen.at/",
            "buy_url": "https://www.ebg-wohnen.at/",
        },
        {
            "label": "OÖ Wohnbau",
            "rent_url": "https://ooewohnbau.at/",
            "buy_url": "https://ooewohnbau.at/",
            "supported_region_codes": ("upper_austria",),
        },
        {
            "label": "Salzburg Wohnbau Projekte",
            "rent_url": "https://www.salzburg-wohnbau.at/wohnbau-projekte/",
            "buy_url": "https://www.salzburg-wohnbau.at/wohnbau-projekte/",
            "supported_region_codes": ("salzburg",),
        },
        {
            "label": "ÖVW",
            "rent_url": "https://www.oevw.at/",
            "buy_url": "https://www.oevw.at/",
        },
    ),
    "broker_direct_at": (
        {
            "label": "Kalandra Direkt",
            "rent_url": "https://www.kalandra.at/immobiliensuche",
            "buy_url": "https://www.kalandra.at/immobiliensuche",
        },
    ),
    "developer_projects_at": (
        {
            "label": "Sozialbau Projekte in Bau",
            "rent_url": "https://angebote.sozialbau.at/sobitvX/htmlprospect/home.xhtml?pq_scope=in_bau",
            "buy_url": "https://angebote.sozialbau.at/sobitvX/htmlprospect/home.xhtml?pq_scope=in_bau",
        },
        {
            "label": "Sozialbau Projekte in Planung",
            "rent_url": "https://angebote.sozialbau.at/sobitvX/htmlprospect/home.xhtml?pq_scope=in_planung",
            "buy_url": "https://angebote.sozialbau.at/sobitvX/htmlprospect/home.xhtml?pq_scope=in_planung",
        },
        {
            "label": "WBV-GPA Projekte in Bau",
            "rent_url": "https://www.wbv-gpa.at/angebote/objekte-in-bau/",
            "buy_url": "https://www.wbv-gpa.at/angebote/objekte-in-bau/",
        },
        {
            "label": "WBV-GPA Projekte in Planung",
            "rent_url": "https://www.wbv-gpa.at/angebote/objekte-in-planung/",
            "buy_url": "https://www.wbv-gpa.at/angebote/objekte-in-planung/",
        },
        {
            "label": "ARWAG Projekte",
            "rent_url": "https://www.arwag.at/",
            "buy_url": "https://www.arwag.at/",
            "supported_region_codes": ("vienna",),
        },
        {
            "label": "Raiffeisen WohnBau Projekte",
            "rent_url": "https://www.raiffeisen-wohnbau.at/en/projects/overview/",
            "buy_url": "https://www.raiffeisen-wohnbau.at/en/projects/overview/",
            "supported_region_codes": ("vienna", "lower_austria"),
        },
        {
            "label": "Leitgöb Wohnbau",
            "rent_url": "https://www.leitgoeb-wohnbau.at/",
            "buy_url": "https://www.leitgoeb-wohnbau.at/",
            "supported_region_codes": ("salzburg", "upper_austria"),
        },
        {
            "label": "Viktoria Wohnbau",
            "rent_url": "https://www.viktoria-wohnbau.at/",
            "buy_url": "https://www.viktoria-wohnbau.at/",
        },
    ),
    "public_housing_at": (
        {
            "label": "Wohnberatung Wien",
            "rent_url": "https://www.wien.gv.at/wohnen/wohnbaufoerderung/wohnungssuche/index.html",
            "buy_url": "https://www.wien.gv.at/wohnen/wohnbaufoerderung/wohnungssuche/index.html",
        },
        {
            "label": "Wiener Wohnen",
            "rent_url": "https://www.wien.gv.at/wohnen/gemeindewohnungen/",
            "buy_url": "https://www.wien.gv.at/wohnen/gemeindewohnungen/",
        },
        {
            "label": "GESIBA Wohnungen",
            "rent_url": "https://www.gesiba.at/immobilien/wohnungen",
            "buy_url": "https://www.gesiba.at/immobilien/wohnungen",
        },
        {
            "label": "Siedlungsunion Sofort",
            "rent_url": "https://www.siedlungsunion.at/wohnen/sofort",
            "buy_url": "https://www.siedlungsunion.at/wohnen/sofort",
        },
        {
            "label": "Sozialbau Projekte in Bau",
            "rent_url": "https://angebote.sozialbau.at/sobitvX/htmlprospect/home.xhtml?pq_scope=in_bau",
            "buy_url": "https://angebote.sozialbau.at/sobitvX/htmlprospect/home.xhtml?pq_scope=in_bau",
        },
    ),
    "distressed_sales_at": (
        {
            "label": "Justiz Edikte",
            "rent_url": "https://edikte2.justiz.gv.at/edikte/ex/exedi3.nsf/Suche!OpenForm",
            "buy_url": "https://edikte2.justiz.gv.at/edikte/ex/exedi3.nsf/Suche!OpenForm",
        },
        {
            "label": "ZVGInfo Austria",
            "rent_url": "https://www.zvginfo.at/",
            "buy_url": "https://www.zvginfo.at/",
        },
    ),
    "community_signals_at": (
        {
            "label": "Flatbee Community Meta",
            "rent_url": "https://www.flatbee.at/properties/property_search",
            "buy_url": "https://www.flatbee.at/properties/property_search",
        },
    ),
    "core_portals_de": (
        {
            "label": "Immowelt",
            "rent_url": "https://www.immowelt.de/suche/mietwohnungen",
            "buy_url": "",
        },
        {
            "label": "ImmoScout24",
            "rent_url": "https://www.immobilienscout24.de/Suche/de/wohnung-mieten",
            "buy_url": "",
        },
        {
            "label": "Kleinanzeigen Immobilien",
            "rent_url": "https://www.kleinanzeigen.de/s-wohnung-mieten/c203",
            "buy_url": "https://www.kleinanzeigen.de/s-wohnung-kaufen/c196",
        },
        {
            "label": "Immonet",
            "rent_url": "https://www.immonet.de/wohnung-mieten.html",
            "buy_url": "",
        },
        {
            "label": "meinestadt.de",
            "rent_url": "https://www.meinestadt.de/deutschland/immobilien/wohnungen",
            "buy_url": "",
        },
    ),
    "shared_housing_de": (
        {
            "label": "WG-Gesucht",
            "rent_url": "https://www.wg-gesucht.de/",
            "buy_url": "https://www.wg-gesucht.de/",
        },
        {
            "label": "meinestadt.de Zimmer",
            "rent_url": "https://www.meinestadt.de/deutschland/immobilien/wohnungen",
            "buy_url": "https://www.meinestadt.de/deutschland/immobilien/wohnungen",
        },
    ),
    "corporate_landlords_de": (
        {
            "label": "Vonovia",
            "rent_url": "https://www.vonovia.de/de-de/wohnungssuche",
            "buy_url": "",
        },
        {
            "label": "LEG Wohnen",
            "rent_url": "https://www.leg-wohnen.de/mietwohnungen/",
            "buy_url": "",
        },
        {
            "label": "TAG Wohnen",
            "rent_url": "https://tag-wohnen.de/",
            "buy_url": "",
        },
    ),
    "municipal_housing_de": (
        {
            "label": "degewo Berlin",
            "rent_url": "https://www.degewo.de/wohnen/wohnungen-und-gewerbe.html",
            "buy_url": "https://www.degewo.de/wohnen/wohnungen-und-gewerbe.html",
        },
        {
            "label": "SAGA Hamburg",
            "rent_url": "https://www.saga.hamburg/immobiliensuche",
            "buy_url": "https://www.saga.hamburg/immobiliensuche",
        },
    ),
    "cooperatives_de": (
        {
            "label": "Wohnprojekte-Portal",
            "rent_url": "https://www.wohnprojekte-portal.de/",
            "buy_url": "https://www.wohnprojekte-portal.de/",
        },
        {
            "label": "Begeno16",
            "rent_url": "https://begeno16.de/",
            "buy_url": "https://begeno16.de/",
        },
    ),
    "new_build_de": (
        {
            "label": "neubau kompass",
            "rent_url": "https://www.neubaukompass.com/new-build-real-estate/deutschland/",
            "buy_url": "https://www.neubaukompass.com/new-build-real-estate/deutschland/",
        },
    ),
    "auctions_de": (
        {
            "label": "ZVG Portal offiziell",
            "rent_url": "https://www.zvg-portal.de/",
            "buy_url": "https://www.zvg-portal.de/",
        },
        {
            "label": "Portal ZVG",
            "rent_url": "https://www.portal-zvg.de/",
            "buy_url": "https://www.portal-zvg.de/",
        },
        {
            "label": "ZVnow",
            "rent_url": "https://www.zvnow.de/",
            "buy_url": "https://www.zvnow.de/",
        },
    ),
    "broker_direct_de": (
        {
            "label": "VON POLL IMMOBILIEN",
            "rent_url": "",
            "buy_url": "",
        },
        {
            "label": "ohne-makler.net",
            "rent_url": "https://www.ohne-makler.net/immobilien/wohnung-mieten/",
            "buy_url": "https://www.ohne-makler.net/immobilien/immobilie-kaufen/",
        },
    ),
    "furnished_relocation_de": (
        {
            "label": "Wunderflats",
            "rent_url": "https://wunderflats.com/de/moeblierte-wohnungen",
            "buy_url": "https://wunderflats.com/de/moeblierte-wohnungen",
        },
        {
            "label": "HousingAnywhere",
            "rent_url": "https://housinganywhere.com/s/",
            "buy_url": "https://housinganywhere.com/s/",
        },
        {
            "label": "Homelike",
            "rent_url": "https://www.thehomelike.com/",
            "buy_url": "https://www.thehomelike.com/",
        },
    ),
}


def normalize_property_platform(value: object) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw in _PROVIDER_INDEX or raw == "all":
        return raw
    normalized = re.sub(r"[^a-z0-9]+", "", raw)
    if not normalized:
        return ""
    if normalized in _PROVIDER_INDEX or normalized == "all":
        return normalized
    return PROPERTY_PLATFORM_ALIAS_MAP.get(normalized, normalized)


def property_platform_keys() -> tuple[str, ...]:
    return tuple(provider.key for provider in PROVIDERS)


def is_known_property_platform(value: object) -> bool:
    return normalize_property_platform(value) in _PROVIDER_INDEX


def resolve_country_code(value: object) -> str | None:
    code = str(value or "").strip().upper()
    if code in _COUNTRY_INDEX:
        return code
    return _COUNTRY_ALIAS_INDEX.get(_country_alias_key(value))


def is_supported_country_code(value: object) -> bool:
    return resolve_country_code(value) is not None


def is_customer_search_country_code(value: object) -> bool:
    resolved = resolve_country_code(value)
    return bool(resolved and resolved in CUSTOMER_SEARCH_COUNTRY_CODES)


def normalize_country_code(value: object, *, default: str = "AT") -> str:
    return resolve_country_code(value) or default


def normalize_language_code(value: object, *, country_code: str = "AT") -> str:
    code = str(value or "").strip().lower()
    if code in _LANGUAGE_INDEX:
        return code
    return _COUNTRY_INDEX.get(normalize_country_code(country_code), _COUNTRY_INDEX["AT"]).default_language


def normalize_listing_mode(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"sale", "purchase", "for_sale", "for-sale", "kauf", "buying"}:
        return "buy"
    return normalized if normalized in {"rent", "buy"} else "rent"


def normalize_property_type_values(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw_values = [str(item or "") for item in value]
    elif isinstance(value, str) and "," in value:
        raw_values = [item.strip() for item in value.split(",")]
    else:
        raw_values = [str(value or "")]

    normalized_values: list[str] = []
    known_values = PROPERTY_TYPE_LABELS
    alias_map = {
        "büro": "office",
        "buero": "office",
        "bueroflaeche": "office",
        "bürofläche": "office",
        "office_space": "office",
        "commercial": "office",
        "gewerbe": "office",
        "gewerbeflaeche": "office",
        "gewerbefläche": "office",
        "praxis": "office",
        "ordination": "office",
    }
    for item in raw_values:
        normalized = str(item or "").strip().lower()
        if not normalized:
            continue
        normalized = alias_map.get(normalized, normalized)
        if normalized not in known_values:
            continue
        if normalized == "any" and len(raw_values) > 1:
            normalized_values = [value for value in normalized_values if value != "any"]
            continue
        if normalized not in normalized_values:
            normalized_values.append(normalized)
    if not normalized_values:
        return ["any"]
    return normalized_values


def normalize_property_type(value: object) -> str:
    values = normalize_property_type_values(value)
    return values[0] if values else "any"


def country_options() -> list[dict[str, str]]:
    return [
        {"value": code, "label": _COUNTRY_INDEX[code].label}
        for code in CUSTOMER_SEARCH_COUNTRY_ORDER
        if code in _COUNTRY_INDEX
    ]


def language_options() -> list[dict[str, str]]:
    return [{"value": code, "label": label} for code, label in LANGUAGES]


def listing_mode_options() -> list[dict[str, str]]:
    return [{"value": key, "label": label} for key, label in LISTING_MODE_LABELS.items()]


def search_goal_options() -> list[dict[str, str]]:
    return [{"value": key, "label": label} for key, label in SEARCH_GOAL_LABELS.items()]


def search_goal_label(value: object) -> str:
    normalized = str(value or "").strip().lower() or "home"
    return SEARCH_GOAL_LABELS.get(normalized, SEARCH_GOAL_LABELS["home"])


def investment_strategy_options() -> list[dict[str, str]]:
    return [{"value": key, "label": label} for key, label in INVESTMENT_STRATEGY_LABELS.items()]


def investment_strategy_label(value: object) -> str:
    normalized = str(value or "").strip().lower() or "best_overall"
    return INVESTMENT_STRATEGY_LABELS.get(normalized, INVESTMENT_STRATEGY_LABELS["best_overall"])


def property_type_options() -> list[dict[str, str]]:
    return [{"value": key, "label": label} for key, label in PROPERTY_TYPE_LABELS.items()]


def _provider_homepage_url(provider: PropertyProviderSpec) -> str:
    search_url = next((str(url or "").strip() for url in provider.search_urls.values() if str(url or "").strip()), "")
    if search_url:
        parsed = urllib.parse.urlsplit(search_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    host = next((str(host or "").strip() for host in provider.host_markers if str(host or "").strip()), "")
    if host:
        return f"https://{host}"
    return ""


def _provider_market_readiness(provider: PropertyProviderSpec) -> str:
    if not bool(provider.search_ready):
        return "catalog_only"
    normalized = str(provider.market_readiness or "").strip().lower()
    if normalized in {"catalog_only", "experimental", "private_beta", "verified", "public"}:
        return normalized
    return "private_beta"


def provider_governance(provider_key: object) -> dict[str, object]:
    provider = _PROVIDER_INDEX.get(normalize_property_platform(provider_key))
    if provider is None:
        return {
            "market_readiness": "catalog_only",
            "access_mode": "unknown",
            "official_api_available": False,
            "browser_access_allowed": False,
            "terms_review_status": "unknown",
            "robots_review_status": "unknown",
            "listing_cache_allowed": False,
            "cache_ttl_seconds": 0,
            "photo_republication_allowed": False,
            "floorplan_republication_allowed": False,
            "public_packet_allowed": False,
            "customer_packet_allowed": False,
            "attribution_required": True,
            "maximum_concurrency": 0,
            "requests_per_hour": 0,
            "operator_owner": "property-market-codex",
            "last_rights_reviewed_at": "",
        }
    return {
        "market_readiness": _provider_market_readiness(provider),
        "access_mode": str(provider.access_mode or "").strip().lower() or "public_web",
        "official_api_available": bool(provider.official_api_available),
        "browser_access_allowed": bool(provider.browser_access_allowed),
        "terms_review_status": str(provider.terms_review_status or "").strip().lower() or "needs_review",
        "robots_review_status": str(provider.robots_review_status or "").strip().lower() or "needs_review",
        "listing_cache_allowed": bool(provider.listing_cache_allowed),
        "cache_ttl_seconds": max(0, int(provider.cache_ttl_seconds or 0)),
        "photo_republication_allowed": bool(provider.photo_republication_allowed),
        "floorplan_republication_allowed": bool(provider.floorplan_republication_allowed),
        "public_packet_allowed": bool(provider.public_packet_allowed),
        "customer_packet_allowed": bool(provider.customer_packet_allowed),
        "attribution_required": bool(provider.attribution_required),
        "maximum_concurrency": max(0, int(provider.maximum_concurrency or 0)),
        "requests_per_hour": max(0, int(provider.requests_per_hour or 0)),
        "operator_owner": str(provider.operator_owner or "").strip() or "property-market-codex",
        "last_rights_reviewed_at": str(provider.last_rights_reviewed_at or "").strip(),
    }


def provider_options(*, country_code: str | None = None) -> list[dict[str, object]]:
    normalized_country = normalize_country_code(country_code, default="AT") if country_code else ""
    rows: list[dict[str, object]] = []
    for provider in PROVIDERS:
        if normalized_country and provider.country_code != normalized_country:
            continue
        country_label = _COUNTRY_INDEX.get(provider.country_code).label if provider.country_code in _COUNTRY_INDEX else provider.country_code
        family_label = provider.family.replace("_", " ").title()
        trust_label = provider.trust_tier.title()
        governance = provider_governance(provider.key)
        rows.append(
            {
                "value": provider.key,
                "label": provider.label,
                "description": (
                    f"{country_label} | {family_label} | Trust {trust_label} | "
                    f"Floorplans {provider.floorplan_reliability} | Filters {provider.filter_pushdown_strength} | "
                    f"{provider.description}"
                ),
                "country_code": provider.country_code,
                "country_label": country_label,
                "family": provider.family,
                "family_label": family_label,
                "supported_listing_modes": list(provider.supported_listing_modes),
                "trust_tier": provider.trust_tier,
                "trust_label": trust_label,
                "coverage": provider.coverage,
                "floorplan_reliability": provider.floorplan_reliability,
                "duplicate_rate": provider.duplicate_rate,
                "tour_availability": provider.tour_availability,
                "scan_reliability": provider.scan_reliability,
                "filter_pushdown_strength": provider.filter_pushdown_strength,
                "official_source_quality": provider.official_source_quality,
                "last_verified": provider.last_verified,
                "homepage_url": _provider_homepage_url(provider),
                "search_ready": bool(provider.search_ready),
                "coming_soon": not bool(provider.search_ready),
                "availability_note": str(provider.availability_note or "").strip(),
                "market_readiness": str(governance.get("market_readiness") or ""),
                "rights_review_status": str(governance.get("terms_review_status") or ""),
                "provider_rights": governance,
            }
        )
    return rows


def property_provider_search_ready(platform_key: object) -> bool:
    provider = _PROVIDER_INDEX.get(normalize_property_platform(platform_key))
    if provider is None:
        return False
    return bool(provider.search_ready)


def selectable_property_platform_keys(
    *,
    country_code: object,
    listing_mode: object | None = None,
    include_distressed_sale_signals: object = False,
) -> tuple[str, ...]:
    normalized_country = normalize_country_code(country_code)
    normalized_mode = normalize_listing_mode(listing_mode) if listing_mode is not None else ""
    allow_distressed_fallback = (
        include_distressed_sale_signals is True
        or str(include_distressed_sale_signals or "").strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}
    )
    rows: list[str] = []
    for provider in PROVIDERS:
        if provider.country_code != normalized_country:
            continue
        if not provider.search_ready:
            continue
        if normalized_mode and normalized_mode not in provider.supported_listing_modes and not allow_distressed_fallback:
            continue
        rows.append(provider.key)
    return tuple(rows)


def filter_selectable_property_platforms(
    selected_platforms: object,
    *,
    country_code: object,
    listing_mode: object | None = None,
    include_distressed_sale_signals: object = False,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    kept, removed_details = filter_selectable_property_platform_details(
        selected_platforms,
        country_code=country_code,
        listing_mode=listing_mode,
        include_distressed_sale_signals=include_distressed_sale_signals,
    )
    return kept, tuple(str(row.get("platform") or "").strip() for row in removed_details if str(row.get("platform") or "").strip())


def filter_selectable_property_platform_details(
    selected_platforms: object,
    *,
    country_code: object,
    listing_mode: object | None = None,
    include_distressed_sale_signals: object = False,
) -> tuple[tuple[str, ...], tuple[dict[str, object], ...]]:
    normalized_country = normalize_country_code(country_code)
    normalized_mode = normalize_listing_mode(listing_mode) if listing_mode is not None else ""
    allow_distressed_fallback = (
        include_distressed_sale_signals is True
        or str(include_distressed_sale_signals or "").strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}
    )
    kept: list[str] = []
    removed_details: list[dict[str, object]] = []
    removed_platforms: set[str] = set()
    if isinstance(selected_platforms, (list, tuple, set)):
        candidates = tuple(selected_platforms)
    elif selected_platforms is None:
        candidates = ()
    else:
        candidates = (selected_platforms,)

    def _append_removed(platform_key: str, *, provider: PropertyProviderSpec | None, reason: str) -> None:
        normalized_platform_key = str(platform_key or "").strip()
        if not normalized_platform_key or normalized_platform_key in removed_platforms:
            return
        removed_platforms.add(normalized_platform_key)
        removed_details.append(
            {
                "platform": normalized_platform_key,
                "provider_label": str(getattr(provider, "label", "") or normalized_platform_key).strip(),
                "reason": reason,
                "requested_country_code": normalized_country,
                "requested_country_label": country_label(normalized_country),
                "provider_country_code": str(getattr(provider, "country_code", "") or "").strip().upper(),
                "provider_country_label": (
                    country_label(getattr(provider, "country_code", ""))
                    if str(getattr(provider, "country_code", "") or "").strip()
                    else ""
                ),
                "requested_listing_mode": normalized_mode,
                "supported_listing_modes": list(getattr(provider, "supported_listing_modes", ()) or ()),
                "search_ready": bool(getattr(provider, "search_ready", False)),
                "market_readiness": str(getattr(provider, "market_readiness", "") or "").strip(),
            }
        )

    for item in candidates:
        current = normalize_property_platform(item)
        if not current or current == "all":
            if current:
                _append_removed(current, provider=None, reason="non_specific_selection")
            continue
        provider = _PROVIDER_INDEX.get(current)
        if provider is None:
            _append_removed(current, provider=None, reason="unknown_provider")
            continue
        if provider.country_code != normalized_country:
            _append_removed(current, provider=provider, reason="wrong_country")
            continue
        if not provider.search_ready:
            _append_removed(current, provider=provider, reason="not_search_ready")
            continue
        if normalized_mode and normalized_mode not in provider.supported_listing_modes and not allow_distressed_fallback:
            _append_removed(current, provider=provider, reason="listing_mode_unsupported")
            continue
        if current in kept:
            continue
        kept.append(current)
    return tuple(kept), tuple(removed_details)


def evidence_source_options(*, country_code: str | None = None) -> list[dict[str, object]]:
    normalized_country = normalize_country_code(country_code, default="AT") if country_code else ""
    rows: list[dict[str, object]] = []
    for source in EVIDENCE_SOURCES:
        if normalized_country and source.country_code != normalized_country:
            continue
        country_label = _COUNTRY_INDEX.get(source.country_code).label if source.country_code in _COUNTRY_INDEX else source.country_code
        family_label = source.evidence_family.replace("_", " ").title()
        rows.append(
            {
                "value": source.key,
                "label": source.label,
                "description": (
                    f"{country_label} | {family_label} | Confidence {source.confidence.title()} | {source.description}"
                ),
                "country_code": source.country_code,
                "country_label": country_label,
                "evidence_family": source.evidence_family,
                "evidence_family_label": family_label,
                "source_type": source.source_type,
                "confidence": source.confidence,
                "last_verified": source.last_verified,
                "license_label": source.license_label,
                "refresh_cadence": source.refresh_cadence,
                "attribution_required": bool(source.attribution_required),
                "downstream_use": source.downstream_use,
                "geographic_granularity": source.geographic_granularity,
            }
        )
    return rows


def provider_quality_labels(provider_key: str) -> dict[str, str]:
    provider = _PROVIDER_INDEX.get(normalize_property_platform(provider_key))
    if provider is None:
        return {
            "coverage": "unknown",
            "floorplan_reliability": "unknown",
            "duplicate_rate": "unknown",
            "tour_availability": "unknown",
            "scan_reliability": "unknown",
            "filter_pushdown_strength": "unknown",
            "official_source_quality": "unknown",
            "last_verified": "",
        }
    return {
        "coverage": provider.coverage,
        "floorplan_reliability": provider.floorplan_reliability,
        "duplicate_rate": provider.duplicate_rate,
        "tour_availability": provider.tour_availability,
        "scan_reliability": provider.scan_reliability,
        "filter_pushdown_strength": provider.filter_pushdown_strength,
        "official_source_quality": provider.official_source_quality,
        "last_verified": provider.last_verified,
    }


def default_platforms_for_country(country_code: object) -> tuple[str, ...]:
    normalized_country = normalize_country_code(country_code)
    return default_platforms_for_country_listing_mode(normalized_country, "rent")


def default_platforms_for_country_listing_mode(
    country_code: object,
    listing_mode: object,
    *,
    property_type: object = "any",
) -> tuple[str, ...]:
    normalized_country = normalize_country_code(country_code)
    normalized_mode = normalize_listing_mode(listing_mode)
    normalized_type = normalize_property_type(property_type)
    if normalized_country == "AT":
        if normalized_mode == "buy":
            if normalized_type == "land":
                defaults = ("willhaben", "immmo", "immoscout_at", "broker_direct_at")
            else:
                defaults = (
                    "willhaben",
                    "immmo",
                    "immoscout_at",
                    "immobilien_net_at",
                    "ohne_makler_at",
                    "sreal_at",
                    "raiffeisen_immobilien_at",
                    "wohnnet_at",
                    "keinmakler_at",
                    "derstandard_at",
                    "broker_direct_at",
                    "developer_projects_at",
                )
            kept, _removed = filter_selectable_property_platforms(defaults, country_code=normalized_country, listing_mode=normalized_mode)
            return kept
        defaults = (
            "willhaben",
            "immmo",
            "immoscout_at",
            "immobilien_net_at",
            "ohne_makler_at",
            "sreal_at",
            "raiffeisen_immobilien_at",
            "wohnnet_at",
            "keinmakler_at",
            "derstandard_at",
            "public_housing_at",
            "genossenschaften_at",
        )
        kept, _removed = filter_selectable_property_platforms(defaults, country_code=normalized_country, listing_mode=normalized_mode)
        return kept
    if normalized_country == "DE":
        if normalized_mode == "buy":
            if normalized_type == "land":
                defaults = ("core_portals_de", "wohnungsboerse_de", "broker_direct_de", "new_build_de")
            else:
                defaults = ("core_portals_de", "wohnungsboerse_de", "new_build_de", "broker_direct_de")
            kept, _removed = filter_selectable_property_platforms(defaults, country_code=normalized_country, listing_mode=normalized_mode)
            return kept
        defaults = ("core_portals_de", "wohnungsboerse_de", "corporate_landlords_de", "municipal_housing_de", "broker_direct_de")
        kept, _removed = filter_selectable_property_platforms(defaults, country_code=normalized_country, listing_mode=normalized_mode)
        return kept
    country = _COUNTRY_INDEX.get(normalized_country)
    defaults = tuple(country.featured_platforms if country is not None else _COUNTRY_INDEX["AT"].featured_platforms)
    kept, _removed = filter_selectable_property_platforms(defaults, country_code=normalized_country, listing_mode=normalized_mode)
    return kept


def _slugify_grouped_location_query(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = (
        text.replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
    )
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")


def default_language_for_country(country_code: object) -> str:
    return _COUNTRY_INDEX.get(normalize_country_code(country_code), _COUNTRY_INDEX["AT"]).default_language


def country_label(country_code: object) -> str:
    return _COUNTRY_INDEX.get(normalize_country_code(country_code), _COUNTRY_INDEX["AT"]).label


def currency_code_for_country(country_code: object) -> str:
    return _COUNTRY_INDEX.get(normalize_country_code(country_code), _COUNTRY_INDEX["AT"]).currency_code


def currency_symbol_for_country(country_code: object) -> str:
    return _COUNTRY_INDEX.get(normalize_country_code(country_code), _COUNTRY_INDEX["AT"]).currency_symbol


def default_timezone_for_country(country_code: object) -> str:
    return _COUNTRY_INDEX.get(normalize_country_code(country_code), _COUNTRY_INDEX["AT"]).default_timezone


def supported_currency_codes() -> tuple[str, ...]:
    return tuple(sorted({str(country.currency_code or "").strip().upper() for country in COUNTRIES if str(country.currency_code or "").strip()}))


def _property_location_catalog_path() -> Path:
    configured = str(os.getenv("PROPERTYQUARRY_LOCATION_CATALOG_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[1] / "data" / "property_location_catalog.json"


def _safe_location_option_rows_with_metadata(value: object) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in list(value or []) if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        option_value = str(item.get("value") or "").strip()
        if not option_value or option_value.lower() in seen:
            continue
        seen.add(option_value.lower())
        row: dict[str, object] = {
            "value": option_value,
            "label": str(item.get("label") or option_value).strip() or option_value,
            "detail": str(item.get("detail") or "").strip(),
        }
        adjacent_values = [
            str(adjacent or "").strip()
            for adjacent in list(item.get("adjacent_values") or [])
            if str(adjacent or "").strip()
        ]
        if adjacent_values:
            row["adjacent_values"] = list(dict.fromkeys(adjacent_values))
        rows.append(row)
    return rows


def _safe_location_option_rows(value: object) -> list[dict[str, str]]:
    return [
        {
            "value": str(row.get("value") or "").strip(),
            "label": str(row.get("label") or row.get("value") or "").strip(),
            "detail": str(row.get("detail") or "").strip(),
        }
        for row in _safe_location_option_rows_with_metadata(value)
        if str(row.get("value") or "").strip()
    ]


def _loaded_property_location_catalog() -> dict[str, object]:
    path = _property_location_catalog_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _generic_country_region_key(country_code: object) -> str:
    label = country_label(country_code)
    token = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return token or normalize_country_code(country_code).lower()


def _generic_country_location_options(country_code: object) -> list[dict[str, str]]:
    country = _COUNTRY_INDEX.get(normalize_country_code(country_code), _COUNTRY_INDEX["AT"])
    rows = [{"value": country.label, "label": f"All {country.label}", "detail": "Country-wide"}]
    for value in [part.strip() for part in str(country.location_placeholder or "").split(",") if part.strip()]:
        if value.lower().startswith("all "):
            continue
        if any(str(row["value"]).strip().lower() == value.lower() for row in rows):
            continue
        rows.append({"value": value, "label": value, "detail": "Suggested target area"})
    return rows


def region_options_for_country(country_code: object) -> list[dict[str, str]]:
    normalized = normalize_country_code(country_code)
    catalog = _loaded_property_location_catalog()
    country_catalog = catalog.get(normalized)
    if isinstance(country_catalog, dict):
        rows = _safe_location_option_rows(country_catalog.get("regions"))
        if rows:
            return rows
    country = _COUNTRY_INDEX.get(normalized)
    if country is None:
        return []
    return [
        {
            "value": _generic_country_region_key(normalized),
            "label": f"All {country.label}",
            "detail": "Country-wide search",
        }
    ]


def location_options_for_country_region(country_code: object, region_code: object = "") -> list[dict[str, str]]:
    normalized = normalize_country_code(country_code)
    requested_region = str(region_code or "").strip().lower()
    catalog = _loaded_property_location_catalog()
    country_catalog = catalog.get(normalized)
    if isinstance(country_catalog, dict):
        locations = country_catalog.get("locations")
        if isinstance(locations, dict):
            if requested_region and requested_region in locations:
                rows = _safe_location_option_rows(locations.get(requested_region))
                if rows:
                    return rows
            regions = region_options_for_country(normalized)
            fallback_region = str(regions[0].get("value") or "").strip().lower() if regions else ""
            if fallback_region and fallback_region in locations:
                rows = _safe_location_option_rows(locations.get(fallback_region))
                if rows:
                    return rows
    return _generic_country_location_options(normalized)


def _location_options_for_country_region_with_metadata(country_code: object, region_code: object = "") -> list[dict[str, object]]:
    normalized = normalize_country_code(country_code)
    requested_region = str(region_code or "").strip().lower()
    catalog = _loaded_property_location_catalog()
    country_catalog = catalog.get(normalized)
    if isinstance(country_catalog, dict):
        locations = country_catalog.get("locations")
        if isinstance(locations, dict):
            if requested_region and requested_region in locations:
                rows = _safe_location_option_rows_with_metadata(locations.get(requested_region))
                if rows:
                    return rows
            regions = region_options_for_country(normalized)
            fallback_region = str(regions[0].get("value") or "").strip().lower() if regions else ""
            if fallback_region and fallback_region in locations:
                rows = _safe_location_option_rows_with_metadata(locations.get(fallback_region))
                if rows:
                    return rows
    return [
        {
            "value": row["value"],
            "label": row["label"],
            "detail": row.get("detail", ""),
        }
        for row in _generic_country_location_options(normalized)
    ]


def region_label_for_country_region(country_code: object, region_code: object = "") -> str:
    requested_region = str(region_code or "").strip().lower()
    for option in region_options_for_country(country_code):
        if str(option.get("value") or "").strip().lower() == requested_region:
            return str(option.get("label") or option.get("value") or requested_region).strip()
    return requested_region.replace("_", " ").title()


def language_label(language_code: object, *, country_code: object = "AT") -> str:
    normalized = normalize_language_code(language_code, country_code=normalize_country_code(country_code))
    return _LANGUAGE_INDEX.get(normalized, _LANGUAGE_INDEX["en"])


def listing_mode_label(listing_mode: object) -> str:
    return LISTING_MODE_LABELS.get(normalize_listing_mode(listing_mode), LISTING_MODE_LABELS["rent"])


def property_type_label(property_type: object) -> str:
    property_types = normalize_property_type_values(property_type)
    if not property_types or "any" in property_types:
        return PROPERTY_TYPE_LABELS["any"]
    labels = [PROPERTY_TYPE_LABELS.get(value, str(value).title()) for value in property_types]
    return ", ".join(labels)


def provider_host_markers() -> tuple[str, ...]:
    return tuple(dict.fromkeys(marker for provider in PROVIDERS for marker in provider.host_markers))


def provider_listing_markers_for_host(hostname: object) -> tuple[str, ...]:
    host = str(hostname or "").strip().lower()
    markers: list[str] = []
    for provider in PROVIDERS:
        if any(marker in host for marker in provider.host_markers):
            markers.extend(provider.listing_path_markers)
    return tuple(dict.fromkeys(markers))


def property_provider_for_platform(platform_key: object) -> PropertyProviderSpec | None:
    return _PROVIDER_INDEX.get(normalize_property_platform(platform_key))


def property_provider_access_level(platform_key: object) -> str:
    provider = property_provider_for_platform(platform_key)
    if provider is None:
        return "public"
    if provider.family in {"community_signals", "community_meta"}:
        return "member_only"
    return "public"


def normalize_property_search_preferences(preferences: dict[str, object] | None) -> dict[str, object]:
    payload = dict(preferences or {})
    def _csv_without_blocked(raw_value: object, blocked: set[str]) -> str:
        if isinstance(raw_value, (list, tuple, set)):
            values = [str(item or "").strip() for item in raw_value if str(item or "").strip()]
        else:
            values = [part.strip() for part in str(raw_value or "").replace(";", ",").split(",") if part.strip()]
        filtered = [value for value in values if _canonical_keyword_preference_key(value) not in blocked]
        return ", ".join(dict.fromkeys(filtered))

    keyword_preference_aliases = {
        "air conditioning": "klimaanlage",
        "air-conditioning": "klimaanlage",
        "air_conditioning": "klimaanlage",
        "aircondition": "klimaanlage",
        "airconditioner": "klimaanlage",
        "ac": "klimaanlage",
        "klima": "klimaanlage",
        "klimatisiert": "klimaanlage",
        "dachgeschoss": "dachgeschosswohnung",
        "dachgeschoß": "dachgeschosswohnung",
        "dachgeschoss-wohnung": "dachgeschosswohnung",
        "dachgeschoßwohnung": "dachgeschosswohnung",
        "top floor": "dachgeschosswohnung",
        "top-floor": "dachgeschosswohnung",
        "attic": "dachgeschosswohnung",
        "attic apartment": "dachgeschosswohnung",
        "attic_apartment": "dachgeschosswohnung",
    }

    def _canonical_keyword_preference_key(value: object) -> str:
        keyword = str(value or "").strip().lower()
        return keyword_preference_aliases.get(keyword, keyword)

    def _normalize_keyword_preferences(raw_value: object) -> dict[str, str]:
        if not isinstance(raw_value, dict):
            return {}
        normalized: dict[str, str] = {}
        for key, value in dict(raw_value or {}).items():
            keyword = _canonical_keyword_preference_key(key)
            state = str(value or "").strip().lower()
            if not keyword or not state:
                continue
            normalized[keyword] = state
        return normalized

    def _csv_keep_states(raw_value: object, keyword_preferences: dict[str, str], allowed_states: set[str]) -> str:
        if isinstance(raw_value, (list, tuple, set)):
            values = [str(item or "").strip() for item in raw_value if str(item or "").strip()]
        else:
            values = [part.strip() for part in str(raw_value or "").replace(";", ",").split(",") if part.strip()]
        filtered: list[str] = []
        for value in values:
            lowered = _canonical_keyword_preference_key(value)
            if not lowered:
                continue
            state = keyword_preferences.get(lowered)
            if state is not None and state not in allowed_states:
                continue
            filtered.append(value)
        return ", ".join(dict.fromkeys(filtered))

    raw_search_mode = str(payload.get("search_mode") or "").strip().lower()
    payload["search_mode"] = raw_search_mode if raw_search_mode in {"strict", "discovery"} else "strict"
    search_goal = str(payload.get("search_goal") or "").strip().lower() or "home"
    if search_goal not in SEARCH_GOAL_LABELS:
        search_goal = "home"
    payload["search_goal"] = search_goal
    country_code = normalize_country_code(payload.get("country_code"))
    payload["country_code"] = country_code
    payload["region_code"] = str(payload.get("region_code") or "").strip().lower()
    payload["language_code"] = normalize_language_code(payload.get("language_code"), country_code=country_code)
    payload["listing_mode"] = normalize_listing_mode(payload.get("listing_mode"))
    if search_goal == "investment":
        payload["listing_mode"] = "buy"
    payload["property_type"] = normalize_property_type_values(payload.get("property_type"))
    land_only_search = bool(payload["property_type"]) and "any" not in payload["property_type"] and all(
        str(item or "").strip().lower() == "land"
        for item in list(payload["property_type"] or [])
    )
    investment_mode = str(payload.get("investment_research_mode") or "").strip().lower() or "off"
    if investment_mode not in INVESTMENT_RESEARCH_MODE_LABELS:
        investment_mode = "off"
    payload["investment_research_mode"] = investment_mode
    investment_strategy = str(payload.get("investment_strategy") or "").strip().lower() or "best_overall"
    if investment_strategy not in INVESTMENT_STRATEGY_LABELS:
        investment_strategy = "best_overall"
    payload["investment_strategy"] = investment_strategy
    raw_full_region_scope = payload.get("full_region_scope")
    payload["full_region_scope"] = (
        raw_full_region_scope is True
        or str(raw_full_region_scope or "").strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}
    )
    raw_location_query = str(payload.get("location_query") or "").strip()
    raw_custom_location_query = str(payload.get("custom_location_query") or "").strip()
    if country_code != "AT":
        stale_at_postal_code_pattern = re.compile(r"^(?:1[0-2]\d{2}|[2-9]\d{3})$")
        location_values = [
            value
            for value in (part.strip() for part in raw_location_query.split(","))
            if value and not stale_at_postal_code_pattern.fullmatch(value)
        ]
        custom_location_values = [
            value
            for value in (part.strip() for part in raw_custom_location_query.split(","))
            if value and not stale_at_postal_code_pattern.fullmatch(value)
        ]
        raw_location_query = ", ".join(dict.fromkeys(location_values))
        raw_custom_location_query = ", ".join(dict.fromkeys(custom_location_values))
    elif str(payload.get("region_code") or "").strip().lower() in {"vienna", "wien"}:
        stale_vienna_stub_pattern = re.compile(r"^1\d{3}$")
        location_values = [
            value
            for value in (part.strip() for part in raw_location_query.split(","))
            if value and not stale_vienna_stub_pattern.fullmatch(value)
        ]
        custom_location_values = [
            value
            for value in (part.strip() for part in raw_custom_location_query.split(","))
            if value and not stale_vienna_stub_pattern.fullmatch(value)
        ]
        raw_location_query = ", ".join(dict.fromkeys(location_values))
        raw_custom_location_query = ", ".join(dict.fromkeys(custom_location_values))
    payload["location_query"] = raw_location_query
    payload["custom_location_query"] = raw_custom_location_query
    if payload["full_region_scope"] and not payload["location_query"] and payload["region_code"]:
        payload["location_query"] = region_label_for_country_region(payload["country_code"], payload["region_code"])
    if payload["full_region_scope"]:
        payload["selected_location_values"] = []
        payload["selected_districts"] = []
    payload["keywords"] = str(payload.get("keywords") or "").strip()
    payload["avoid_keywords"] = str(payload.get("avoid_keywords") or "").strip()
    payload["keyword_preferences"] = _normalize_keyword_preferences(payload.get("keyword_preferences"))
    positive_keyword_preference_states = {
        "nice_to_have",
        "important",
        "strong_wish",
        "must_have",
        "hard",
        "required",
        "strict",
    }
    heat_preference_state = str(payload["keyword_preferences"].get("klimaerwaermungsfit") or "").strip().lower()
    if heat_preference_state in positive_keyword_preference_states:
        payload["prefer_heat_resilient_home"] = True
    air_conditioning_preference_state = str(payload["keyword_preferences"].get("klimaanlage") or "").strip().lower()
    if air_conditioning_preference_state in positive_keyword_preference_states:
        payload["prefer_air_conditioning"] = True
    attic_preference_state = str(payload["keyword_preferences"].get("dachgeschosswohnung") or "").strip().lower()
    if attic_preference_state == "avoid":
        payload["avoid_attic_apartment"] = True
        payload["prefer_attic_apartment"] = False
    elif attic_preference_state in positive_keyword_preference_states:
        payload["prefer_attic_apartment"] = True
        payload["avoid_attic_apartment"] = False
    if payload["keyword_preferences"]:
        payload["keywords"] = _csv_keep_states(
            payload.get("keywords"),
            payload["keyword_preferences"],
            {"must_have", "hard", "required", "strict"},
        )
        payload["avoid_keywords"] = _csv_keep_states(
            payload.get("avoid_keywords"),
            payload["keyword_preferences"],
            {"avoid"},
        )
    raw_require_floorplan = payload.get("require_floorplan")
    payload["require_floorplan"] = (
        raw_require_floorplan is True
        or str(raw_require_floorplan or "").strip().lower() in {"1", "true", "yes", "y", "on"}
    )
    raw_use_stored_feedback = payload.get("use_stored_feedback_preferences")
    payload["use_stored_feedback_preferences"] = not (
        raw_use_stored_feedback is False
        or str(raw_use_stored_feedback or "").strip().lower() in {"0", "false", "no", "n", "off"}
    )
    normalized_alert_frequency = str(payload.get("alert_frequency") or "").strip().lower() or "daily"
    if normalized_alert_frequency not in ALERT_FREQUENCY_LABELS:
        normalized_alert_frequency = "daily"
    payload["alert_frequency"] = normalized_alert_frequency
    raw_search_agent_enabled = payload.get("search_agent_enabled")
    payload["search_agent_enabled"] = (
        raw_search_agent_enabled is True
        or str(raw_search_agent_enabled or "").strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}
    )
    try:
        payload["search_agent_duration_days"] = max(7, min(365, int(float(str(payload.get("search_agent_duration_days") or 30).strip()))))
    except Exception:
        payload["search_agent_duration_days"] = 30
    notification_period = str(payload.get("search_agent_notification_period") or "").strip().lower()
    payload["search_agent_notification_period"] = notification_period if notification_period in {"day", "week"} else "day"
    try:
        payload["search_agent_notification_limit"] = max(1, min(50, int(float(str(payload.get("search_agent_notification_limit") or 5).strip()))))
    except Exception:
        payload["search_agent_notification_limit"] = 5
    raw_alert_channels = payload.get("alert_channels")
    if isinstance(raw_alert_channels, (list, tuple, set)):
        alert_channels = [
            current
            for current in dict.fromkeys(str(item or "").strip().lower() for item in raw_alert_channels)
            if current in ALERT_CHANNEL_KEYS
        ]
    else:
        single_channel = str(raw_alert_channels or "").strip().lower()
        alert_channels = [single_channel] if single_channel in ALERT_CHANNEL_KEYS else []
    payload["alert_channels"] = alert_channels or ["telegram"]
    selected_platforms, removed_platform_details = filter_selectable_property_platform_details(
        tuple(
            dict.fromkeys(
                normalize_property_platform(item)
                for item in (payload.get("selected_platforms") or [])
                if normalize_property_platform(item) and normalize_property_platform(item) != "all"
            )
        ),
        country_code=payload.get("country_code"),
        listing_mode=payload.get("listing_mode"),
        include_distressed_sale_signals=payload.get("include_distressed_sale_signals"),
    )
    payload["selected_platforms"] = list(selected_platforms)
    if removed_platform_details:
        payload["provider_selection_filter_applied"] = True
        payload["provider_selection_filter_removed"] = [
            str(row.get("platform") or "").strip()
            for row in removed_platform_details
            if str(row.get("platform") or "").strip()
        ]
        payload["provider_selection_filter_removed_details"] = [dict(row) for row in removed_platform_details]
    try:
        from app.services.property_billing import property_commercial_snapshot, property_plan_has_unlimited_provider_results

        commercial_snapshot = property_commercial_snapshot(payload)
        plan_key = str(commercial_snapshot.get("current_plan_key") or "free").strip().lower() or "free"
        plan_result_cap = int(commercial_snapshot.get("max_results_per_source") or 0)
    except Exception:
        plan_key = "free"
        plan_result_cap = 2
    try:
        requested_max_results = int(float(str(payload.get("max_results_per_source") or "").strip()))
    except Exception:
        requested_max_results = 0
    if property_plan_has_unlimited_provider_results(plan_key, plan_result_cap):
        payload.pop("max_results_per_source", None)
    elif requested_max_results > 0:
        payload["max_results_per_source"] = max(1, min(plan_result_cap or 10, requested_max_results))
    else:
        payload.pop("max_results_per_source", None)
    catalog_distance_keys = property_distance_preference_keys()
    catalog_importance_keys = {
        property_distance_importance_key(key)
        for key in catalog_distance_keys
    }
    catalog_input_keys = set(catalog_distance_keys) | catalog_importance_keys
    for raw_key in tuple(payload):
        raw_key_text = str(raw_key or "")
        normalized_key = raw_key_text.strip()
        if (
            normalized_key.casefold().startswith("max_distance_to_")
            and (
                not isinstance(raw_key, str)
                or raw_key != normalized_key
                or normalized_key not in catalog_input_keys
            )
        ):
            payload.pop(raw_key, None)
    for importance_key in catalog_importance_keys:
        if importance_key not in payload:
            continue
        normalized_importance = normalize_property_distance_importance(
            payload.get(importance_key),
            default="",
        )
        if normalized_importance:
            payload[importance_key] = normalized_importance
        else:
            payload.pop(importance_key, None)
    distance_numeric_keys = tuple(
        key for key in catalog_distance_keys if key in payload
    )
    for numeric_key in (
        "min_price_eur",
        "max_price_eur",
        "min_gross_yield_pct",
        "eigenmittel_max_eur",
        "min_rooms",
        "min_area_m2",
        "application_window_days",
        "available_within_years",
        "max_commute_minutes_transit",
        "max_commute_minutes_drive",
        "max_commute_minutes_bike",
        "max_commute_minutes_walk",
        *distance_numeric_keys,
    ):
        try:
            numeric_value = int(float(str(payload.get(numeric_key) or "").strip()))
        except Exception:
            numeric_value = 0
        if numeric_value > 0:
            if numeric_key == "available_within_years":
                payload[numeric_key] = max(1, min(10, numeric_value))
            elif numeric_key == "min_gross_yield_pct":
                payload[numeric_key] = max(1, min(15, numeric_value))
            elif numeric_key == "eigenmittel_max_eur":
                payload[numeric_key] = max(1000, min(1_000_000, numeric_value))
            elif numeric_key == "application_window_days":
                payload[numeric_key] = max(1, min(365, numeric_value))
            elif numeric_key in {
                "max_commute_minutes_transit",
                "max_commute_minutes_drive",
                "max_commute_minutes_bike",
                "max_commute_minutes_walk",
            }:
                payload[numeric_key] = max(5, min(180, numeric_value))
            elif numeric_key.startswith("max_distance_to_") and numeric_key.endswith("_m"):
                payload[numeric_key] = max(50, min(7000, numeric_value))
            else:
                payload[numeric_key] = numeric_value
        else:
            payload.pop(numeric_key, None)
    parking_pressure_preference = str(
        payload.get("parking_pressure_preference")
        or payload.get("parking_pressure_tolerance")
        or ""
    ).strip().lower()
    if parking_pressure_preference in {"any", "low", "medium", "high"}:
        payload["parking_pressure_preference"] = parking_pressure_preference
    else:
        payload.pop("parking_pressure_preference", None)
    payload.pop("parking_pressure_tolerance", None)
    payload.pop("min_match_score", None)
    raw_flatbee_penalty = payload.get("use_flatbee_reputation_penalty")
    payload["use_flatbee_reputation_penalty"] = not (
        raw_flatbee_penalty is False
        or str(raw_flatbee_penalty or "").strip().lower() in {"0", "false", "no", "n", "off"}
    )
    for bool_key in (
        "include_broker_direct_sources",
        "include_shared_housing_sources",
        "include_corporate_landlord_sources",
        "include_municipal_housing_sources",
        "include_cooperative_sources",
        "include_furnished_relocation_sources",
        "include_community_signals",
        "include_developer_project_signals",
        "include_public_housing_signals",
        "include_distressed_sale_signals",
        "wiener_wohnticket_available",
        "subsidized_required",
        "miete_mit_kaufoption",
        "require_school_evidence",
        "ganztag_required",
        "prefer_good_air_quality",
        "prefer_heat_resilient_home",
        "prefer_air_conditioning",
        "prefer_attic_apartment",
        "avoid_attic_apartment",
        "avoid_noise_risk_area",
        "require_energy_certificate",
        "require_operating_cost_statement",
        "require_high_speed_internet",
        "enable_auction_legal_review",
        "require_manual_validation_for_community",
        "enable_building_risk_research",
        "enable_market_supply_research",
        "enable_location_risk_research",
        "enable_trust_risk_scoring",
        "enable_lifestyle_research",
        "enable_family_mode",
        "enable_commute_research",
        "apply_unknowns_penalty",
        "enable_action_readiness_research",
        "investment_require_floorplan",
        "investment_require_legal_clarity",
        "investment_require_tenant_clarity",
        "investment_avoid_major_renovation",
    ):
        raw_value = payload.get(bool_key)
        payload[bool_key] = bool(raw_value) or str(raw_value or "").strip().lower() in {"1", "true", "yes", "y", "on"}
    raw_commute_destination = str(payload.get("commute_destination") or "").strip()
    if raw_commute_destination:
        payload["commute_destination"] = raw_commute_destination[:240]
    else:
        payload.pop("commute_destination", None)
    raw_additional_reachability_targets = str(payload.get("additional_reachability_targets") or "").strip()
    if raw_additional_reachability_targets:
        payload["additional_reachability_targets"] = raw_additional_reachability_targets[:500]
    else:
        payload.pop("additional_reachability_targets", None)
    raw_university_name = str(payload.get("university_name") or "").strip()
    if raw_university_name:
        payload["university_name"] = raw_university_name[:240]
    else:
        payload.pop("university_name", None)
    school_evidence_priority = str(
        payload.get("school_evidence_priority") or payload.get("school_quality_priority") or ""
    ).strip().lower()
    if school_evidence_priority not in {"", "any", "important", "very_important"}:
        school_evidence_priority = "any"
    payload["school_evidence_priority"] = school_evidence_priority or "any"
    payload.pop("school_quality_priority", None)
    raw_school_stages = payload.get("school_stage_preferences")
    if isinstance(raw_school_stages, (list, tuple, set)):
        school_stage_preferences = [
            current
            for current in dict.fromkeys(str(item or "").strip().lower() for item in raw_school_stages)
            if current in {
                "kindergarten",
                "public_kindergarten",
                "private_kindergarten",
                "volksschule",
                "ganztags_volksschule",
                "halbtags_volksschule",
                "gymnasium",
            }
        ]
    else:
        school_stage_preferences = [
            current
            for current in dict.fromkeys(
                part.strip().lower()
                for part in str(raw_school_stages or "").replace(";", ",").split(",")
            )
            if current in {
                "kindergarten",
                "public_kindergarten",
                "private_kindergarten",
                "volksschule",
                "ganztags_volksschule",
                "halbtags_volksschule",
                "gymnasium",
            }
        ]
    if any(current in {"public_kindergarten", "private_kindergarten"} for current in school_stage_preferences) and "kindergarten" not in school_stage_preferences:
        school_stage_preferences = ["kindergarten", *school_stage_preferences]
    payload["school_stage_preferences"] = school_stage_preferences
    raw_reachability_modes = payload.get("preferred_reachability_modes")
    if isinstance(raw_reachability_modes, (list, tuple, set)):
        preferred_reachability_modes = [
            current
            for current in dict.fromkeys(str(item or "").strip().lower() for item in raw_reachability_modes)
            if current in {"public_transit", "bike", "car", "walk"}
        ]
    else:
        preferred_reachability_modes = [
            current
            for current in dict.fromkeys(
                part.strip().lower()
                for part in str(raw_reachability_modes or "").replace(";", ",").split(",")
            )
            if current in {"public_transit", "bike", "car", "walk"}
        ]
    payload["preferred_reachability_modes"] = preferred_reachability_modes
    raw_project_stages = payload.get("desired_project_stages")
    if isinstance(raw_project_stages, (list, tuple, set)):
        desired_project_stages = [
            current
            for current in dict.fromkeys(str(item or "").strip().lower() for item in raw_project_stages)
            if current in {"existing", "under_construction", "planned", "waitlist", "pre_registration"}
        ]
    else:
        desired_project_stages = [
            current
            for current in dict.fromkeys(
                part.strip().lower()
                for part in str(raw_project_stages or "").replace(";", ",").split(",")
            )
            if current in {"existing", "under_construction", "planned", "waitlist", "pre_registration"}
        ]
    payload["desired_project_stages"] = desired_project_stages
    if str(payload.get("listing_mode") or "rent").strip().lower() != "buy":
        payload["investment_research_mode"] = "off"
    if payload["search_goal"] == "investment":
        payload["listing_mode"] = "buy"
        payload["enable_family_mode"] = False
        payload["enable_commute_research"] = False
        payload["enable_lifestyle_research"] = False
        payload["school_stage_preferences"] = []
        payload["require_school_evidence"] = False
        payload["school_evidence_priority"] = "any"
        payload["preferred_reachability_modes"] = []
        payload.pop("commute_destination", None)
        payload.pop("additional_reachability_targets", None)
        payload.pop("university_name", None)
        for key in (
            "max_commute_minutes_transit",
            "max_commute_minutes_drive",
            "max_commute_minutes_bike",
            "max_commute_minutes_walk",
            "max_distance_to_playground_m",
            "max_distance_to_library_m",
            "max_distance_to_zoo_m",
            "max_distance_to_public_pool_m",
            "max_distance_to_medical_care_m",
            "max_distance_to_subway_m",
            "max_distance_to_university_m",
            "max_distance_to_starbucks_m",
            "max_distance_to_fitness_center_m",
            "max_distance_to_cinema_m",
            "max_distance_to_bouldering_m",
            "max_distance_to_dog_park_m",
            "max_distance_to_good_cafe_m",
        ):
            payload.pop(key, None)
        payload["require_floorplan"] = bool(payload.get("require_floorplan")) or bool(payload.get("investment_require_floorplan"))
    if land_only_search:
        blocked_land_keywords = {
            "lift",
            "barrier-free",
            "balcony",
            "terrace",
            "klimaanlage",
            "dachgeschosswohnung",
            "no gas",
            "district heating",
            "bright",
        }
        payload["require_floorplan"] = False
        payload["require_energy_certificate"] = False
        payload["require_operating_cost_statement"] = False
        payload["investment_require_floorplan"] = False
        payload["require_barrier_free"] = False
        payload.pop("min_rooms", None)
        payload["keywords"] = _csv_without_blocked(payload.get("keywords"), blocked_land_keywords)
        payload["avoid_keywords"] = _csv_without_blocked(payload.get("avoid_keywords"), blocked_land_keywords)
        if isinstance(payload.get("keyword_preferences"), dict):
            payload["keyword_preferences"] = {
                str(key or "").strip(): str(value or "").strip()
                for key, value in dict(payload.get("keyword_preferences") or {}).items()
                if str(key or "").strip() and _canonical_keyword_preference_key(key) not in blocked_land_keywords
            }
    return normalize_property_distance_preference_pairs(payload)


def investment_research_mode_options() -> list[dict[str, str]]:
    return [{"value": key, "label": label} for key, label in INVESTMENT_RESEARCH_MODE_LABELS.items()]


def investment_research_mode_label(value: object) -> str:
    normalized = str(value or "").strip().lower() or "off"
    return INVESTMENT_RESEARCH_MODE_LABELS.get(normalized, INVESTMENT_RESEARCH_MODE_LABELS["off"])


def _csv_tokens(raw_value: object) -> list[str]:
    if isinstance(raw_value, (list, tuple, set)):
        return [str(item or "").strip() for item in raw_value if str(item or "").strip()]
    return [part.strip() for part in str(raw_value or "").replace(";", ",").split(",") if part.strip()]


def _provider_discovery_keywords(preferences: dict[str, object] | None) -> str:
    payload = dict(preferences or {})
    managed_soft_keywords = {
        "lift",
        "barrier-free",
        "balcony",
        "terrace",
        "klimaanlage",
        "dachgeschosswohnung",
        "baugrund",
        "seezugang",
        "wasserzugang",
        "family",
        "playground nearby",
        "library nearby",
        "zoo nearby",
        "public pool nearby",
        "medical care nearby",
        "supermarket nearby",
        "pharmacy nearby",
        "underground nearby",
        "no gas",
        "district heating",
        "parking",
        "pets allowed",
        "quiet",
        "bright",
    }
    keyword_preferences = {
        str(key or "").strip().lower(): str(value or "").strip().lower()
        for key, value in dict(payload.get("keyword_preferences") or {}).items()
        if str(key or "").strip() and str(value or "").strip()
    }
    hard_states = {"must_have", "hard", "required", "strict"}
    avoid_states = {"avoid"}
    hard_keywords = [
        keyword
        for keyword, state in keyword_preferences.items()
        if state in hard_states
    ]
    raw_keywords = _csv_tokens(payload.get("keywords"))
    custom_keywords = _csv_tokens(payload.get("custom_keywords"))
    avoid_keywords = {
        token.strip().lower()
        for token in _csv_tokens(payload.get("avoid_keywords"))
        if token.strip()
    }
    ordered: list[str] = []
    seen: set[str] = set()

    def _add(value: str) -> None:
        normalized = str(value or "").strip()
        lowered = normalized.lower()
        if not normalized or lowered in seen or lowered in avoid_keywords:
            return
        seen.add(lowered)
        ordered.append(normalized)

    if keyword_preferences:
        for value in hard_keywords:
            _add(value)
        for value in custom_keywords:
            _add(value)
        for value in raw_keywords:
            lowered = str(value or "").strip().lower()
            state = keyword_preferences.get(lowered)
            if state and state not in hard_states:
                continue
            if state in avoid_states:
                continue
            _add(value)
    else:
        for value in raw_keywords:
            if str(value or "").strip().lower() in managed_soft_keywords:
                continue
            _add(value)
        for value in custom_keywords:
            _add(value)
    return ", ".join(ordered)


def _append_query(url: str, query_items: dict[str, str]) -> str:
    if not query_items:
        return url
    parsed = urllib.parse.urlparse(url)
    existing = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
    for key, value in query_items.items():
        normalized = str(value or "").strip()
        if normalized:
            existing[key] = [normalized]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(existing, doseq=True)))


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = int(float(str(value).strip()))
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _willhaben_rooms_bucket(min_rooms: int | None) -> str:
    if not min_rooms:
        return ""
    if min_rooms >= 10:
        return "10X"
    if min_rooms >= 6:
        return "6X9"
    normalized = max(1, min(int(min_rooms), 5))
    return f"{normalized}X{normalized}"


def _willhaben_search_base_url(*, base_url: str, listing_mode: str, property_type: str) -> str:
    normalized_type = normalize_property_type(property_type)
    if normalized_type == "land":
        return "https://www.willhaben.at/iad/immobilien/grundstuecke" if normalize_listing_mode(listing_mode) == "buy" else base_url
    if normalized_type == "office":
        return "https://www.willhaben.at/iad/immobilien/gewerbeimmobilien"
    if normalized_type != "house":
        return base_url
    if normalize_listing_mode(listing_mode) == "buy":
        return "https://www.willhaben.at/iad/immobilien/haus-kaufen"
    return "https://www.willhaben.at/iad/immobilien/haus-mieten"


def _provider_filter_pushdown_payload(
    *,
    provider: PropertyProviderSpec,
    country_code: str,
    listing_mode: str,
    location_query: str,
    keywords: str,
    property_type: str,
    max_price_eur: int | None,
    min_rooms: int | None,
    min_area_m2: int | None,
    require_floorplan: bool,
) -> dict[str, object]:
    requested: dict[str, object] = {
        "country_code": str(country_code or "").strip().upper(),
        "listing_mode": normalize_listing_mode(listing_mode),
    }
    for key, value in (
        ("location_query", str(location_query or "").strip()),
        ("keywords", str(keywords or "").strip()),
        ("property_type", normalize_property_type(property_type)),
        ("max_price_eur", _positive_int(max_price_eur)),
        ("min_rooms", _positive_int(min_rooms)),
        ("min_area_m2", _positive_int(min_area_m2)),
        ("require_floorplan", bool(require_floorplan)),
    ):
        if value not in (None, "", False, "any"):
            requested[key] = value

    weak_search_query_providers = {
        "immobilien_net_at",
        "ohne_makler_at",
        "sreal_at",
        "raiffeisen_immobilien_at",
        "wohnnet_at",
        "keinmakler_at",
        "re_cr_mls",
        "realtor_cr",
        "coldwellbanker_cr",
        "propertiesincostarica_cr",
        "costaricarealestateservice_cr",
        "twocostaricarealestate_cr",
    }
    provider_query_blocklist = {
        "wohnberatung_wien",
        "wiener_wohnen",
    }
    provider_side_area_keys = {
        "willhaben",
        "immmo",
        "immoscout_at",
        "derstandard_at",
        "immowelt_at",
        "findmyhome_at",
        "wohnberatung_wien",
        "remax_at",
        "kalandra",
        "flatbee",
        "immoscout_de",
        "immonet",
        "kleinanzeigen_immo",
        "homegate",
        "bienici",
        "funda",
        "pararius",
        "immoweb",
        "realestate_au",
        "domain_au",
        "otodom",
        "rightmove",
        "zoopla",
        "realtor",
        "zillow",
        "encuentra24_cr",
    }
    provider_side_price_keys = provider_side_area_keys | {
        "seloger",
        "imovirtual",
        "realtor_ca",
        "rew_ca",
    }
    provider_side_room_keys = provider_side_area_keys | {"rew_ca"}
    applied: dict[str, object] = {
        "country_code": requested["country_code"],
        "listing_mode": requested["listing_mode"],
    }
    attempted: dict[str, object] = {}
    if requested.get("location_query"):
        if provider.key in provider_query_blocklist:
            pass
        elif provider.key in weak_search_query_providers:
            attempted["location_query"] = requested["location_query"]
        else:
            applied["location_query"] = requested["location_query"]
    if requested.get("keywords"):
        if provider.key in provider_query_blocklist:
            pass
        elif provider.key in weak_search_query_providers:
            attempted["keywords"] = requested["keywords"]
        else:
            applied["keywords"] = requested["keywords"]
    if requested.get("property_type") and provider.key in {"willhaben", "funda"}:
        applied["property_type"] = requested["property_type"]
    if requested.get("max_price_eur") and provider.key in provider_side_price_keys:
        applied["max_price_eur"] = requested["max_price_eur"]
    if requested.get("min_rooms") and provider.key in provider_side_room_keys:
        applied["min_rooms"] = requested["min_rooms"]
    if requested.get("min_area_m2") and provider.key in provider_side_area_keys:
        applied["min_area_m2"] = requested["min_area_m2"]
    elif requested.get("min_area_m2") and provider.key in weak_search_query_providers:
        attempted["min_area_m2"] = requested["min_area_m2"]

    post_filter_only = sorted(key for key in requested if key not in applied)
    post_filter_reasons = {
        key: (
            "attempted_as_provider_search_query_then_verified_after_fetch"
            if key in attempted
            else "provider_has_no_reliable_dedicated_filter_or_parameter"
        )
        for key in post_filter_only
    }
    cache_applied = {
        **applied,
        **{f"attempted_{key}": value for key, value in attempted.items()},
    }
    cache_key = _provider_filter_pushdown_cache_key(
        provider_key=provider.key,
        country_code=requested["country_code"],
        listing_mode=requested["listing_mode"],
        applied=cache_applied,
    )
    return {
        "version": "property_provider_filter_pushdown_v1",
        "provider": provider.key,
        "requested": requested,
        "applied": applied,
        "attempted": attempted,
        "filter_strength": "weak_search_then_post_filter" if attempted else "provider_side",
        "post_filter_only": post_filter_only,
        "post_filter_reasons": post_filter_reasons,
        "cache_key": cache_key,
    }


def _provider_filter_pushdown_cache_key(
    *,
    provider_key: str,
    country_code: str,
    listing_mode: str,
    applied: dict[str, object],
) -> str:
    cache_seed = {
        "provider": provider_key,
        "country_code": str(country_code or "").strip().upper(),
        "listing_mode": normalize_listing_mode(listing_mode),
        "filters": applied,
    }
    cache_key = hashlib.sha256(json.dumps(cache_seed, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:24]
    return f"{provider_key}:{cache_key}"


def _slug_tokens(value: str) -> list[str]:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return [token for token in cleaned.split("-") if token]


def _location_slug(value: str) -> str:
    return "-".join(_slug_tokens(value))


def _willhaben_slug(value: object) -> str:
    normalized = str(value or "").strip().lower()
    replacements = {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
    }
    for source, target in replacements.items():
        normalized = normalized.replace(source, target)
    cleaned = re.sub(r"[^a-z0-9]+", "-", normalized)
    return re.sub(r"-+", "-", cleaned).strip("-")


def _willhaben_location_path(location_query: object) -> str:
    normalized = str(location_query or "").strip()
    if not normalized:
        return ""
    normalized_key = _normalized_location_option_key(normalized)
    if normalized_key in {"wien", "vienna"}:
        return "wien"
    postal_match = re.search(r"\b(1[0-2]\d0)\b", normalized)
    if not postal_match:
        return ""
    postal_code = str(postal_match.group(1) or "").strip()
    catalog_rows = _location_options_for_country_region_with_metadata("AT", "vienna")
    for row in catalog_rows:
        if not isinstance(row, dict):
            continue
        value = str(row.get("value") or "")
        detail = str(row.get("detail") or "")
        if postal_code not in value and postal_code not in detail:
            continue
        label_slug = _willhaben_slug(row.get("label") or "")
        if label_slug:
            return f"wien/wien-{postal_code}-{label_slug}"
    return f"wien/wien-{postal_code}"


_AT_JUSTIZ_BUNDESLAND_CODES: tuple[tuple[str, str], ...] = (
    ("burgenland", "2"),
    ("kaernten", "6"),
    ("kärnten", "6"),
    ("niederoesterreich", "1"),
    ("niederösterreich", "1"),
    ("oberoesterreich", "3"),
    ("oberösterreich", "3"),
    ("salzburg", "4"),
    ("steiermark", "5"),
    ("tirol", "7"),
    ("vorarlberg", "8"),
    ("wien", "0"),
    ("vienna", "0"),
)


def _justiz_edikte_bundesland_code(location_query: str) -> str:
    normalized = str(location_query or "").strip().lower()
    if re.search(r"\b1\d{3}\b", normalized):
        return "0"
    for marker, code in _AT_JUSTIZ_BUNDESLAND_CODES:
        if marker in normalized:
            return code
    return ""


def _build_justiz_edikte_search_url(*, base_url: str, location_query: str) -> str:
    normalized = str(location_query or "").strip()
    if not normalized:
        return base_url
    postal_match = re.search(r"\b(\d{4})\b", normalized)
    postal_code = str(postal_match.group(1) or "").strip() if postal_match else ""
    bundesland_code = _justiz_edikte_bundesland_code(normalized)
    city = "Wien" if bundesland_code == "0" else ""
    query_parts: list[str] = []
    retfields: list[str] = []
    if postal_code:
        query_parts.append(f"([VPLZ]=({postal_code}))")
        retfields.append(f"%5BVPLZ%5D={urllib.parse.quote(postal_code)}")
    if city:
        query_parts.append(f"([VOrt]=({city}))")
        retfields.append(f"%5BVOrt%5D={urllib.parse.quote(city)}")
    if bundesland_code:
        query_parts.append(f"([BL]=({bundesland_code}))")
        retfields.append(f"%5BBL%5D={urllib.parse.quote(bundesland_code)}")
    if not query_parts:
        return base_url
    search_query = "(" + " AND ".join(f"({part})" for part in query_parts) + ")"
    return (
        "https://edikte2.justiz.gv.at/edikte/ex/exedi3.nsf/suchedi"
        f"?SearchView&subf=eex&SearchOrder=4&SearchMax=0&retfields={';'.join(retfields)}"
        f"&ftquery=&query={urllib.parse.quote(search_query, safe='')}"
    )


def _location_query_variants(value: str) -> tuple[str, ...]:
    raw_parts = [str(part or "").strip() for part in str(value or "").split(",")]
    variants = tuple(part for part in raw_parts if part)
    return variants or (str(value or "").strip(),)


def _explicit_location_query_variants(preferences: dict[str, object]) -> tuple[str, ...]:
    if bool(preferences.get("full_region_scope")):
        return ()
    for key in ("selected_location_values", "selected_districts"):
        raw_values = preferences.get(key)
        if not isinstance(raw_values, (list, tuple, set)):
            continue
        variants = tuple(
            dict.fromkeys(
                str(item or "").strip()
                for item in raw_values
                if str(item or "").strip()
            )
        )
        if variants:
            return variants
    raw_preferences = preferences.get("raw_preferences")
    if isinstance(raw_preferences, dict):
        return _explicit_location_query_variants(dict(raw_preferences))
    return ()


def _adjacent_area_radius_m_from_preferences(preferences: dict[str, object]) -> int:
    direct_value = preferences.get("adjacent_area_radius_m")
    try:
        direct_meters = int(float(direct_value))
    except Exception:
        direct_meters = 0
    if direct_meters > 0:
        return max(0, direct_meters)
    raw_value = preferences.get("adjacent_area_radius_value")
    try:
        unit_value = max(0.0, float(raw_value))
    except Exception:
        unit_value = 0.0
    unit = str(preferences.get("adjacent_area_radius_unit") or "m").strip().lower()
    multiplier = 1000 if unit == "km" else 1
    return max(0, int(round(unit_value * multiplier)))


def _normalized_location_option_key(value: object) -> str:
    return re.sub(r"[^a-z0-9äöüß]+", "", str(value or "").strip().lower())


def _adjacent_location_query_variants(preferences: dict[str, object]) -> tuple[str, ...]:
    if bool(preferences.get("full_region_scope")):
        return ()
    if _adjacent_area_radius_m_from_preferences(preferences) <= 0:
        return ()
    selected_variants = _explicit_location_query_variants(preferences)
    if not selected_variants:
        return ()
    country_code = preferences.get("country_code")
    region_code = preferences.get("region_code")
    normalized_country = normalize_country_code(country_code)
    rows = _location_options_for_country_region_with_metadata(country_code, region_code)
    fallback_rows: list[dict[str, object]] = []
    if not rows:
        rows = []
        for item in _generic_country_location_options(normalized_country):
            if isinstance(item, dict):
                fallback_rows.append(
                    {
                        "value": item.get("value", ""),
                        "label": item.get("label", ""),
                        "detail": item.get("detail", ""),
                    }
                )
    by_key: dict[str, dict[str, object]] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        for field in ("value", "label", "detail"):
            key = _normalized_location_option_key(row.get(field))
            if key:
                by_key.setdefault(key, row)
        postal_match = re.search(r"\b([1-9]\d{3})\b", str(row.get("value") or ""))
        if postal_match:
            by_key.setdefault(postal_match.group(1), row)
        region_label = str(row.get("detail") or "").strip()
        if region_label:
            by_key.setdefault(_normalized_location_option_key(region_label), row)
        region_value = str(row.get("region") or "").strip()
        if region_value:
            by_key.setdefault(_normalized_location_option_key(region_value), row)
    for row in fallback_rows:
        if not isinstance(row, dict):
            continue
        rows.append(row)

    def _fallback_region_match_row(
        *, selected_key: str, selected_postal: str | None
    ) -> dict[str, object] | None:
        normalized_country_code = normalized_country
        if not normalized_country_code:
            return None
        for region_option in region_options_for_country(normalized_country_code):
            region_value = region_option.get("value")
            if not region_value:
                continue
            for candidate in _location_options_for_country_region_with_metadata(country_code, region_value):
                if not isinstance(candidate, dict):
                    continue
                candidate_value = str(candidate.get("value") or "")
                candidate_label = str(candidate.get("label") or "")
                candidate_detail = str(candidate.get("detail") or "")
                if selected_postal:
                    candidate_postal_match = re.search(r"\b([1-9]\d{3})\b", candidate_value)
                    if candidate_postal_match and candidate_postal_match.group(1) == selected_postal:
                        return candidate
                if selected_key and (
                    _normalized_location_option_key(candidate_value) == selected_key
                    or _normalized_location_option_key(candidate_label) == selected_key
                    or _normalized_location_option_key(candidate_detail) == selected_key
                ):
                    return candidate
                if selected_key in {
                    _normalized_location_option_key(candidate_value),
                    _normalized_location_option_key(candidate_label),
                }:
                    return candidate
        return None
    selected_keys = {_normalized_location_option_key(value) for value in selected_variants}
    adjacent: list[str] = []
    for selected in selected_variants:
        selected_key = _normalized_location_option_key(selected)
        row = by_key.get(selected_key)
        if row is None:
            postal_match = re.search(r"\b([1-9]\d{3})\b", str(selected or ""))
            row = by_key.get(postal_match.group(1)) if postal_match else None
            if row is None:
                selected_postal = postal_match.group(1) if postal_match else None
                row = _fallback_region_match_row(
                    selected_key=selected_key,
                    selected_postal=selected_postal,
                )
        if row is None:
            continue
        for adjacent_value in list(row.get("adjacent_values") or []):
            normalized_adjacent = _normalized_location_option_key(adjacent_value)
            if not normalized_adjacent or normalized_adjacent in selected_keys:
                continue
            adjacent.append(str(adjacent_value or "").strip())
    return tuple(dict.fromkeys(value for value in adjacent if value))


def adjacent_location_query_variants(preferences: dict[str, object] | None) -> tuple[str, ...]:
    return _adjacent_location_query_variants(dict(preferences or {}))


def _region_code_matches_supported(value: object, supported_region_codes: object) -> bool:
    normalized_region = str(value or "").strip().lower()
    supported = tuple(
        str(item or "").strip().lower()
        for item in list(supported_region_codes or ())
        if str(item or "").strip()
    )
    if not normalized_region or not supported:
        return True
    aliases = {
        "wien": "vienna",
        "vienna": "vienna",
        "oberoesterreich": "upper_austria",
        "oberösterreich": "upper_austria",
        "ooe": "upper_austria",
        "oö": "upper_austria",
        "niederoesterreich": "lower_austria",
        "niederösterreich": "lower_austria",
        "noe": "lower_austria",
        "nö": "lower_austria",
        "steiermark": "styria",
        "styria": "styria",
        "salzburg": "salzburg",
    }
    canonical_region = aliases.get(normalized_region, normalized_region)
    return canonical_region in supported


def _provider_property_type_segment(property_type: str) -> str:
    normalized = normalize_property_type(property_type)
    if normalized == "apartment":
        return "apartment"
    if normalized == "house":
        return "house"
    if normalized == "office":
        return "office"
    if normalized == "land":
        return "land"
    return ""


def _findmyhome_property_segment(*, listing_mode: str, property_type: str) -> str:
    normalized_type = normalize_property_type(property_type)
    normalized_mode = normalize_listing_mode(listing_mode)
    mode_segment = "kaufen" if normalized_mode == "buy" else "mieten"
    if normalized_type == "house":
        return f"haus-{mode_segment}"
    if normalized_type == "land":
        return f"grundstueck-{mode_segment}"
    if normalized_type == "office":
        return "sonderobjekt" if normalized_mode != "buy" else "sonderobjekt-kaufen"
    if normalized_type in {"apartment", "any"}:
        return f"wohnung-{mode_segment}"
    return f"immobilie-{mode_segment}"


def _findmyhome_location_segment(location_query: str) -> str:
    normalized = str(location_query or "").strip().lower()
    if not normalized:
        return ""
    if "wien" in normalized or "vienna" in normalized:
        return "wien"
    return _location_slug(location_query)


def _build_provider_search_url(
    *,
    provider: PropertyProviderSpec,
    base_url: str,
    listing_mode: str,
    location_query: str,
    keywords: str,
    property_type: str,
    max_price_eur: int | None,
    min_rooms: int | None,
    min_area_m2: int | None,
) -> str:
    search_terms = " ".join(part for part in (location_query, keywords) if part).strip()
    location_slug = _location_slug(location_query)
    if provider.key == "justiz_edikte_at":
        return _build_justiz_edikte_search_url(base_url=base_url, location_query=location_query)
    if provider.key == "willhaben":
        base_search_url = _willhaben_search_base_url(
            base_url=base_url,
            listing_mode=listing_mode,
            property_type=property_type,
        )
        location_path = _willhaben_location_path(location_query)
        if location_path:
            base_search_url = f"{base_search_url.rstrip('/')}/{location_path}"
        query_items = {"isNavigation": "true"}
        if search_terms and not location_path:
            query_items["q"] = search_terms
        elif keywords:
            query_items["q"] = str(keywords or "").strip()
        if max_price_eur:
            query_items["PRICE_TO"] = str(max_price_eur)
        if min_area_m2:
            query_items["ESTATE_SIZE/LIVING_AREA_FROM"] = str(min_area_m2)
        room_bucket = _willhaben_rooms_bucket(min_rooms)
        if room_bucket:
            query_items["NO_OF_ROOMS_BUCKET"] = room_bucket
        return _append_query(base_search_url, query_items)
    if provider.key == "immoscout_at":
        scout_fallback = "https://www.immmo.at/suche/kauf" if listing_mode == "buy" else "https://www.immmo.at/suche/miete"
        query_items = {"pq_upstream": "immoscout_at"}
        if search_terms:
            query_items["q"] = search_terms
        if max_price_eur:
            query_items["maxPrice"] = str(max_price_eur)
        if min_rooms:
            query_items["minRooms"] = str(min_rooms)
        if min_area_m2:
            query_items["minArea"] = str(min_area_m2)
        return _append_query(scout_fallback, query_items)
    if provider.key == "remax_at":
        query_items = {}
        if search_terms:
            query_items["q"] = search_terms
        if max_price_eur:
            query_items["maxPrice"] = str(max_price_eur)
        if min_rooms:
            query_items["minRooms"] = str(min_rooms)
        if min_area_m2:
            query_items["minArea"] = str(min_area_m2)
        return _append_query(base_url, query_items)
    if provider.key == "derstandard_at":
        query_items = {}
        if search_terms:
            query_items["q"] = search_terms
        if max_price_eur:
            query_items["maxPrice"] = str(max_price_eur)
        if min_rooms:
            query_items["minRooms"] = str(min_rooms)
        if min_area_m2:
            query_items["minArea"] = str(min_area_m2)
        return _append_query(base_url, query_items)
    if provider.key == "findmyhome_at":
        location_segment = _findmyhome_location_segment(location_query)
        property_segment = _findmyhome_property_segment(listing_mode=listing_mode, property_type=property_type)
        target_url = f"https://www.findmyhome.at/immo/{property_segment}"
        if location_segment:
            target_url = f"{target_url}/{location_segment}"
        query_items = {}
        if min_area_m2:
            query_items["minArea"] = str(min_area_m2)
        return _append_query(target_url, query_items)
    if provider.key in {"wohnberatung_wien", "wiener_wohnen"}:
        query_items = {}
        if max_price_eur:
            query_items["maxPrice"] = str(max_price_eur)
        if min_rooms:
            query_items["minRooms"] = str(min_rooms)
        if min_area_m2:
            query_items["minArea"] = str(min_area_m2)
        return _append_query(base_url, query_items)
    if provider.key in {
        "immowelt_at",
        "glorit_at",
        "findmyhome_at",
        "gesiba_at",
        "oesw_at",
        "egw_at",
        "wag_at",
        "heimat_oesterreich_at",
        "bwsg_at",
        "wiensued_at",
        "ebg_wohnen_at",
        "ooe_wohnbau_at",
        "salzburg_wohnbau_at",
        "oevw_at",
        "arwag_at",
        "raiffeisen_wohnbau_at",
        "leitgoeb_wohnbau_at",
        "viktoria_wohnbau_at",
        "zvginfo_at",
        "theagency_cr",
        "krain_cr",
        "desarrollos_cr",
        "propertiesincostarica_cr",
        "costaricarealestateservice_cr",
        "twocostaricarealestate_cr",
        "tierraverde_cr",
        "neubaukompass_de",
        "meinestadt_de",
        "wg_gesucht_de",
        "vonovia_de",
        "leg_wohnen_de",
        "tag_wohnen_de",
        "degewo_berlin",
        "saga_hamburg",
        "wohnprojekte_portal_de",
        "portal_zvg_de",
        "zvnow_de",
        "ohne_makler_de",
        "von_poll_de",
    }:
        query_items = {}
        if search_terms:
            query_items["q"] = search_terms
        if max_price_eur:
            query_items["maxPrice"] = str(max_price_eur)
        if min_rooms:
            query_items["minRooms"] = str(min_rooms)
        if min_area_m2:
            query_items["minArea"] = str(min_area_m2)
        return _append_query(base_url, query_items)
    if provider.key == "kalandra":
        query_items = {}
        if min_area_m2:
            query_items["f[all][living_area][min]"] = str(min_area_m2)
        return _append_query("https://www.kalandra.at/immobiliensuche", query_items)
    if provider.key == "flatbee":
        query_items = {}
        if max_price_eur:
            query_items["preis_nach"] = str(max_price_eur)
        if min_rooms:
            query_items["zimmer_ab"] = str(min_rooms)
        if min_area_m2:
            query_items["wohnflache_ab"] = str(min_area_m2)
        return _append_query(base_url or "https://www.flatbee.at/properties/property_search", query_items)
    if provider.key == "immoscout_de" and location_slug:
        suffix = "wohnung-kaufen" if listing_mode == "buy" else "wohnung-mieten"
        query_items = {}
        if min_area_m2:
            query_items["livingspace"] = f"{float(min_area_m2):.1f}-"
        return _append_query(
            f"https://www.immobilienscout24.de/Suche/de/{location_slug}/{location_slug}/{suffix}",
            query_items,
        )
    if provider.key == "immowelt" and location_slug:
        base_path = "kaufen/wohnung" if listing_mode == "buy" else "mietwohnungen"
        return f"https://www.immowelt.de/suche/{base_path}/{location_slug}"
    if provider.key == "homegate":
        query_items = {}
        if search_terms:
            query_items["loc"] = search_terms
        if max_price_eur:
            query_items["ag"] = str(max_price_eur)
        if min_rooms:
            query_items["ac"] = str(min_rooms)
        if min_area_m2:
            query_items["areaMin"] = str(min_area_m2)
        return _append_query(base_url, query_items)
    if provider.key == "idealista_es" and location_slug:
        if listing_mode == "buy":
            return f"https://www.idealista.com/en/venta-viviendas/{location_slug}/"
        return f"https://www.idealista.com/en/alquiler-viviendas/{location_slug}/"
    if provider.key == "fotocasa" and location_slug:
        mode_segment = "comprar" if listing_mode == "buy" else "alquiler"
        return f"https://www.fotocasa.es/es/{mode_segment}/viviendas/{location_slug}/l"
    if provider.key == "idealista_it" and location_slug:
        if listing_mode == "buy":
            return f"https://www.idealista.it/vendita-case/{location_slug}/"
        return f"https://www.idealista.it/affitto-case/{location_slug}/"
    if provider.key == "idealista_pt" and location_slug:
        if listing_mode == "buy":
            return f"https://www.idealista.pt/en/comprar-casas/{location_slug}/"
        return f"https://www.idealista.pt/en/arrendar-casas/{location_slug}/"
    if provider.key == "seloger":
        query_items = {"projects": "2" if listing_mode == "buy" else "1", "types": "1"}
        if search_terms:
            query_items["places"] = f"[{{ci:search-{search_terms}}}]"
        if max_price_eur:
            query_items["price"] = f"/{max_price_eur}"
        return _append_query(base_url, query_items)
    if provider.key == "bienici" and location_slug:
        mode_segment = "achat" if listing_mode == "buy" else "location"
        query_items = {}
        if min_rooms:
            query_items["minRooms"] = str(min_rooms)
        if max_price_eur:
            query_items["maxPrice"] = str(max_price_eur)
        if min_area_m2:
            query_items["minLivingArea"] = str(min_area_m2)
        return _append_query(f"https://www.bienici.com/recherche/{mode_segment}/{location_slug}", query_items)
    if provider.key == "funda" and location_slug:
        mode_segment = "koop" if listing_mode == "buy" else "huur"
        query_items = {}
        property_segment = _provider_property_type_segment(property_type)
        if property_segment:
            query_items["object_type"] = property_segment
        if min_rooms:
            query_items["min_kamers"] = str(min_rooms)
        if min_area_m2:
            query_items["min_woonopp"] = str(min_area_m2)
        return _append_query(f"https://www.funda.nl/zoeken/{mode_segment}/{location_slug}/", query_items)
    if provider.key == "pararius":
        query_items = {}
        if search_terms:
            query_items["q"] = search_terms
        if min_rooms:
            query_items["bedrooms"] = str(min_rooms)
        if max_price_eur:
            query_items["price_to"] = str(max_price_eur)
        if min_area_m2:
            query_items["surface_from"] = str(min_area_m2)
        return _append_query(base_url, query_items)
    if provider.key == "immoweb":
        query_items = {}
        if search_terms:
            query_items["q"] = search_terms
        if max_price_eur:
            query_items["maxPrice"] = str(max_price_eur)
        if min_rooms:
            query_items["minBedroomCount"] = str(min_rooms)
        if min_area_m2:
            query_items["minSurface"] = str(min_area_m2)
        return _append_query(base_url, query_items)
    if provider.key == "daft_ie" and location_slug:
        if listing_mode == "buy":
            return f"https://www.daft.ie/property-for-sale/{location_slug}"
        return f"https://www.daft.ie/property-for-rent/{location_slug}"
    if provider.key == "myhome_ie":
        query_items = {}
        if search_terms:
            query_items["query"] = search_terms
        return _append_query(base_url, query_items)
    if provider.key == "realestate_au":
        query_items = {}
        if search_terms:
            query_items["keywords"] = search_terms
        if max_price_eur:
            query_items["maxPrice"] = str(max_price_eur)
        if min_rooms:
            query_items["bedrooms"] = str(min_rooms)
        if min_area_m2:
            query_items["minLandSize"] = str(min_area_m2)
        return _append_query(base_url, query_items)
    if provider.key == "domain_au":
        query_items = {}
        if search_terms:
            query_items["suburb"] = search_terms
        if max_price_eur:
            query_items["price-max"] = str(max_price_eur)
        if min_rooms:
            query_items["bedrooms"] = str(min_rooms)
        if min_area_m2:
            query_items["areaMin"] = str(min_area_m2)
        return _append_query(base_url, query_items)
    if provider.key == "imovirtual":
        query_items = {}
        if search_terms:
            query_items["q"] = search_terms
        if max_price_eur:
            query_items["priceMax"] = str(max_price_eur)
        if min_area_m2:
            query_items["areaMin"] = str(min_area_m2)
        return _append_query(base_url, query_items)
    if provider.key == "otodom":
        query_items = {}
        if search_terms:
            query_items["locations"] = search_terms
        if max_price_eur:
            query_items["priceMax"] = str(max_price_eur)
        if min_rooms:
            query_items["roomsNumberMin"] = str(min_rooms)
        if min_area_m2:
            query_items["areaMin"] = str(min_area_m2)
        return _append_query(base_url, query_items)
    if provider.key == "realtor_ca":
        query_items = {}
        if search_terms:
            query_items["searchtext"] = search_terms
        if max_price_eur:
            query_items["price-max"] = str(max_price_eur)
        if min_area_m2:
            query_items["building-size-min"] = str(min_area_m2)
        return _append_query(base_url, query_items)
    if provider.key == "rew_ca":
        query_items = {}
        if search_terms:
            query_items["query"] = search_terms
        if max_price_eur:
            query_items["price_max"] = str(max_price_eur)
        if min_rooms:
            query_items["bedrooms"] = str(min_rooms)
        if min_area_m2:
            query_items["sqft_min"] = str(min_area_m2)
        return _append_query(base_url, query_items)
    if provider.key == "rightmove":
        query_items = {"searchLocation": location_query or keywords}
        if max_price_eur:
            query_items["maxPrice"] = str(max_price_eur)
        if min_rooms:
            query_items["minBedrooms"] = str(min_rooms)
        if min_area_m2:
            query_items["minSize"] = str(min_area_m2)
        return _append_query(base_url, query_items)
    if provider.key == "zoopla":
        query_items = {"q": location_query or keywords}
        if max_price_eur:
            query_items["price_max"] = str(max_price_eur)
        if min_rooms:
            query_items["beds_min"] = str(min_rooms)
        if min_area_m2:
            query_items["floor_area_min"] = str(min_area_m2)
        return _append_query(base_url, query_items)
    if provider.key == "realtor":
        query_items = {"view": "list", "query": location_query or keywords}
        if min_rooms:
            query_items["beds-min"] = str(min_rooms)
        if max_price_eur:
            query_items["price-max"] = str(max_price_eur)
        if min_area_m2:
            query_items["sqft-min"] = str(min_area_m2)
        return _append_query(base_url, query_items)
    if provider.key == "zillow":
        query_items = {"query": location_query or keywords}
        if min_rooms:
            query_items["beds"] = str(min_rooms)
        if max_price_eur:
            query_items["price"] = f"-{max_price_eur}"
        if min_area_m2:
            query_items["sqft"] = f"{min_area_m2}-"
        return _append_query(base_url, query_items)
    query_items: dict[str, str] = {}
    if search_terms:
        query_items["q"] = search_terms
    if max_price_eur:
        query_items["maxPrice"] = str(max_price_eur)
    if min_rooms:
        query_items["minRooms"] = str(min_rooms)
    if min_area_m2:
        query_items["minArea"] = str(min_area_m2)
    if property_type and property_type != "any":
        query_items["propertyType"] = property_type
    return _append_query(base_url, query_items)


def _build_grouped_provider_source_url(
    *,
    base_url: str,
    min_area_m2: int | None,
    location_query: str | None,
) -> tuple[str, set[str]]:
    normalized_url = str(base_url or "").strip()
    if not normalized_url:
        return "", set()
    query_items: dict[str, str] = {}
    pushed: set[str] = set()
    parsed = urllib.parse.urlparse(normalized_url)
    host = str(parsed.netloc or "").strip().lower()
    normalized_location = _slugify_grouped_location_query(location_query)
    if normalized_location:
        path = parsed.path.rstrip("/")
        if "ohne-makler.net" in host and path in {
            "/immobilien/immobilie-kaufen",
            "/immobilien/wohnung-mieten",
        }:
            normalized_url = urllib.parse.urlunparse(parsed._replace(path=f"/immobilien/{normalized_location}/{normalized_location}/", query=""))
            parsed = urllib.parse.urlparse(normalized_url)
            host = str(parsed.netloc or "").strip().lower()
        elif "neubaukompass.com" in host and path == "/new-build-real-estate/deutschland":
            normalized_url = urllib.parse.urlunparse(parsed._replace(path=f"/new-build-real-estate/{normalized_location}/", query=""))
            parsed = urllib.parse.urlparse(normalized_url)
            host = str(parsed.netloc or "").strip().lower()
    if min_area_m2:
        if "gesiba.at" in host:
            query_items["size-from"] = str(min_area_m2)
            pushed.add("min_area_m2")
        elif "siedlungsunion.at" in host:
            query_items["size"] = str(min_area_m2)
            pushed.add("min_area_m2")
        elif "kalandra.at" in host:
            query_items["f[all][living_area][min]"] = str(min_area_m2)
            pushed.add("min_area_m2")
    return _append_query(normalized_url, query_items), pushed


def generated_source_specs(
    *,
    preferences: dict[str, object] | None,
    selected_platforms: tuple[str, ...] | list[str] | None,
    principal_id: str = "",
    default_person_id: str = "self",
    notify_telegram: bool = True,
    max_results: int | None = None,
) -> tuple[dict[str, object], ...]:
    normalized_preferences = normalize_property_search_preferences(preferences)
    country_code = str(normalized_preferences.get("country_code") or "AT").strip().upper() or "AT"
    listing_mode = str(normalized_preferences.get("listing_mode") or "rent").strip().lower() or "rent"
    location_query = str(normalized_preferences.get("location_query") or "").strip()
    region_code = str(normalized_preferences.get("region_code") or "").strip().lower()
    keywords = _provider_discovery_keywords(normalized_preferences)
    normalized_property_types = normalize_property_type_values(normalized_preferences.get("property_type"))
    property_type = normalize_property_type(normalized_property_types)
    max_price_eur = normalized_preferences.get("max_price_eur")
    min_rooms = normalized_preferences.get("min_rooms")
    min_area_m2 = normalized_preferences.get("min_area_m2")
    require_floorplan = bool(normalized_preferences.get("require_floorplan"))
    requested_platforms = [normalize_property_platform(item) for item in (selected_platforms or ())]
    effective_platforms = [item for item in requested_platforms if item and item != "all"]
    if not effective_platforms or "all" in requested_platforms:
        effective_platforms = list(
            selectable_property_platform_keys(
                country_code=country_code,
                listing_mode=listing_mode,
                include_distressed_sale_signals=normalized_preferences.get("include_distressed_sale_signals"),
            )
        )
    explicit_location_queries = _explicit_location_query_variants(normalized_preferences)
    if explicit_location_queries:
        location_queries = tuple(
            dict.fromkeys(
                (
                    *explicit_location_queries,
                    *_adjacent_location_query_variants(normalized_preferences),
                )
            )
        )
    else:
        location_queries = _location_query_variants(location_query)
    rows: list[dict[str, object]] = []
    for provider_key in effective_platforms:
        provider = _PROVIDER_INDEX.get(provider_key)
        if provider is None or provider.country_code != country_code:
            continue
        if not provider.search_ready:
            continue
        if not _region_code_matches_supported(region_code, provider.supported_region_codes):
            continue
        if listing_mode not in provider.supported_listing_modes:
            if not bool(normalized_preferences.get("include_distressed_sale_signals")):
                continue
        provider_mode = listing_mode if listing_mode in provider.supported_listing_modes else provider.supported_listing_modes[0]
        governance = provider_governance(provider.key)
        grouped_sources = GROUPED_PROVIDER_SOURCE_MAP.get(provider.key)
        if grouped_sources:
            for location_variant in location_queries:
                pushdown = _provider_filter_pushdown_payload(
                    provider=provider,
                    country_code=country_code,
                    listing_mode=provider_mode,
                    location_query=location_variant,
                    keywords=keywords,
                    property_type=property_type,
                    max_price_eur=int(max_price_eur) if isinstance(max_price_eur, int) else None,
                    min_rooms=int(min_rooms) if isinstance(min_rooms, int) else None,
                    min_area_m2=int(min_area_m2) if isinstance(min_area_m2, int) else None,
                    require_floorplan=require_floorplan,
                )
                detail_parts = [provider.label, country_label(country_code), LISTING_MODE_LABELS.get(provider_mode, provider_mode.capitalize())]
                if location_variant:
                    detail_parts.append(location_variant)
                for source_index, grouped_source in enumerate(grouped_sources, start=1):
                    if not _region_code_matches_supported(region_code, grouped_source.get("supported_region_codes")):
                        continue
                    base_group_url = str(grouped_source.get(f"{provider_mode}_url") or "").strip()
                    if not base_group_url:
                        continue
                    source_url, pushed_filters = _build_grouped_provider_source_url(
                        base_url=base_group_url,
                        min_area_m2=int(min_area_m2) if isinstance(min_area_m2, int) else None,
                        location_query=location_variant,
                    )
                    source_pushdown = json.loads(json.dumps(pushdown))
                    if "min_area_m2" in pushed_filters and isinstance(source_pushdown.get("applied"), dict):
                        source_pushdown["applied"]["min_area_m2"] = int(min_area_m2)
                        source_pushdown["post_filter_only"] = [
                            key for key in list(source_pushdown.get("post_filter_only") or []) if str(key) != "min_area_m2"
                        ]
                        source_pushdown["cache_key"] = _provider_filter_pushdown_cache_key(
                            provider_key=provider.key,
                            country_code=country_code,
                            listing_mode=provider_mode,
                            applied=dict(source_pushdown.get("applied") or {}),
                        )
                    rows.append(
                        {
                            "url": source_url or base_group_url,
                            "label": " | ".join(detail_parts + [str(grouped_source.get("label") or f"Source {source_index}").strip()]),
                            "principal_id": str(principal_id or "").strip(),
                            "preference_person_id": str(normalized_preferences.get("preference_person_id") or default_person_id or "self").strip() or "self",
                            "account_email": "",
                            "notify_telegram": bool(notify_telegram),
                            "platform": provider.key,
                            "provider_family": provider.family,
                            "provider_trust_tier": provider.trust_tier,
                            "provider_quality": provider_quality_labels(provider.key),
                            "provider_governance": governance,
                            "provider_market_readiness": str(governance.get("market_readiness") or ""),
                            "provider_rights_review_status": str(governance.get("terms_review_status") or ""),
                            "source_access_level": property_provider_access_level(provider.key),
                            "verification_required": provider.trust_tier in {"watch", "restricted"} or provider.family in {"community_signals", "community_meta"},
                            "provider_source_key": f"{provider.key}:{source_index}",
                            "max_results": max(1, min(int(max_results or 5), 10)),
                            "country_code": country_code,
                            "language_code": str(normalized_preferences.get("language_code") or "en"),
                            "listing_mode": provider_mode,
                            "location_query": location_variant,
                            "keywords": keywords,
                            "provider_filter_pushdown": source_pushdown,
                            "provider_cache_key": f"{source_pushdown['cache_key']}:{source_index}",
                        }
                    )
            continue
        base_url = str(provider.search_urls.get(provider_mode) or next(iter(provider.search_urls.values()), "")).strip()
        if not base_url:
            continue
        for location_variant in location_queries:
            url = _build_provider_search_url(
                provider=provider,
                base_url=base_url,
                listing_mode=provider_mode,
                location_query=location_variant,
                keywords=keywords,
                property_type=property_type,
                max_price_eur=int(max_price_eur) if isinstance(max_price_eur, int) else None,
                min_rooms=int(min_rooms) if isinstance(min_rooms, int) else None,
                min_area_m2=int(min_area_m2) if isinstance(min_area_m2, int) else None,
            )
            pushdown = _provider_filter_pushdown_payload(
                provider=provider,
                country_code=country_code,
                listing_mode=provider_mode,
                location_query=location_variant,
                keywords=keywords,
                property_type=property_type,
                max_price_eur=int(max_price_eur) if isinstance(max_price_eur, int) else None,
                min_rooms=int(min_rooms) if isinstance(min_rooms, int) else None,
                min_area_m2=int(min_area_m2) if isinstance(min_area_m2, int) else None,
                require_floorplan=require_floorplan,
            )
            detail_parts = [provider.label, country_label(country_code), LISTING_MODE_LABELS.get(provider_mode, provider_mode.capitalize())]
            if location_variant:
                detail_parts.append(location_variant)
            row = {
                "url": url,
                "label": " | ".join(detail_parts),
                "principal_id": str(principal_id or "").strip(),
                "preference_person_id": str(normalized_preferences.get("preference_person_id") or default_person_id or "self").strip() or "self",
                "account_email": "",
                "notify_telegram": bool(notify_telegram),
                "platform": provider.key,
                "provider_family": provider.family,
                "provider_trust_tier": provider.trust_tier,
                "provider_quality": provider_quality_labels(provider.key),
                "provider_governance": governance,
                "provider_market_readiness": str(governance.get("market_readiness") or ""),
                "provider_rights_review_status": str(governance.get("terms_review_status") or ""),
                "source_access_level": property_provider_access_level(provider.key),
                "verification_required": provider.trust_tier in {"watch", "restricted"} or provider.family in {"community_signals", "community_meta"},
                "max_results": max(1, min(int(max_results or 5), 10)),
                "country_code": country_code,
                "language_code": str(normalized_preferences.get("language_code") or "en"),
                "listing_mode": provider_mode,
                "location_query": location_variant,
                "keywords": keywords,
                "provider_filter_pushdown": pushdown,
                "provider_cache_key": str(pushdown.get("cache_key") or ""),
            }
            if provider.key == "remax_at":
                row["fetch_timeout_seconds"] = 8
                row["fallback_listing_urls"] = [
                    "https://www.remax.at/de/ib/remax-first-wien/immobilien",
                ]
            if provider.key == "glorit_at":
                row["fetch_timeout_seconds"] = 10
                row["fallback_listing_urls"] = [
                    "https://glorit.at/wohnung-kaufen/1220-wien-arminenstrasse-4a-8?t=top201",
                ]
            rows.append(row)
    return tuple(rows)
