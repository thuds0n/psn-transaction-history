"""Persistent per-user configuration stored in ~/.psn-receipts/config.json."""

import json
from pathlib import Path

CONFIG_FILE = Path.home() / ".psn-receipts" / "config.json"

_DEFAULTS: dict = {"locale": "en-us"}

# Full locale codes used in PS Store URLs (store.playstation.com/{locale}/)
# Format: {language}-{country} — both parts matter for non-English stores
SUPPORTED_LOCALES = [
    # English-speaking markets
    "en-us",  # United States
    "en-gb",  # United Kingdom
    "en-au",  # Australia
    "en-ca",  # Canada
    # Europe (native language)
    "de-de",  # Germany
    "fr-fr",  # France
    "es-es",  # Spain
    "it-it",  # Italy
    "nl-nl",  # Netherlands
    "pt-pt",  # Portugal
    # Asia Pacific
    "ja-jp",  # Japan
    "ko-kr",  # South Korea
    # Latin America
    "pt-br",  # Brazil
    "es-mx",  # Mexico
]


def load() -> dict:
    if CONFIG_FILE.exists():
        return {**_DEFAULTS, **json.loads(CONFIG_FILE.read_text())}
    return dict(_DEFAULTS)


def save(data: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = load()
    existing.update(data)
    CONFIG_FILE.write_text(json.dumps(existing, indent=2))


def locale_parts(locale: str) -> tuple[str, str]:
    """Split 'en-au' into ('AU', 'en') for use in Chihiro API URLs.

    The Chihiro URL format is: container/{COUNTRY}/{LANG}/999/{SKU}
    Locale format is always {lang}-{country}, e.g. en-au, de-de, ja-jp.
    """
    lang, country = locale.split("-", 1)
    return country.upper(), lang.lower()  # ('AU', 'en'), ('DE', 'de'), ('JP', 'ja')


def store_url(locale: str) -> str:
    return f"https://store.playstation.com/{locale}/"
