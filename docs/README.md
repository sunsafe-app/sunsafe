# ☀️ SunSafe

Smart sun exposure meter — real-time UV Index tracking, personalized
Telegram alerts, powered by the Gemini API and MCP (Model Context Protocol).

Final project for the AI Dev course.

## What it does

SunSafe tracks the UV Index at your location in real time, and calculates
how long you can safely stay in the sun — based on your skin type and
sunscreen use. When the UV is high, the bot sends a Telegram alert with a
clear recommendation (sunscreen, a hat, avoiding direct exposure).

**Usage example:** ask the bot "What's the UV right now in Tel Aviv?" — an
AI agent (powered by Gemini) calls a dedicated MCP server in real time to
fetch live UV data, and returns a personalized recommendation within
seconds.

## How the Exposure Score is calculated

SunSafe translates a raw UV Index into a personal exposure score of
0–100+, built from three multiplied layers:

1. **Base safe time** — `200 / UV`. A standard rule-of-thumb approximation:
   the higher the UV, the shorter the safe unprotected-exposure time,
   inversely.
2. **Skin-type factor** (Fitzpatrick scale I–VI) — lighter skin burns
   faster and gets a factor below 1 (shortens the safe time); darker skin
   tolerates longer exposure and gets a factor above 1:

   | Skin type | I | II | III | IV | V | VI |
   |---|---|---|---|---|---|---|
   | Factor | 0.5 | 0.75 | 1.0 | 1.5 | 2.5 | 4.0 |

3. **Effective sunscreen protection** — `1 + (labeled SPF − 1) × 0.4`. The
   labeled SPF number isn't taken at face value: in practice people apply
   less sunscreen than lab testing assumes, so real-world protection is
   roughly 40% of the theoretical value (a labeled SPF 30 gives an
   effective protection factor of about 12.6, not 30).

Combining the three layers gives a total safe exposure time, compared
against the actual time spent outside:

```
safe_minutes   = (200 / UV) × skin_factor × effective_spf
exposure_score = (actual_minutes / safe_minutes) × 100
```

A result above 100 means actual exposure exceeded the calculated safe
threshold. The score maps to a level:

| Score | Level |
|---|---|
| Below 40 | Low (good) |
| 40–69 | Moderate (warning) |
| 70–99 | Moderate-high (serious) |
| 100+ | Exceeded (critical) |

This is an approximation built on established concepts (UV Index, the
Fitzpatrick scale, real-world SPF effectiveness drop-off) — **not a
clinically validated medical formula**, and not a substitute for medical
advice. Intended for personal tracking and awareness only.

> Currently this formula is only implemented in the demo page (JS);
> a backend implementation as an MCP Tool is tracked in the TODO list
> (see [`CLAUDE.md`](./CLAUDE.md)).

## Architecture

```
User (Telegram) ⇄ Agent Loop (Gemini API + Function Calling)
                        │
                        ▼
                 MCP Client ⇄ MCP Weather Server ⇄ Open-Meteo API
```

- **Agent Loop** — implemented manually in Python against the Gemini API
  (`google-genai`), with no external framework.
- **MCP Weather Server** — a standalone server wrapping Open-Meteo (free,
  no API key) that exposes four tools as standard MCP Tools:
  `geocode_city` (resolves a city name to coordinates), `get_current_uv`,
  `get_uv_forecast`, and `log_uv_reading` (persists a UV reading to
  Supabase). The same server can also be connected to Claude Desktop for
  manual testing.
- **Telegram Bot** — the user-facing interface for alerts and real-time
  queries.

For full technical detail, development conventions, and architecture
decisions — see [`CLAUDE.md`](./CLAUDE.md).

## Setup & Running

Requirements: Python 3.12+, a Telegram account, a free Gemini API key.

```powershell
git clone https://github.com/sunsafe-app/sunsafe.git
cd sunsafe

python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in:

```
BOT_TOKEN=                     # from @BotFather on Telegram
CHAT_ID=                       # your chat_id (see instructions in CLAUDE.md)
GEMINI_API_KEY=                # free key from https://aistudio.google.com/apikey
SUPABASE_URL=                  # your Supabase project URL
SUPABASE_SERVICE_ROLE_KEY=     # service_role key (not anon) from Supabase
```

Run:

```powershell
python mcp_agent_loop.py     # test the Agent Loop against the MCP Server
python send_uv_report.py     # actually send a UV alert to Telegram
```

## Project Status

In active development — see the detailed TODO list in
[`CLAUDE.md`](./CLAUDE.md#todo-לפי-סדר-עדיפות).

## License

Academic project — AI Dev course.
