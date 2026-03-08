#!/usr/bin/env python3
"""
Lufthansa Flight Search Agent
==============================
Searches lufthansa.com for return tickets and two separate one-ways,
compares prices (with live FX conversion), and shows whether shifting
departure by ±2 hours is meaningfully cheaper (>10 % threshold).

Usage
-----
    python main.py \\
        --origin FRA \\
        --destination LHR \\
        --outbound-date 2024-07-15 \\
        --outbound-time 10:00 \\
        --inbound-date  2024-07-22 \\
        --inbound-time  17:00 \\
        --cabin economy \\
        --currency EUR

All date/time arguments use local time (Europe/Berlin).

Optional flags
--------------
    --headless / --no-headless   Show/hide browser (default: headless)
    --debug                      Save browser screenshots for each search
    --currency USD               Display prices in target currency (default EUR)
"""

import asyncio
import logging
import sys
from datetime import datetime

import typer

from agent import LufthansaAgent
from formatter import analyse, render
from fx import get_rate

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _parse_date(s: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    raise typer.BadParameter(f"Cannot parse date '{s}'. Use YYYY-MM-DD.")


def _parse_time(s: str) -> tuple[int, int]:
    try:
        h, m = s.split(":")
        return int(h), int(m)
    except Exception:
        raise typer.BadParameter(f"Cannot parse time '{s}'. Use HH:MM.")


@app.command()
def main(
    origin: str = typer.Option(..., "--origin", "-o", help="Origin IATA code, e.g. FRA"),
    destination: str = typer.Option(..., "--destination", "-d", help="Destination IATA code, e.g. LHR"),
    outbound_date: str = typer.Option(..., "--outbound-date", help="Outbound date YYYY-MM-DD"),
    outbound_time: str = typer.Option(..., "--outbound-time", help="Preferred outbound departure HH:MM"),
    inbound_date: str = typer.Option(..., "--inbound-date", help="Inbound date YYYY-MM-DD"),
    inbound_time: str = typer.Option(..., "--inbound-time", help="Preferred inbound departure HH:MM"),
    cabin: str = typer.Option("economy", "--cabin", "-c",
                               help="Cabin: economy | premium_economy | business | first"),
    currency: str = typer.Option("EUR", "--currency", help="Display currency, e.g. EUR USD GBP"),
    headless: bool = typer.Option(True, "--headless/--no-headless",
                                   help="Run browser headlessly (default: headless)"),
    debug: bool = typer.Option(False, "--debug", help="Save screenshots for debugging"),
) -> None:
    """Lufthansa flight search agent with ±2h time-window price comparison."""
    asyncio.run(
        _run(
            origin=origin,
            destination=destination,
            outbound_date_str=outbound_date,
            outbound_time_str=outbound_time,
            inbound_date_str=inbound_date,
            inbound_time_str=inbound_time,
            cabin=cabin,
            currency=currency.upper(),
            headless=headless,
            debug=debug,
        )
    )


async def _run(
    origin: str,
    destination: str,
    outbound_date_str: str,
    outbound_time_str: str,
    inbound_date_str: str,
    inbound_time_str: str,
    cabin: str,
    currency: str,
    headless: bool,
    debug: bool,
) -> None:
    from rich.console import Console
    from rich.panel import Panel

    console = Console()

    ob_date = _parse_date(outbound_date_str)
    ob_h, ob_m = _parse_time(outbound_time_str)
    ob_datetime = ob_date.replace(hour=ob_h, minute=ob_m)

    ib_date = _parse_date(inbound_date_str)
    ib_h, ib_m = _parse_time(inbound_time_str)
    ib_datetime = ib_date.replace(hour=ib_h, minute=ib_m)

    console.print(Panel(
        f"[bold]Lufthansa Flight Search[/bold]\n"
        f"Route:    [cyan]{origin.upper()} ↔ {destination.upper()}[/cyan]\n"
        f"Outbound: [cyan]{ob_datetime.strftime('%a %d %b %Y')}[/cyan] "
        f"± 2h around [cyan]{outbound_time_str}[/cyan]\n"
        f"Inbound:  [cyan]{ib_datetime.strftime('%a %d %b %Y')}[/cyan] "
        f"± 2h around [cyan]{inbound_time_str}[/cyan]\n"
        f"Cabin:    [cyan]{cabin}[/cyan]   "
        f"Currency: [cyan]{currency}[/cyan]",
        expand=False,
    ))

    agent = LufthansaAgent(
        origin=origin,
        destination=destination,
        outbound_date=ob_date,
        outbound_time=ob_datetime,
        inbound_date=ib_date,
        inbound_time=ib_datetime,
        cabin=cabin,
        headless=headless,
        debug=debug,
    )

    console.print("\n[bold yellow]⟳[/bold yellow] Launching browser and searching…")

    try:
        raw = await agent.run()
    except Exception as exc:
        logger.exception("Search failed: %s", exc)
        console.print(f"[red]Search failed:[/red] {exc}")
        sys.exit(1)

    total_legs = sum(len(v) for v in raw.values())
    console.print(
        f"[green]✓[/green] Scraped {total_legs} flight offers "
        f"({len(raw.get('return', []))} return, "
        f"{len(raw.get('outbound', []))} outbound OW, "
        f"{len(raw.get('inbound', []))} inbound OW)"
    )

    if total_legs == 0:
        console.print(
            "\n[red]No flights found.[/red] "
            "This can happen if Lufthansa.com blocked the automated browser, "
            "returned a CAPTCHA, or the route/dates have no availability.\n"
            "Try running with [bold]--no-headless[/bold] to see the browser, "
            "or check the [bold]--debug[/bold] screenshots."
        )
        sys.exit(1)

    # FX
    if currency != "EUR":
        rate = await get_rate("EUR", currency)
        console.print(f"[dim]Live FX: 1 EUR = {rate:.4f} {currency}[/dim]")

    summary = await analyse(raw, ob_datetime, ib_datetime, target_currency=currency)
    render(summary)


if __name__ == "__main__":
    app()
