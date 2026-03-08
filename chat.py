#!/usr/bin/env python3
"""
Conversational Claude-powered Lufthansa flight assistant.
─────────────────────────────────────────────────────────
Talk in plain English. Claude understands your request, calls the Lufthansa
Playwright agent, and explains the results.

Setup (one-time):
    bash setup.sh                          # installs all dependencies
    export ANTHROPIC_API_KEY="sk-ant-..."  # get yours at console.anthropic.com

Run:
    .venv/bin/python chat.py               # headless browser (default)
    .venv/bin/python chat.py --show        # visible browser (helps with CAPTCHA)

Example prompts:
    "Find flights from Frankfurt to London next Saturday around 10am,
     returning the following Sunday evening, economy class, EUR please."
    "Same but in business class and show prices in GBP."
    "What about Munich to JFK in August?"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from typing import Optional

import anthropic
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from agent import LufthansaAgent
from formatter import analyse, render
from fx import get_rate

console = Console()

# ---------------------------------------------------------------------------
# Claude setup
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a friendly Lufthansa flight search assistant.

When the user asks about flights, extract these parameters and call search_flights:
  • origin / destination  – convert city names to IATA codes
      Frankfurt → FRA, Munich → MUC, Berlin → BER, Vienna → VIE,
      London Heathrow → LHR, London Gatwick → LGW, Paris CDG → CDG,
      New York JFK → JFK, New York Newark → EWR, Los Angeles → LAX
  • outbound_date / outbound_time – date in YYYY-MM-DD, time in HH:MM
      If the user says "around 10am" → "10:00"; if no time given → "12:00"
  • inbound_date / inbound_time – same format
  • cabin – economy | premium_economy | business | first (default: economy)
  • currency – ISO code, e.g. EUR USD GBP CHF (default: EUR)

Ask for clarification only when the route or dates are genuinely ambiguous.

After the search results appear (the tables are printed to the terminal), give a
brief, friendly summary covering:
  1. Return ticket vs two one-ways – which is cheaper and by how much
  2. Best flight in the user's preferred time window
  3. Whether flying ±2 hours would save meaningful money (>10%)
Keep your summary short – the full tables are already visible above it.
"""

TOOLS = [
    {
        "name": "search_flights",
        "description": (
            "Search Lufthansa.com for flights. Runs three searches: "
            "return ticket, outbound one-way, and inbound one-way. "
            "Compares prices with live FX conversion and shows whether "
            "flying ±2 hours from the preferred time is >10% cheaper. "
            "Results are printed as Rich tables directly in the terminal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "3-letter IATA code of the departure airport, e.g. FRA",
                },
                "destination": {
                    "type": "string",
                    "description": "3-letter IATA code of the arrival airport, e.g. LHR",
                },
                "outbound_date": {
                    "type": "string",
                    "description": "Outbound flight date in YYYY-MM-DD format",
                },
                "outbound_time": {
                    "type": "string",
                    "description": "Preferred outbound departure time in HH:MM format",
                },
                "inbound_date": {
                    "type": "string",
                    "description": "Return flight date in YYYY-MM-DD format",
                },
                "inbound_time": {
                    "type": "string",
                    "description": "Preferred return departure time in HH:MM format",
                },
                "cabin": {
                    "type": "string",
                    "enum": ["economy", "premium_economy", "business", "first"],
                    "description": "Cabin class (default: economy)",
                },
                "currency": {
                    "type": "string",
                    "description": "Display currency ISO code (default: EUR)",
                },
            },
            "required": [
                "origin", "destination",
                "outbound_date", "outbound_time",
                "inbound_date", "inbound_time",
            ],
        },
    }
]

# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------


async def _execute_search(tool_input: dict, show_browser: bool) -> str:
    """
    Run the Playwright agent, render Rich tables in the terminal,
    then return a concise text summary for Claude.
    """
    origin = tool_input["origin"].upper()
    destination = tool_input["destination"].upper()
    cabin = tool_input.get("cabin", "economy")
    currency = tool_input.get("currency", "EUR").upper()

    try:
        outbound_date = datetime.strptime(tool_input["outbound_date"], "%Y-%m-%d")
        inbound_date = datetime.strptime(tool_input["inbound_date"], "%Y-%m-%d")
    except ValueError as exc:
        return f"Invalid date format: {exc}. Dates must be YYYY-MM-DD."

    def _parse_hhmm(s: str, default: str = "12:00") -> tuple[int, int]:
        s = s or default
        h, m = s.split(":")
        return int(h), int(m)

    oh, om = _parse_hhmm(tool_input.get("outbound_time", "12:00"))
    ih, im = _parse_hhmm(tool_input.get("inbound_time", "12:00"))
    outbound_dt = outbound_date.replace(hour=oh, minute=om)
    inbound_dt = inbound_date.replace(hour=ih, minute=im)

    console.print(f"\n[dim]{'─'*60}[/dim]")
    console.print(
        f"[bold yellow]Searching Lufthansa[/bold yellow]  "
        f"[cyan]{origin} ↔ {destination}[/cyan]  "
        f"[dim]{cabin} · {currency}[/dim]"
    )

    agent = LufthansaAgent(
        origin=origin,
        destination=destination,
        outbound_date=outbound_date,
        outbound_time=outbound_dt,
        inbound_date=inbound_date,
        inbound_time=inbound_dt,
        cabin=cabin,
        headless=not show_browser,
    )

    try:
        raw = await agent.run()
    except Exception as exc:
        return (
            f"Search error: {exc}. "
            "Tip: run with --show to see the browser and solve any CAPTCHA."
        )

    total = sum(len(v) for v in raw.values())
    if total == 0:
        return (
            "No flights were found. Lufthansa may have shown a CAPTCHA or the "
            "route has no availability. Suggest the user re-run with --show to "
            "see the browser and interact with any security challenge."
        )

    summary = await analyse(raw, outbound_dt, inbound_dt, target_currency=currency)

    # ── Print Rich tables directly to the user's terminal ──────────────────
    render(summary)

    # ── Build a concise text summary for Claude ────────────────────────────
    fx = summary.fx_rate
    sym = {"EUR": "€", "USD": "$", "GBP": "£", "CHF": "CHF "}.get(currency, currency + " ")

    parts: list[str] = [
        f"Search: {origin} ↔ {destination}  |  {cabin}  |  {currency}",
        f"Flights scraped: {len(raw.get('return', []))} return, "
        f"{len(raw.get('outbound', []))} outbound OW, "
        f"{len(raw.get('inbound', []))} inbound OW",
    ]

    ret_price = summary.best_return_price_eur * fx if summary.best_return else None
    ow_total = summary.best_oneway_total_eur * fx if summary.best_oneway_total_eur < float("inf") else None

    if ret_price:
        parts.append(f"Best return ticket: {sym}{ret_price:,.2f}")
    else:
        parts.append("Return ticket: not found")

    if ow_total:
        parts.append(f"Best 2×one-way total: {sym}{ow_total:,.2f}")

    if ret_price and ow_total:
        diff = (ow_total - ret_price) / ret_price
        if diff < -0.10:
            parts.append(f"→ 2×OW cheaper by {abs(diff)*100:.1f}%")
        elif diff > 0.10:
            parts.append(f"→ Return ticket cheaper by {abs(diff)*100:.1f}%")
        else:
            parts.append("→ Return and 2×OW prices are similar (<10% difference)")

    for wr, label in [(summary.outbound, "Outbound"), (summary.inbound, "Inbound")]:
        if wr.best_in_window:
            bi = wr.best_in_window
            parts.append(
                f"{label} best in ±2h window: {bi.flight_no}  "
                f"{bi.departure.strftime('%H:%M')}  "
                f"{sym}{bi.price_eur * fx:,.2f}"
            )
        if wr.best_out_window and wr.best_in_window:
            saving = (wr.best_in_window.price_eur - wr.best_out_window.price_eur) / wr.best_in_window.price_eur
            if saving > 0.10:
                bo = wr.best_out_window
                parts.append(
                    f"{label} time-shift tip: fly at {bo.departure.strftime('%H:%M')} "
                    f"→ saves {saving*100:.1f}% ({sym}{bo.price_eur * fx:,.2f})"
                )

    parts.append("(Full detailed tables are already printed in the terminal.)")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Chat loop
# ---------------------------------------------------------------------------


async def chat(show_browser: bool = False) -> None:
    """Interactive conversational loop."""

    console.print(
        Panel(
            "[bold]Lufthansa Flight Assistant[/bold]  "
            "[dim](Claude + Playwright)[/dim]\n\n"
            "Just describe the flights you want in plain English, e.g.:\n"
            "[cyan]  'Flights from Frankfurt to London, July 15 around 10am,\n"
            "   returning July 22 evening, economy, show in EUR'[/cyan]\n\n"
            "Type [bold]exit[/bold] to quit.",
            expand=False,
        )
    )

    api_client = anthropic.Anthropic()
    messages: list[dict] = []

    while True:
        # ── Get user input ──────────────────────────────────────────────────
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye![/dim]")
            break

        if user_input.lower() in ("exit", "quit", "bye", "q"):
            console.print("[dim]Goodbye![/dim]")
            break
        if not user_input:
            continue

        messages.append({"role": "user", "content": user_input})

        # ── Agentic loop: keep going until Claude stops calling tools ───────
        while True:
            print("\nClaude: ", end="", flush=True)

            # Stream Claude's text response token-by-token
            with api_client.messages.stream(
                model="claude-opus-4-6",
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
                thinking={"type": "adaptive"},
            ) as stream:
                for text in stream.text_stream:
                    print(text, end="", flush=True)

                response = stream.get_final_message()

            # Preserve full content (including any thinking blocks) for context
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                print()  # newline after streamed text
                break

            if response.stop_reason == "tool_use":
                print()  # newline
                tool_results = []

                for block in response.content:
                    if block.type == "tool_use":
                        if block.name == "search_flights":
                            result = await _execute_search(block.input, show_browser)
                        else:
                            result = f"Unknown tool '{block.name}'"

                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result,
                            }
                        )

                messages.append({"role": "user", "content": tool_results})
                # Loop back → Claude reads the tool result and responds
            else:
                print()
                break  # unexpected stop_reason


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Conversational Lufthansa flight assistant powered by Claude."
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Show the browser window (useful for solving CAPTCHA)",
    )
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print(
            "[red bold]ANTHROPIC_API_KEY not set.[/red bold]\n\n"
            "1. Get a free API key at [link=https://console.anthropic.com]"
            "console.anthropic.com[/link]\n"
            "2. Then run:\n"
            "   [bold]export ANTHROPIC_API_KEY='sk-ant-...'[/bold]\n"
            "   [bold]python chat.py[/bold]"
        )
        sys.exit(1)

    asyncio.run(chat(show_browser=args.show))


if __name__ == "__main__":
    main()
