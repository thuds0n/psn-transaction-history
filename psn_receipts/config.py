"""Persistent per-user configuration stored in ~/.psn-receipts/config.json."""

import json
from pathlib import Path

from psn_receipts.errors import PSNReceiptsError
from psn_receipts.storage import atomic_write_json

CONFIG_FILE = Path.home() / ".psn-receipts" / "config.json"
DEFAULT_LOCALE = "en-us"

_DEFAULTS: dict = {"locale": DEFAULT_LOCALE}

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
        try:
            saved = json.loads(CONFIG_FILE.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise PSNReceiptsError(
                f"Could not read configuration from {CONFIG_FILE}: {exc}"
            ) from exc
        if not isinstance(saved, dict):
            raise PSNReceiptsError(
                f"Configuration at {CONFIG_FILE} must contain a JSON object."
            )
        return {**_DEFAULTS, **saved}
    return dict(_DEFAULTS)


def get_locale() -> str:
    return load()["locale"]


def save(data: dict) -> None:
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PSNReceiptsError(
            f"Could not create the configuration directory {CONFIG_FILE.parent}: {exc}"
        ) from exc
    existing = load()
    existing.update(data)
    atomic_write_json(CONFIG_FILE, existing, description="configuration")


def locale_parts(locale: str) -> tuple[str, str]:
    """Split 'en-au' into ('AU', 'en') for use in Chihiro API URLs.

    The Chihiro URL format is: container/{COUNTRY}/{LANG}/999/{SKU}
    Locale format is always {lang}-{country}, e.g. en-au, de-de, ja-jp.
    """
    lang, country = locale.split("-", 1)
    return country.upper(), lang.lower()  # ('AU', 'en'), ('DE', 'de'), ('JP', 'ja')


def store_url(locale: str) -> str:
    return f"https://store.playstation.com/{locale}/"
