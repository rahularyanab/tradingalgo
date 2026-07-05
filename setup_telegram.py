"""
Telegram Bot Setup Wizard
=========================
Run:  python setup_telegram.py

Steps:
  1. Guides you to create a bot via @BotFather
  2. Accepts your bot token
  3. Fetches your chat ID automatically (no manual lookup needed)
  4. Sends a test message to confirm everything works
  5. Writes credentials to .env
"""

import json
import sys
import time
from pathlib import Path

import requests

ENV_FILE = Path(__file__).parent / ".env"


def _bold(text): return f"\033[1m{text}\033[0m"
def _green(text): return f"\033[32m{text}\033[0m"
def _red(text):   return f"\033[31m{text}\033[0m"
def _yellow(text): return f"\033[33m{text}\033[0m"


def print_step(n, text):
    print(f"\n{_bold(f'Step {n}:')} {text}")


def get_updates(token: str) -> list:
    url  = f"https://api.telegram.org/bot{token}/getUpdates"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json().get("result", [])


def send_message(token: str, chat_id: str, text: str) -> bool:
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
    return resp.ok


def write_env(token: str, chat_id: str):
    existing = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()

    existing["TELEGRAM_BOT_TOKEN"] = token
    existing["TELEGRAM_CHAT_ID"]   = chat_id

    lines = [
        "# ── Zerodha KiteConnect ──────────────────────────────────────────",
        f"KITE_API_KEY={existing.get('KITE_API_KEY', 'your_api_key_here')}",
        f"KITE_API_SECRET={existing.get('KITE_API_SECRET', 'your_api_secret_here')}",
        "",
        "# ── Telegram ─────────────────────────────────────────────────────",
        f"TELEGRAM_BOT_TOKEN={token}",
        f"TELEGRAM_CHAT_ID={chat_id}",
    ]
    ENV_FILE.write_text("\n".join(lines) + "\n")


def main():
    print(_bold("\n══════════════════════════════════════════"))
    print(_bold("   Nifty Bot — Telegram Setup Wizard"))
    print(_bold("══════════════════════════════════════════"))

    # ── Step 1: Create the bot ────────────────────────────────────
    print_step(1, "Create your Telegram bot")
    print("""
  Open Telegram and do the following:

  a) Search for  @BotFather  and open it
  b) Send:  /newbot
  c) Enter a display name, e.g.:  Nifty Option Signals
  d) Enter a username (must end in 'bot'), e.g.:  NiftyOptionBot
  e) BotFather will reply with your bot token — looks like:
     1234567890:ABCdefGHIjklmNOPQrstUVWxyz
""")

    token = input("  Paste your bot token here: ").strip()
    if not token or ":" not in token:
        print(_red("  Invalid token format. Should be like: 1234567890:ABC...xyz"))
        sys.exit(1)

    # Quick token validation
    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        data = resp.json()
        if not data.get("ok"):
            print(_red(f"  Token rejected by Telegram: {data.get('description')}"))
            sys.exit(1)
        bot_name = data["result"]["first_name"]
        bot_user = data["result"]["username"]
        print(_green(f"  ✓ Bot verified: {bot_name} (@{bot_user})"))
    except Exception as e:
        print(_red(f"  Could not reach Telegram API: {e}"))
        sys.exit(1)

    # ── Step 2: Get chat ID ───────────────────────────────────────
    print_step(2, "Connect your Telegram account to the bot")
    print(f"""
  a) Open Telegram
  b) Search for  @{bot_user}  and open it
  c) Press  START  or send any message (e.g. "hi")
  d) Come back here and press Enter
""")
    input("  Press Enter once you've sent a message to the bot... ")

    print("  Fetching your chat ID...", end="", flush=True)

    chat_id = None
    for attempt in range(3):
        try:
            updates = get_updates(token)
            if updates:
                # Take the most recent message
                msg     = updates[-1].get("message") or updates[-1].get("my_chat_member", {})
                chat    = msg.get("chat", {})
                chat_id = str(chat.get("id", ""))
                if chat_id:
                    break
        except Exception as e:
            pass
        if attempt < 2:
            print(".", end="", flush=True)
            time.sleep(2)

    if not chat_id:
        print(_red("\n  Could not fetch chat ID automatically."))
        print("  Visit this URL in your browser and find 'chat':{'id': XXXXX}:")
        print(f"  https://api.telegram.org/bot{token}/getUpdates")
        chat_id = input("\n  Enter your chat ID manually: ").strip()

    print(_green(f"\n  ✓ Chat ID: {chat_id}"))

    # ── Step 3: Send test message ─────────────────────────────────
    print_step(3, "Sending test message to your Telegram...")

    test_msg = (
        "✅ *Nifty Option Selling Bot — Connected!*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Your bot is set up correctly.\n\n"
        "You will receive:\n"
        "• 📊 Signal alerts every 15 minutes\n"
        "• ⚠️ Trade warnings when conditions change\n"
        "• 🔴 CALL SELL / 🟢 PUT SELL signals\n\n"
        "_Setup complete. Run `python main.py` to start the bot._"
    )

    if send_message(token, chat_id, test_msg):
        print(_green("  ✓ Test message sent! Check your Telegram."))
    else:
        print(_red("  ✗ Could not send message. Check token and chat ID."))
        sys.exit(1)

    # ── Step 4: Write .env ────────────────────────────────────────
    print_step(4, "Saving credentials to .env...")
    write_env(token, chat_id)
    print(_green(f"  ✓ Saved to {ENV_FILE}"))

    # ── Done ──────────────────────────────────────────────────────
    print(f"""
{_bold("══════════════════════════════════════════")}
{_green("  Telegram bot setup complete!")}
{_bold("══════════════════════════════════════════")}

  Next steps:
  1. Add your Zerodha API key to .env (see .env.example)
  2. Run:  python auth/kite_login.py   (once each morning)
  3. Run:  python main.py              (start the live bot)

  To test signals manually:
  Run:  python test_signal.py
""")


if __name__ == "__main__":
    main()
