"""Flipkart stock checker -> Telegram alert. No location, no login."""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# ---- Edit these if needed (or set them as env vars) ----------------------------
PRODUCT_URL = os.getenv(
    "PRODUCT_URL",
    "https://www.flipkart.com/asus-expertbook-p5-full-metal-oled-ai-pc-1-yr-adp-"
    "intel-core-ultra-7-255h-16-gb-1-tb-ssd-windows-11-home-p5406cca-sf0161ws-laptop/"
    "p/itmd6dd1bfb91dbf?pid=COMHHT9GHUHHZXYK&lid=LSTCOMHHT9GHUHHZXYKJTLNDE",
)
NAME = os.getenv(
    "PRODUCT_NAME",
    "ASUS ExpertBook P5 (Core Ultra 7 255H, 16GB, 1TB) P5406CCA-SF0161WS 14 inch",
)
# The page must contain this text, so we never watch the wrong model (e.g. 16 inch)
REQUIRE_TEXT = os.getenv("REQUIRE_TEXT", "P5406CCA").lower()
# Re-send the in-stock alert every N minutes while it stays in stock (0 = off)
REMIND_MIN = int(os.getenv("REMIND_MIN", "30"))
# Send one "bot is alive" message per day after 9 AM IST (set to "false" to turn off)
HEARTBEAT = os.getenv("HEARTBEAT", "true").lower() == "true"

OUT_WORDS = ("sold out", "currently unavailable", "out of stock", "notify me")
BLOCK_WORDS = ("captcha", "unusual traffic", "access denied", "are you a human")
# ---------------------------------------------------------------------------------

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
IST = timezone(timedelta(hours=5, minutes=30))
STATE = Path("state.json")
MANUAL = os.getenv("MANUAL_RUN") == "true"
FAIL_HELP = {
    "WRONG_PAGE": "The page opened is not the expected model. Check PRODUCT_URL.",
    "BLOCKED": "Flipkart is blocking the bot (captcha or access denied).",
    "UNKNOWN": "Could not tell if it is in stock (page layout may have changed).",
    "ERROR": "The page failed to load.",
}


def need(name):
    value = os.getenv(name)
    if not value:
        sys.exit(f"Missing secret/env var: {name}")
    return value


TOKEN = need("TELEGRAM_BOT_TOKEN")
CHAT_ID = need("TELEGRAM_CHAT_ID")

if not PRODUCT_URL.isascii():
    sys.exit("PRODUCT_URL has non-English lookalike characters. Retype it.")


def send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )
    if not r.ok:
        sys.exit(f"Telegram error {r.status_code}: {r.text}")


def get_status():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(
                user_agent=UA, locale="en-IN", viewport={"width": 1366, "height": 900}
            )
            page = ctx.new_page()
            page.goto(PRODUCT_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(5000)
            page.screenshot(path="screenshot.png")
            text = page.inner_text("body").lower()
            seen = (page.title() + " " + text).lower()

            if any(w in seen for w in BLOCK_WORDS):
                return "BLOCKED"
            if REQUIRE_TEXT and REQUIRE_TEXT not in seen:
                return "WRONG_PAGE"
            if any(w in text for w in OUT_WORDS):
                return "OUT_OF_STOCK"

            buttons = page.get_by_text(
                re.compile(r"^\s*(add to cart|buy now)\s*$", re.I)
            )
            for i in range(buttons.count()):
                if buttons.nth(i).is_visible():
                    return "IN_STOCK"
            return "UNKNOWN"
        finally:
            browser.close()


def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    now = datetime.now(IST)
    try:
        status = get_status()
    except Exception as e:  # network error, timeout, etc.
        print("Check failed:", e)
        status = "ERROR"
    print("Status:", status)

    prev = state.get("status")
    if status == "IN_STOCK":
        state["fails"] = 0
        last = state.get("last_alert")
        remind_due = (
            REMIND_MIN > 0
            and last
            and now - datetime.fromisoformat(last) >= timedelta(minutes=REMIND_MIN)
        )
        if prev != "IN_STOCK" or remind_due:
            send(f"The Product is currently In Stock.\n\n{NAME}\n\n{PRODUCT_URL}")
            state["last_alert"] = now.isoformat()
        state["status"] = status
    elif status == "OUT_OF_STOCK":
        state["fails"] = 0
        if prev == "IN_STOCK":
            send(f"Out of stock again: {NAME}")
        state["status"] = status
        state.pop("last_alert", None)
    else:  # BLOCKED / WRONG_PAGE / UNKNOWN / ERROR
        state["fails"] = state.get("fails", 0) + 1
        if state["fails"] == 4:  # alert once after 4 failed runs in a row
            send(f"⚠️ {FAIL_HELP[status]} Check the latest Actions run.")

    today = now.strftime("%Y-%m-%d")
    if HEARTBEAT and now.hour >= 9 and state.get("last_heartbeat") != today:
        send(f"💓 Bot is running. Latest status: {state.get('status', status)}")
        state["last_heartbeat"] = today

    if MANUAL:
        send(f"Manual check result: {status}\n{PRODUCT_URL}")

    STATE.write_text(json.dumps(state, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
