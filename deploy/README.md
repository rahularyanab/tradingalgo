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

## Not included yet
Angel One (the profile accepts only `zerodha` today and says so clearly), a public website, and any public posting of trades.
