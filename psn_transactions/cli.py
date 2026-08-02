import typer
from typing import Optional

from psn_transactions import config as cfg
from psn_transactions.errors import PSNTransactionsError

app = typer.Typer(help="Export your PlayStation Network transaction history.")


@app.command()
def login(
    force: bool = typer.Option(False, "--force", help="Re-authenticate even if session exists."),
    debug: bool = typer.Option(
        False,
        "--debug",
        help="Report key session cookies after login (values are always redacted).",
    ),
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
    from psn_transactions.auth import login as _login
    try:
        _login(force=force, debug=debug, locale=locale)
    except PSNTransactionsError as exc:
        typer.secho(str(exc), err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1)


@app.command()
def fetch(
    output: str = typer.Option("psn_transactions.json", "--output", help="Path to save raw JSON."),
    limit: Optional[int] = typer.Option(
        None, "--limit",
        help="Max pages to fetch (1 page ≈ 100 transactions). Useful for testing.",
    ),
    start_date: Optional[str] = typer.Option(
        None,
        "--start",
        help="Earliest transaction date to fetch (YYYY-MM-DD, inclusive).",
    ),
    end_date: Optional[str] = typer.Option(
        None,
        "--end",
        help="Latest transaction date to fetch (YYYY-MM-DD, inclusive).",
    ),
    timezone_name: Optional[str] = typer.Option(
        None,
        "--timezone",
        help="IANA timezone for date bounds, e.g. Australia/Sydney (default: local timezone).",
    ),
) -> None:
    """Fetch transaction history from PSN and save to JSON."""
    from psn_transactions.fetch import fetch_all
    try:
        fetch_all(
            output_path=output,
            limit=limit,
            start_date=start_date,
            end_date=end_date,
            timezone_name=timezone_name,
        )
    except (FileNotFoundError, PSNTransactionsError) as exc:
        typer.secho(str(exc), err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1)


@app.command()
def export(
    input: str = typer.Option("psn_transactions.json", "--input", help="Path to raw JSON from fetch."),
    csv: str = typer.Option("psn_transactions.csv", "--csv", help="Path for output CSV."),
    enrich: bool = typer.Option(False, "--enrich", help="Look up SKUs on PS Store to classify content type."),
) -> None:
    """Parse transaction JSON and export to CSV."""
    from psn_transactions.parse import export as _export
    _export(json_path=input, csv_path=csv, enrich=enrich)


if __name__ == "__main__":
    app()
