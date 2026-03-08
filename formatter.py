"""
Price comparison engine and Rich table formatter.

Given the raw scraped legs, this module:
  1. Converts all prices to a target currency (default EUR) via live FX.
  2. Filters legs within the ±2 h time windows.
  3. Compares return-ticket price vs. two one-way prices.
  4. Checks whether flying ±2 h outside the window is >10 % cheaper.
  5. Renders results as Rich tables.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agent import FlightLeg
from fx import convert, get_rate

console = Console()

WINDOW_HOURS = 2  # ±2 h
SAVINGS_THRESHOLD = 0.10  # 10 %


# ---------------------------------------------------------------------------
# Data structures for analysis
# ---------------------------------------------------------------------------


@dataclass
class WindowResult:
    """Best and all flights within the ±2 h window, plus outside options."""
    direction: str
    target_time: datetime
    in_window: list[FlightLeg]      # legs within ±2 h
    out_window: list[FlightLeg]     # legs outside ±2 h (same day)
    best_in_window: Optional[FlightLeg]
    best_out_window: Optional[FlightLeg]
    savings_pct: float              # negative = cheaper inside window


@dataclass
class Summary:
    outbound: WindowResult
    inbound: WindowResult
    return_legs: list[FlightLeg]    # all return-ticket offers (combined)
    best_return: Optional[FlightLeg]
    best_return_price_eur: float
    best_oneway_total_eur: float    # best outbound + best inbound one-ways
    target_currency: str
    fx_rate: float


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------


async def analyse(
    raw: dict[str, list[FlightLeg]],
    outbound_time: datetime,
    inbound_time: datetime,
    target_currency: str = "EUR",
) -> Summary:
    """
    Convert prices, filter windows, and build a Summary.

    raw keys: 'return', 'outbound', 'inbound'
    Each value is a list of FlightLeg objects.
    """

    # --- FX conversion for all legs ---
    async def enrich(legs: list[FlightLeg]) -> None:
        for leg in legs:
            leg.price_eur = await convert(leg.price, leg.currency, "EUR")

    await asyncio.gather(
        enrich(raw.get("return", [])),
        enrich(raw.get("outbound", [])),
        enrich(raw.get("inbound", [])),
    )

    fx_rate = await get_rate("EUR", target_currency) if target_currency != "EUR" else 1.0

    outbound = _window_result("outbound", raw.get("outbound", []), outbound_time)
    inbound = _window_result("inbound", raw.get("inbound", []), inbound_time)

    return_legs = raw.get("return", [])
    best_return = min(return_legs, key=lambda l: l.price_eur, default=None)
    best_return_eur = best_return.price_eur if best_return else float("inf")

    best_ob = outbound.best_in_window
    best_ib = inbound.best_in_window
    best_oneway_eur = (
        (best_ob.price_eur if best_ob else 0.0) +
        (best_ib.price_eur if best_ib else 0.0)
    ) if best_ob and best_ib else float("inf")

    return Summary(
        outbound=outbound,
        inbound=inbound,
        return_legs=return_legs,
        best_return=best_return,
        best_return_price_eur=best_return_eur,
        best_oneway_total_eur=best_oneway_eur,
        target_currency=target_currency,
        fx_rate=fx_rate,
    )


def _window_result(
    direction: str,
    legs: list[FlightLeg],
    target: datetime,
) -> WindowResult:
    window_start = target - timedelta(hours=WINDOW_HOURS)
    window_end = target + timedelta(hours=WINDOW_HOURS)

    in_window = sorted(
        [l for l in legs if window_start <= l.departure <= window_end],
        key=lambda l: l.price_eur,
    )
    out_window = sorted(
        [l for l in legs if l.departure < window_start or l.departure > window_end],
        key=lambda l: l.price_eur,
    )

    best_in = in_window[0] if in_window else None
    best_out = out_window[0] if out_window else None

    if best_in and best_out and best_in.price_eur > 0:
        savings_pct = (best_out.price_eur - best_in.price_eur) / best_in.price_eur
    else:
        savings_pct = 0.0

    return WindowResult(
        direction=direction,
        target_time=target,
        in_window=in_window,
        out_window=out_window,
        best_in_window=best_in,
        best_out_window=best_out,
        savings_pct=savings_pct,
    )


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _eur(amount: float, fx: float, currency: str) -> str:
    if amount == float("inf") or amount == 0:
        return "N/A"
    converted = amount * fx
    sym = {"EUR": "€", "USD": "$", "GBP": "£", "CHF": "CHF "}.get(currency, currency + " ")
    return f"{sym}{converted:,.2f}"


def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _fmt_date(dt: datetime) -> str:
    return dt.strftime("%d %b")


def _pct(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v*100:.1f}%"


def _leg_row(leg: FlightLeg, fx: float, currency: str, highlight: str = "") -> tuple:
    return (
        leg.flight_no,
        f"{_fmt_date(leg.departure)} {_fmt_time(leg.departure)}",
        _fmt_time(leg.arrival),
        f"{leg.duration_min // 60}h {leg.duration_min % 60:02d}m",
        str(leg.stops),
        leg.cabin,
        _eur(leg.price_eur, fx, currency),
        highlight,
    )


def render(summary: Summary) -> None:
    """Render all tables to the console."""
    fx = summary.fx_rate
    cur = summary.target_currency

    # -----------------------------------------------------------------------
    # Table 1 – Return vs Two One-Ways comparison
    # -----------------------------------------------------------------------
    t1 = Table(
        title="[bold cyan]Return Ticket vs Two One-Ways[/bold cyan]",
        box=box.ROUNDED,
        show_lines=True,
        expand=True,
    )
    t1.add_column("Booking type", style="bold", min_width=20)
    t1.add_column("Outbound", min_width=20)
    t1.add_column("Inbound", min_width=20)
    t1.add_column(f"Total ({cur})", justify="right", min_width=14)
    t1.add_column("Note", min_width=20)

    best_ob = summary.outbound.best_in_window
    best_ib = summary.inbound.best_in_window

    # Return row
    if summary.best_return:
        r = summary.best_return
        t1.add_row(
            "Return ticket",
            f"{r.origin}→{r.destination}",
            f"{r.destination}→{r.origin}",
            _eur(summary.best_return_price_eur, fx, cur),
            "",
        )
    else:
        t1.add_row("Return ticket", "N/A", "N/A", "N/A", "Not found")

    # Two one-ways row
    if best_ob and best_ib:
        total_1w = best_ob.price_eur + best_ib.price_eur
        if summary.best_return_price_eur < float("inf") and total_1w < float("inf"):
            diff = (total_1w - summary.best_return_price_eur) / summary.best_return_price_eur
            if diff < -SAVINGS_THRESHOLD:
                note = f"[green]2×OW cheaper by {_pct(diff)}[/green]"
            elif diff > SAVINGS_THRESHOLD:
                note = f"[red]Return cheaper by {_pct(-diff)}[/red]"
            else:
                note = "[yellow]Similar price[/yellow]"
        else:
            note = ""
        ob_txt = f"{best_ob.flight_no}  {_fmt_time(best_ob.departure)}"
        ib_txt = f"{best_ib.flight_no}  {_fmt_time(best_ib.departure)}"
        t1.add_row(
            "2× One-way",
            ob_txt,
            ib_txt,
            _eur(total_1w, fx, cur),
            note,
        )
    else:
        t1.add_row("2× One-way", "N/A", "N/A", "N/A", "Insufficient data")

    console.print()
    console.print(t1)

    # -----------------------------------------------------------------------
    # Table 2 – All outbound options within ±2 h window
    # -----------------------------------------------------------------------
    _render_window_table(summary.outbound, fx, cur, "Outbound")

    # -----------------------------------------------------------------------
    # Table 3 – All inbound options within ±2 h window
    # -----------------------------------------------------------------------
    _render_window_table(summary.inbound, fx, cur, "Inbound")

    # -----------------------------------------------------------------------
    # Table 4 – Shift-window savings analysis
    # -----------------------------------------------------------------------
    _render_savings(summary, fx, cur)

    console.print()


def _render_window_table(wr: WindowResult, fx: float, cur: str, label: str) -> None:
    target_str = wr.target_time.strftime("%d %b %H:%M")
    title = (
        f"[bold cyan]{label} Flights[/bold cyan]  "
        f"[dim]target {target_str} ±{WINDOW_HOURS}h[/dim]"
    )
    t = Table(title=title, box=box.ROUNDED, show_lines=True, expand=True)
    t.add_column("Flight", min_width=8)
    t.add_column("Departure", min_width=14)
    t.add_column("Arrival", min_width=8)
    t.add_column("Duration", min_width=9)
    t.add_column("Stops", min_width=6)
    t.add_column("Cabin", min_width=16)
    t.add_column(f"Price ({cur})", justify="right", min_width=12)
    t.add_column("Window", min_width=10)

    all_legs = sorted(
        [(l, "✓ in window") for l in wr.in_window] +
        [(l, "outside") for l in wr.out_window],
        key=lambda x: x[0].departure,
    )

    best_price = wr.best_in_window.price_eur if wr.best_in_window else None

    for leg, window_tag in all_legs:
        is_best = best_price is not None and leg.price_eur == best_price and window_tag == "✓ in window"
        style = "bold green" if is_best else ""
        tag_text = "[green]✓ in window[/green]" if "in window" in window_tag else "[dim]outside[/dim]"
        row = _leg_row(leg, fx, cur)
        t.add_row(*row[:-1], tag_text, style=style)

    if not all_legs:
        t.add_row(*["—"] * 8)

    console.print()
    console.print(t)


def _render_savings(summary: Summary, fx: float, cur: str) -> None:
    """Show whether flying ±2 h outside the target window is meaningfully cheaper."""
    t = Table(
        title="[bold cyan]Time-Shift Savings Analysis (±2h)[/bold cyan]",
        box=box.ROUNDED,
        show_lines=True,
        expand=True,
    )
    t.add_column("Direction", min_width=12)
    t.add_column("Target window", min_width=20)
    t.add_column(f"Best in-window ({cur})", justify="right", min_width=18)
    t.add_column("Best departure", min_width=14)
    t.add_column(f"Best outside ({cur})", justify="right", min_width=18)
    t.add_column("Departure", min_width=14)
    t.add_column("Potential saving", justify="right", min_width=16)
    t.add_column("Worth it? (>10%)", min_width=14)

    for wr, label in [(summary.outbound, "Outbound"), (summary.inbound, "Inbound")]:
        tgt = wr.target_time
        window_str = (
            f"{(tgt - timedelta(hours=WINDOW_HOURS)).strftime('%H:%M')}–"
            f"{(tgt + timedelta(hours=WINDOW_HOURS)).strftime('%H:%M')}"
        )
        bi = wr.best_in_window
        bo = wr.best_out_window

        if bi and bo:
            saving = (bi.price_eur - bo.price_eur) / bi.price_eur  # positive = cheaper outside
            worth_it = saving > SAVINGS_THRESHOLD
            worth_str = (
                f"[green]YES ({_pct(saving)} cheaper)[/green]"
                if worth_it
                else f"[red]NO ({_pct(saving)})[/red]"
                if saving > 0
                else f"[dim]NO (in-window cheaper)[/dim]"
            )
            t.add_row(
                label,
                window_str,
                _eur(bi.price_eur, fx, cur),
                _fmt_time(bi.departure),
                _eur(bo.price_eur, fx, cur),
                _fmt_time(bo.departure),
                _pct(saving) if saving != 0 else "—",
                worth_str,
            )
        elif bi:
            t.add_row(
                label, window_str,
                _eur(bi.price_eur, fx, cur), _fmt_time(bi.departure),
                "N/A", "—", "—", "[dim]No outside data[/dim]",
            )
        else:
            t.add_row(label, window_str, *["N/A"] * 6)

    console.print()
    console.print(t)
