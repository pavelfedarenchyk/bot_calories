# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Telegram calorie-tracking bot, version `0.5.0`. The whole app is one module, `bot.py` (~1570 lines), organized top-to-bottom by Russian banner comments (`# ===== БАЗА ДАННЫХ SQLITE =====`, `# ===== КЛАВИАТУРЫ =====`, …) — use those to navigate. All user-facing text is Russian.

## Setup and commands

No `pyproject.toml`, tests, linter config, Dockerfile, or CI. `requirements.txt` pins the two direct dependencies, `aiogram~=3.31` and `aiohttp~=3.14`, which currently resolve to 3.31.0 and 3.14.3 on Python 3.14.7; `pydantic`, `aiofiles`, `magic-filter` and the rest are transitive via aiogram. `aiohttp` is listed explicitly even though aiogram pulls it in, because `bot.py` imports it directly.

```bash
cp .env.example .env        # fill TELEGRAM_BOT_TOKEN (from BotFather)
source .venv/bin/activate
python bot.py               # long-polling; Ctrl-C to stop
```

- **The venv has no `pip`** (it was created with `uv`), so `python -m pip ...` fails with `No module named pip`. Use `uv pip list` / `uv pip install --python .venv/bin/python <pkg>`. Recreate the venv with `uv venv && uv pip install -r requirements.txt`.
- `load_env()` reads `.env` next to `bot.py` into `os.environ` without overwriting variables already set, then `TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")`; a missing value exits with an explanatory `SystemExit` rather than failing inside aiogram. No `python-dotenv`: the loader is a few lines of stdlib and keeps the dependency list at two.
- `calorie_bot.db` is created on first run. Deleting it is the only reset: there are no migrations, and `create_tables()` uses `CREATE TABLE IF NOT EXISTS`, so **a new column never reaches an existing DB file**.
- Never read `.venv/` (in `.claudeignore`, also denied in the untracked `.claude/settings.json`), `docs/codemie/` (analytics dumps), or `*.db`.

## Agent configuration

`.mcp.json` declares `github` and `context7`, which need a one-time approval on first use; user-level servers from `~/.claude.json` load alongside them, including `codebase-memory`. `.claude/bin/github-mcp.sh` wraps `github-mcp-server` and reads the token from `gh auth token` at every start, so no credential is ever written to a file; it runs read/write — add `--read-only` to the exec line to restrict it.

**None of this is in git**: `.gitignore` excludes `.mcp.json` and all of `.claude/`, so a fresh clone has no agent configuration and must recreate it by hand. Only `CLAUDE.md` and `.claudeignore` are tracked.

## Architecture

Module-level singletons, created at import and used as globals by every handler: `bot`, `dp` (Dispatcher with `MemoryStorage`), and `db = Database()` at [bot.py:313](bot.py#L313).

**Two ID spaces — the main source of bugs.** `users.telegram_id` is **not** `users.id`, and `foods`, `diary`, `friends` and `achievements` all foreign-key to the internal `users.id`. Handlers must translate first: `user_id = db.get_user_id(message.from_user.id)`. Every `Database` method takes the internal id except `get_or_create_user()`; the shared renderers `show_today` / `show_week` / `show_history` take `(telegram_id, message)` and translate internally, because both message and callback handlers call them.

**Food lookup pipeline.** Local catalog `db.get_food(user_id, name)` → on a miss `search_openfoodfacts()` (top 5 from `world.openfoodfacts.org/cgi/search.pl`, kcal/100 g + macros) → the chosen product is **written into that user's `foods` table** ([bot.py:1069](bot.py#L1069)), so the next lookup hits the catalog → the bot asks for grams and writes a `diary` row. `foods.calories` is per 100 g while `diary.calories` is an absolute total, and `process_meal_grams` falls back to `calories_per_100 = 0` for a missing product, silently logging a 0-kcal meal. The lowercased product **name is the de-facto key** — `get_food`/`delete_food` match on it and `callback_data` embeds it as `food_{name}`, a live risk against Telegram's 64-byte cap.

**Routing.** `@dp.message` / `@dp.callback_query` with lambda filters, where reply-keyboard **labels including emoji are the router keys** — `@dp.message(lambda m: m.text == "📊 Сегодня")` — so changing a label in `get_main_keyboard()` silently breaks its handler; update both together. Callback prefixes: `foods_page_{n}`, `food_{name}`, `select_product_{index}` (positional index into `search_results` held in FSM state), `accept_friend_`, `activity_`, plus bare actions (`show_today`, `back_to_menu`, `manual_add`, `noop`, …).

**FSM.** Four groups: `SetupState` (weight → height → age → activity), `AddFoodState`, `AddMealState`, `FriendState`. `MemoryStorage` means **every in-progress dialog is lost on restart**. `select_product_from_search` serves both `AddFoodState` and `AddMealState`, branching on `await state.get_state()`.

**Daily calorie target.** Mifflin-St Jeor, computed only in `Database.update_user_profile` ([bot.py:130](bot.py#L130)) and persisted to `users.daily_norm` — recalculated when the profile is saved, never on read:

```python
daily_norm = int((10 * weight + 6.25 * height - 5 * age - 161) * activity)
```

The `-161` constant is the **female** variant, hardcoded; there is no sex field.

**Rendering.** Every reply uses `parse_mode="Markdown"` and emoji/ASCII art rather than images: `get_progress_bar()` (20 cells), `create_weekly_chart()` (vertical bars from `weekly_stats`), `get_mood_emoji()`, `get_advice()`.

**Achievements.** `ACHIEVEMENTS` dict plus `check_achievements(user_id, action_type, data=None)`, called after meal adds and friend accepts; it returns newly earned keys for the caller to append to its reply.

## Conventions

- User-facing strings stay Russian.
- Add methods to `Database` instead of writing SQL in handlers — [bot.py:633](bot.py#L633) and [bot.py:1314](bot.py#L1314) reach into `db.cursor` directly; don't copy that.
- Keep `callback_data` within Telegram's 64-byte cap.
- A release bumps `__version__` in `bot.py`, the git tag (`v0.5.0`) and a Keep-a-Changelog entry in `CHANGELOG.md` together.
- Never commit `.env` or `calorie_bot.db`.

## Not wired up — do not assume these work

- `json` and `re` are imported and never used; `OPENAI_API_KEY` in `.env.example` is referenced nowhere in the code.
- `users.streak_days` is never read or written, so `week_streak` can never be earned; `weight_master` is a stub (`заглушка`). `is_premium` and `referrer_id` exist with no logic behind them.
