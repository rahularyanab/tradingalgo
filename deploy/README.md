# Running the bot for family on one shared server

Your own bot is untouched: it stays `trading-bot.service` with no profile, exactly as before.
Each family member gets their **own folder and their own process**:

    /opt/algo-users/<name>/
        profile.toml   their settings (capital, lots, daily limit)       — safe for them to edit
        .env           their secrets (Telegram + broker keys), mode 600   — owner-readable only
        logs/  data/   their own bot log, trade state, journal, S/R levels, broker token

Nothing is shared between people: a key missing from someone's `.env` stays empty (it is never filled in from yours),
and a bot with missing secrets refuses to start.

## One-time, on the server
    cp deploy/trading-bot@.service deploy/trading-login@.service deploy/trading-login@.timer /etc/systemd/system/
    systemctl daemon-reload

## Adding a person
    cd /opt/trading-algo
    python3 scripts/provision_user.py priya --capital 1000000            # add --lots 2 / --max-loss 10000 / --experience experienced if wanted
    nano /opt/algo-users/priya/.env                                      # fill in the secrets (see below)
    ALGO_USER_DIR=/opt/algo-users/priya python3 scripts/check_profile.py # dry run: validates, shows sizing, previews the Telegram guide
    systemctl enable --now trading-bot@priya
    systemctl enable --now trading-login@priya.timer                     # daily broker login 8:45 AM IST

The first time the bot connects, the person gets the welcome guide on Telegram automatically (and `/start` repeats it).

## What each person must set up first
1. **Telegram bot:** open @BotFather, send `/newbot`, copy the token. Send any message to the new bot, then get your numeric chat id from @userinfobot.
2. **Zerodha Kite Connect app** (developers.kite.trade): note the API key and secret. Set its redirect URL as in your own app.
3. **Static IP:** SEBI's rules need the IP that places API orders to be registered with the broker. The shared server's IP must be
   registered on each person's broker account. Check with each broker whether family members may share one IP.
4. **Capital and margin:** fund the account. At the suggested size the bot uses roughly 15-20% of capital as margin.

## Day-to-day
    systemctl status trading-bot@priya
    journalctl -u trading-bot@priya -f
    systemctl restart trading-bot@priya

After you deploy new code to /opt/trading-algo, restart everyone's bot (none should have an open trade):
    systemctl restart 'trading-bot@*'

## Security — read this
* `auto_login` stores each person's broker **password and TOTP secret** in their `.env` on this server. Treat the server as holding the keys to
  their accounts: keep it patched, SSH by key only, disk backups encrypted, and tell family exactly what is stored.
* Prefer asking each person to log in themselves if they would rather not share a TOTP secret. A "send me the login link on Telegram" flow is a
  possible later addition.
* Never put secrets in `profile.toml`; never commit `.env`.

## Angel One

    python3 scripts/provision_user.py raj --capital 1000000 --broker angel      # paper mode by default

Per person:
1. **SmartAPI app** (smartapi.angelone.in): create an app, note the API key, and register the shared server's IP on it (same static-IP rule as Zerodha).
2. **Enable TOTP** on their Angel account and keep the base32 secret it shows — the daily login is a plain API call (client code + PIN + TOTP), no browser.
3. Fill `ANGEL_API_KEY`, `ANGEL_CLIENT_CODE`, `ANGEL_PASSWORD` (their login PIN), `ANGEL_TOTP_SECRET` in their `.env`.

**Angel starts in PAPER MODE** — real Angel market data, but orders are simulated in a per-person ledger (`paper_ledger.json`); nothing reaches their account. The adapter was written without access to an Angel account, so each of these is an assumption until checked:
response field names of the order book and positions, how many tokens one quote request may carry (and the rate limit), whether option quotes carry open interest, and real order placement.

Verify, **during market hours (09:15-15:30 IST)**:

    ALGO_USER_DIR=/opt/algo-users/raj python3 scripts/verify_angel.py               # read-only: login, funds, price, candles, batching, OI, positions, order book, the bot's own option chain
    ALGO_USER_DIR=/opt/algo-users/raj python3 scripts/verify_angel.py --test-order  # optional: ONE far out-of-the-money 1-lot limit buy at ₹0.05, then modify + cancel it (asks you to type YES)

Then run the bot in paper mode for a couple of weeks and compare its decisions with the Zerodha instance. Only then set `paper = false` in their `profile.toml` (or provision with `--live`) and restart. The Telegram guide says PAPER MODE until then.

Open-interest signals are the single biggest dependency: if `verify_angel.py` reports zero open interest for options, the bot's PCR / wall / writing signals cannot work on Angel and it should not go live.

## Single-lot LIVE test on the owner's own Angel account

    python3 scripts/provision_user.py rahul-angel --broker angel --live --lots 1 --no-pyramiding --owner --capital <rupees> --max-loss <rupees> [--loss-per-lot <rupees>]

* `--lots 1 --no-pyramiding`: exactly 1 lot is ever held (a strangle is 1 lot on each side); size is never added to a winner.
* `--live`: real orders (`paper = false`). **The bot refuses to start live on Angel (exit 3, with a Telegram message, no restart loop) until `scripts/verify_angel.py` has passed for that account** — a clean run during market hours with option open interest present. The pass is recorded in `.angel_verified` (a fingerprint of the account, valid 30 days). There is deliberately no override.
* `--loss-per-lot`: only when your capital is too small for the default planning figure (₹4,500 per lot). It says what you accept one lot losing in a bad exit; the profile and the Telegram guide then warn that a fast market can take one lot past the daily limit.
* `--owner`: that bot's Telegram may use `/pauseall` / `/resumeall` for every bot on the server.
* Use a **separate Telegram bot token** for this instance, so its alerts do not mix with the Zerodha bot's.

Order of events: provision → fill `.env` → `verify_angel.py` in market hours (add `--test-order` once) → `systemctl enable --now trading-bot@rahul-angel` and the login timer. First live day: watch it from the Telegram chat and keep the Angel app open.

## Pause / resume (Telegram)

| command | who | effect |
|---|---|---|
| `/pause` | any bot's chat | this bot stops NEW trades, adding size, and re-entering after a reversal |
| `/resume` | same | allows new trades again (does not override the owner's pause-all) |
| `/status` | same | paused or running, and any open trade |
| `/pauseall`, `/resumeall` | profiles with `owner = true` (and the owner's own instance) | the same for every bot on the server (`/opt/trading-algo/pause_all.flag`) |

Pausing never switches off protection: an open trade keeps its stop-loss, the day-loss floor, signal and profit exits, the 3:25 PM flatten and its fill checks. If you want everything closed right now, close it in the broker app, then `/pause` — the bot notices a manual close within a minute or two and will not trade against it. A paused bot reminds you once a day. The pause survives restarts (it is a file).

## Not included yet
A public website and any public posting of trades.
