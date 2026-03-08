# Lufthansa Flight Search Agent

A Python + Playwright agent that searches **lufthansa.com** for flights,
compares return tickets against two separate one-ways, applies live FX
conversion, and shows whether flying ±2 hours from your preferred time is
meaningfully cheaper (>10 % threshold).

---

## Features

| Feature | Detail |
|---|---|
| **Three searches** | Return ticket, outbound one-way, inbound one-way |
| **±2 h time window** | All flights within ±2 h of your preferred departure are collected |
| **Price comparison** | Return vs. 2× one-way total |
| **Live FX conversion** | Via [frankfurter.app](https://www.frankfurter.app) – no API key needed |
| **Savings analysis** | Flags if shifting ±2 h saves >10 % |
| **Rich tables** | Clean formatted output in the terminal |

---

## Quick Start

```bash
# 1. Install dependencies (creates .venv automatically)
bash setup.sh

# 2. Run a search
.venv/bin/python main.py \
    --origin FRA \
    --destination LHR \
    --outbound-date 2024-07-15 \
    --outbound-time 10:00 \
    --inbound-date  2024-07-22 \
    --inbound-time  17:00 \
    --cabin economy \
    --currency EUR
```

Or manually:

```bash
pip install -r requirements.txt
playwright install chromium

python main.py \
    --origin MUC \
    --destination JFK \
    --outbound-date 2024-08-01 \
    --outbound-time 09:00 \
    --inbound-date  2024-08-10 \
    --inbound-time  14:00 \
    --cabin business \
    --currency USD
```

---

## CLI Reference

```
Options:
  --origin TEXT          Origin IATA code (e.g. FRA)      [required]
  --destination TEXT     Destination IATA code (e.g. LHR) [required]
  --outbound-date TEXT   Outbound date YYYY-MM-DD          [required]
  --outbound-time TEXT   Preferred outbound departure HH:MM[required]
  --inbound-date TEXT    Inbound date YYYY-MM-DD           [required]
  --inbound-time TEXT    Preferred inbound departure HH:MM [required]
  --cabin TEXT           economy | premium_economy | business | first
                         [default: economy]
  --currency TEXT        Display currency ISO code         [default: EUR]
  --headless/--no-headless  Hide/show browser             [default: headless]
  --debug                Save browser screenshots
```

---

## Output Tables

### Table 1 – Return vs Two One-Ways
Compares the cheapest return ticket found against the sum of the cheapest
outbound + inbound one-way tickets (within the ±2 h windows).

### Table 2 & 3 – Window Flight Lists
All flights on each date, annotated as `✓ in window` (within ±2 h of your
target time) or `outside`.  The cheapest in-window flight is highlighted.

### Table 4 – Time-Shift Savings Analysis
For each direction: best price within ±2 h vs. best price outside the window.
Marks **YES** if shifting saves >10 %, **NO** otherwise.

---

## Architecture

```
main.py          CLI entry point (Typer)
agent.py         Playwright browser automation & scraping
fx.py            Live FX rate fetching (frankfurter.app)
formatter.py     Price analysis engine + Rich table renderer
requirements.txt Python dependencies
setup.sh         One-shot setup helper
```

### agent.py

- Launches Chromium via Playwright (headless by default).
- Opens three independent browser contexts (return, outbound OW, inbound OW).
- Handles Lufthansa's cookie-consent overlay with multiple selector fallbacks.
- Fills origin / destination autocomplete fields, date pickers, and cabin class.
- Scrapes flight cards using structured selectors; falls back to full-page text
  parsing with regex if the DOM layout changes.

### Limitations / Notes

- **Bot detection**: Lufthansa.com uses Akamai bot-management. The agent uses
  realistic browser headers and disables the `navigator.webdriver` flag, but
  may still be challenged by a CAPTCHA.  Run with `--no-headless` to solve
  CAPTCHAs manually.
- **Layout changes**: Lufthansa periodically redesigns their booking widget.
  The selector lists in `agent.py` cover common patterns; update them if a
  field is not found.
- **Prices reflect lowest available fare** at scrape time – they may differ
  from what you see in a normal browser session due to personalisation.

---

## License

MIT
