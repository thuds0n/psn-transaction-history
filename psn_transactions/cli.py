import typer
from typing import Optional

from psn_transactions import config as cfg
from psn_transactions.errors import PSNTransactionsError

app = typer.Typer(help="Fetch and export your PlayStation Network transaction history.")


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
            f"Saved to config and reused by fetch/enrich. "
            f"Supported: {', '.join(cfg.SUPPORTED_LOCALES)}"
        ),
    ),
    manual_confirmation: bool = typer.Option(
        False,
        "--manual-confirmation",
        help="Wait for ENTER instead of detecting sign-in automatically.",
    ),
) -> None:
    """Open a browser and save your PSN session for future commands."""
    from psn_transactions.auth import login as _login
    try:
        _login(
            force=force,
            debug=debug,
            locale=locale,
            manual_confirmation=manual_confirmation,
        )
    except PSNTransactionsError as exc:
        typer.secho(str(exc), err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1)


@app.command()
def fetch(
    output: str = typer.Option(
        "psn_transactions_raw.json",
        "--output",
        help="Path to save the raw transaction snapshot.",
    ),
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
    transport: str = typer.Option(
        "http",
        "--transport",
        help="Fetch transport: http (default) or browser fallback.",
    ),
) -> None:
    """Fetch transaction history from PSN and save a raw JSON snapshot."""
    from psn_transactions.fetch import fetch_all
    try:
        fetch_all(
            output_path=output,
            limit=limit,
            start_date=start_date,
            end_date=end_date,
            timezone_name=timezone_name,
            transport=transport,
        )
    except (FileNotFoundError, PSNTransactionsError) as exc:
        typer.secho(str(exc), err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1)


def _export_csv(input_path: str, output_path: str) -> None:
    from psn_transactions.export import export_csv

    try:
        export_csv(json_path=input_path, csv_path=output_path)
    except PSNTransactionsError as exc:
        typer.secho(str(exc), err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1)


def _enrich_csv(
    input_path: str,
    output_path: str,
    *,
    paid_only: bool,
    refresh: bool,
    cache_only: bool,
    summary: bool,
) -> None:
    from psn_transactions.export import enrich_csv

    try:
        enrich_csv(
            json_path=input_path,
            csv_path=output_path,
            paid_only=paid_only,
            refresh=refresh,
            cache_only=cache_only,
            summary=summary,
        )
    except PSNTransactionsError as exc:
        typer.secho(str(exc), err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1)


@app.command()
def export(
    input: str = typer.Option(
        "psn_transactions_raw.json",
        "--input",
        help="Path to raw JSON from fetch.",
    ),
    output: str = typer.Option(
        "psn_transactions.csv",
        "--output",
        "--csv",
        help="Path for the output CSV.",
    ),
) -> None:
    """Export raw transaction JSON to CSV without Store lookups."""
    _export_csv(input, output)


@app.command()
def enrich(
    input: str = typer.Option(
        "psn_transactions_raw.json",
        "--input",
        help="Path to raw JSON from fetch.",
    ),
    output: str = typer.Option(
        "psn_transactions_enriched.csv",
        "--output",
        "--csv",
        help="Path for the enriched output CSV.",
    ),
    paid_only: bool = typer.Option(
        False,
        "--paid-only",
        help="Include only product items with a positive item-level total.",
    ),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="Repeat Store lookups instead of reusing cached results.",
    ),
    cache_only: bool = typer.Option(
        False,
        "--cache-only",
        help="Use cached Store metadata without making network requests.",
    ),
    summary: bool = typer.Option(
        False,
        "--summary",
        help="Print detailed privacy-safe processing and classification counts.",
    ),
) -> None:
    """Export to CSV with current PS Store metadata and classification."""
    if refresh and cache_only:
        typer.secho(
            "--refresh and --cache-only cannot be used together.",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    _enrich_csv(
        input,
        output,
        paid_only=paid_only,
        refresh=refresh,
        cache_only=cache_only,
        summary=summary,
    )


if __name__ == "__main__":
    app()
