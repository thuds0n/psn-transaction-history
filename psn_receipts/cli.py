import typer
from typing import Optional

from psn_receipts import config as cfg

app = typer.Typer(help="Export your PlayStation Network transaction history.")


@app.command()
def login(
    force: bool = typer.Option(False, "--force", help="Re-authenticate even if session exists."),
    debug: bool = typer.Option(False, "--debug", help="Print key session cookies after login."),
    locale: str = typer.Option(
        None,
        "--locale",
        help=(
            "PlayStation Store region, e.g. en-au, en-us, en-gb. "
            f"Saved to config and reused by fetch/export. "
            f"Supported: {', '.join(cfg.SUPPORTED_LOCALES)}"
        ),
    ),
) -> None:
    """Open a browser and save your PSN session for future commands."""
    from psn_receipts.auth import login as _login
    _login(force=force, debug=debug, locale=locale)


@app.command()
def fetch(
    output: str = typer.Option("psn_history_full.json", "--output", help="Path to save raw JSON."),
    limit: Optional[int] = typer.Option(
        None, "--limit",
        help="Max pages to fetch (1 page ≈ 100 transactions). Useful for testing.",
    ),
) -> None:
    """Fetch transaction history from PSN and save to JSON."""
    from psn_receipts.fetch import fetch_all
    fetch_all(output_path=output, limit=limit)


@app.command()
def export(
    input: str = typer.Option("psn_history_full.json", "--input", help="Path to raw JSON from fetch."),
    csv: str = typer.Option("psn_history_enriched.csv", "--csv", help="Path for output CSV."),
    enrich: bool = typer.Option(False, "--enrich", help="Look up SKUs on PS Store to classify content type."),
) -> None:
    """Parse transaction JSON and export to CSV."""
    from psn_receipts.parse import export as _export
    _export(json_path=input, csv_path=csv, enrich=enrich)


if __name__ == "__main__":
    app()
