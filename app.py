import os
import csv
import io
import math
import smtplib
import secrets
import uuid
import json
import re
import time
import base64
from functools import wraps
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone, date
import calendar
import random
from email.message import EmailMessage
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory, jsonify, Response
from supabase import create_client, Client
from dotenv import load_dotenv
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash, check_password_hash
from report_pdf import build_report_pdf
from trip_accounting import calculate_trip_balances, suggested_settlements, trip_payables_for_user

load_dotenv()

app = Flask(__name__)
MINTO_VERSION = "2.0.0"
app.secret_key = os.environ["SECRET_KEY"]
# How long a logged-in session survives with no activity at all — separate
# from the Supabase access token's 1-hour life, which refresh_if_needed()
# renews automatically as long as this outer session is still alive.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
# Cookie hardening. Lax stops other sites from making a logged-in browser
# submit this app's POST forms (there are no CSRF tokens). Secure keeps the
# cookie off plain HTTP; set SESSION_COOKIE_SECURE=0 only for local http dev.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "1") != "0"


@app.after_request
def add_no_cache_headers(response):
    """Every page here reflects a specific logged-in session — if a browser
    (or a backgrounded/suspended mobile tab) serves a disk-cached copy of one
    without re-checking with the server, someone could see stale content
    (e.g. an old page's state) sitting behind a nav bar rendered fresh for
    whatever their *current* session actually is. Cheap to disable caching
    entirely here since this isn't a content site where caching matters.
    Static assets (favicon, etc.) are exempt — they already have their own
    ?v= cache-busting query param when they actually change."""
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


MODES = ("personal", "trip")
# Profile avatars are WebP portraits stored as static/avatars/avatar-NN.webp.
# Keep the legacy database column name for compatibility and translate existing emoji values on login.
PROFILE_AVATARS = [f"avatar-{i:02d}" for i in range(1, 11)]
LEGACY_PROFILE_EMOJI_MAP = {
    "😀": "avatar-01", "😃": "avatar-02", "😄": "avatar-03", "😁": "avatar-04",
    "😆": "avatar-05", "😅": "avatar-06", "😂": "avatar-07", "🤣": "avatar-08",
    "😊": "avatar-09", "😇": "avatar-10", "🙂": "avatar-11", "🙃": "avatar-12",
    "😉": "avatar-13", "😌": "avatar-14", "😍": "avatar-15", "🥰": "avatar-16",
    "😘": "avatar-01", "😗": "avatar-02", "😙": "avatar-03", "😚": "avatar-04",
    "😋": "avatar-05", "😛": "avatar-06", "😜": "avatar-07", "🤪": "avatar-08",
    "🤨": "avatar-09", "🧐": "avatar-10", "🤓": "avatar-11", "😎": "avatar-12",
    "🥳": "avatar-13", "🤩": "avatar-14", "🦊": "avatar-15", "🐼": "avatar-16",
    "👤": "avatar-01",
}

# In Trip mode the app shows trip pages and nothing else. This is an allow
# list rather than a block list on purpose: any page added later is hidden in
# Trip mode by default instead of leaking personal finances onto a screen
# that's being shared around a group.
TRIP_MODE_ENDPOINTS = {
    "trips", "trip_detail", "trip_home", "friends", "update_trip", "add_trip_friends", "remove_trip_friend",
    "add_trip_expense", "edit_trip_expense", "delete_trip_expense", "delete_trip",
    "add_trip_settlement", "update_trip_settlement_status", "update_trip_status", "set_mode",
    "accept_trip_expense_share", "reject_trip_expense_share", "reassign_rejected_trip_share",
    "record_trip_settlement_receipt", "linked_transaction",
    "logout", "login", "signup", "favicon", "health", "static",
    "service_worker", "web_manifest",
}


def home_url():
    """Where 'home' is depends on the mode: the entry form for Personal, the
    current trip for Trip mode."""
    if session.get("mode") == "trip":
        return url_for("trip_home")
    return url_for("entry")


def load_saved_mode(client, user_id):
    """The mode this user left the app in last time, so logging back in during
    a trip puts them straight back into it. Falls back to Personal if it
    can't be read (no profile row yet, or the app_mode column isn't there)."""
    try:
        rows = client.table("profiles").select("app_mode").eq("id", user_id).execute().data
        if rows and rows[0].get("app_mode") in MODES:
            return rows[0]["app_mode"]
    except Exception:
        pass
    return "personal"


def load_saved_theme(client, user_id):
    """Load the user's saved appearance preference. Falls back to light for
    older profiles that do not have a theme value yet."""
    try:
        rows = client.table("profiles").select("theme").eq("id", user_id).execute().data
        if rows and rows[0].get("theme") in ("light", "dark"):
            return rows[0]["theme"]
    except Exception:
        pass
    return "light"


def save_theme(client, user_id, theme):
    if theme not in ("light", "dark"):
        return False
    try:
        client.table("profiles").upsert({"id": user_id, "theme": theme}).execute()
        return True
    except Exception:
        return False


def load_genz_mode(client, user_id):
    """Load the optional lighter, Gen Z wording preference."""
    try:
        rows = client.table("profiles").select("genz_mode").eq("id", user_id).execute().data
        return bool(rows and rows[0].get("genz_mode"))
    except Exception:
        return False


def save_genz_mode(client, user_id, enabled):
    try:
        client.table("profiles").upsert({
            "id": user_id,
            "genz_mode": bool(enabled),
        }).execute()
        return True
    except Exception:
        return False


def load_biometric_flag(client, user_id):
    """Whether this account turned biometric login on. Safe on databases that
    don't have the column yet."""
    try:
        rows = client.table("profiles").select("biometric_enabled").eq("id", user_id).execute().data
        return bool(rows and rows[0].get("biometric_enabled"))
    except Exception:
        return False


def load_profile_avatar(client, user_id):
    """Load the saved illustrated avatar and migrate legacy emoji values."""
    try:
        rows = (
            client.table("profiles")
            .select("profile_emoji")
            .eq("id", user_id)
            .limit(1)
            .execute()
            .data
        )
        saved = rows[0].get("profile_emoji") if rows else None
    except Exception:
        # Do not attempt a write when the profile read failed. The UI can use
        # the default for this request and retry on the next page load.
        app.logger.exception("Could not load profile avatar")
        return "avatar-01"

    if saved in PROFILE_AVATARS:
        return saved

    avatar = LEGACY_PROFILE_EMOJI_MAP.get(saved, "avatar-01")
    # Migrate a legacy emoji or missing/invalid value, but don't let a failed
    # migration break rendering of otherwise usable pages.
    try:
        client.table("profiles").upsert(
            {"id": user_id, "profile_emoji": avatar}
        ).execute()
    except Exception:
        app.logger.exception("Could not migrate profile avatar")
    return avatar


def save_profile_avatar(client, user_id, avatar):
    """Persist only a known avatar symbol; callers update the session on success."""
    if avatar not in PROFILE_AVATARS:
        return False
    try:
        client.table("profiles").upsert(
            {"id": user_id, "profile_emoji": avatar}
        ).execute()
        return True
    except Exception:
        app.logger.exception("Could not save profile avatar")
        return False


def load_credit_card_setup_completed(client, user_id):
    """Return whether this user has completed the one-time first credit-card
    payment setup. Older profiles safely fall back to False if the column has
    not been added yet."""
    try:
        rows = (
            client.table("profiles")
            .select("credit_card_setup_completed")
            .eq("id", user_id)
            .execute()
            .data
        )
        return bool(rows and rows[0].get("credit_card_setup_completed"))
    except Exception:
        return False


def save_credit_card_setup_completed(client, user_id):
    try:
        client.table("profiles").upsert({
            "id": user_id,
            "credit_card_setup_completed": True,
        }).execute()
        return True
    except Exception:
        return False


def load_display_name(client, user_id):
    """The name shown in the top bar. Empty when none is saved yet (or the
    display_name column isn't there), in which case the email name is used."""
    try:
        rows = client.table("profiles").select("display_name").eq("id", user_id).execute().data
        return (rows[0].get("display_name") or "") if rows else ""
    except Exception:
        return ""


def save_mode(client, user_id, mode):
    try:
        client.table("profiles").upsert({"id": user_id, "app_mode": mode}).execute()
    except Exception:
        # The session already holds the mode for this visit; remembering it
        # across logins is a convenience, so a failure here is not worth an error page.
        pass


@app.before_request
def keep_trip_mode_trip_only():
    if (
        session.get("user_id")
        and session.get("mode") == "trip"
        and request.endpoint
        and request.endpoint not in TRIP_MODE_ENDPOINTS
    ):
        return redirect(url_for("trip_home"))


@app.context_processor
def inject_template_globals():
    """asset_version busts the browser's favicon/logo cache when the image
    changes. app_mode tells every page (mainly the nav) whether the user is
    in Personal or Trip mode."""
    mode = session.get("mode", "personal") if session.get("user_id") else "personal"
    # The anon key is public by design (it ships to every browser); passkey
    # sign-in and enrolment run in the browser and need it.
    name = (session.get("display_name") or "").strip()
    if not name:
        name = (session.get("email") or "").split("@")[0]

    profile_avatar = "avatar-01"
    if session.get("user_id"):
        profile_avatar = session.get("profile_avatar")
        if profile_avatar not in PROFILE_AVATARS:
            profile_avatar = load_profile_avatar(get_user_client(), session["user_id"])
            session["profile_avatar"] = profile_avatar

    return {
        "asset_version": "6",
        "minto_version": MINTO_VERSION,
        "app_mode": mode,
        "user_theme": session.get("theme", "light") if session.get("user_id") else "light",
        "genz_mode": bool(session.get("genz_mode", False)) if session.get("user_id") else False,
        "nav_name": name,
        "profile_avatar": profile_avatar,
        "supabase_url": SUPABASE_URL,
        "supabase_anon_key": SUPABASE_ANON_KEY,
    }


SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]

# Optional server-side Supabase key, used only by the monthly-report cron.
# Never expose this key to the browser.
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
MONTHLY_REPORT_CRON_SECRET = os.environ.get("MONTHLY_REPORT_CRON_SECRET", "")

# SMTP is environment-driven so Minto is not tied to one mail provider.
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USERNAME)

# Credit-card dashboard warning thresholds: plain numbers, not a black box.
CC_SPEND_SHARE_WARNING = float(os.environ.get("CC_SPEND_SHARE_WARNING", "50"))
CC_UTILIZATION_WARNING = float(os.environ.get("CC_UTILIZATION_WARNING", "70"))

# Reports and "this month" follow Indian time, not UTC, so a job that runs just
# after midnight IST on the 1st still reports the month that just ended.
APP_TZ = ZoneInfo("Asia/Kolkata")
MAX_AMOUNT = 100_000_000  # one hundred million rupees: a sanity ceiling

# How often login_required re-checks with Supabase's Auth service that the
# session's user still actually exists. An access token stays valid (passes
# signature checks) until its own expiry no matter what happens to the
# underlying auth.users row — deleting the user doesn't revoke tokens already
# issued to them — so without this check, a browser that was logged in before
# a user gets deleted (e.g. from the Supabase dashboard) would keep working
# for up to an hour with no way to detect it short of an insert failing.
# Lower to 0 to check on every single request instead (safer, adds one extra
# Auth API round trip per page load).
USER_VERIFY_INTERVAL_SECONDS = 300

# The categories every user starts with, for the expense/investment
# sub-category chips on the Add Entry form. Kept short and generic on
# purpose — anyone can add their own on top via /categories/add, stored per
# user in user_categories, and "Other" is always pinned last regardless of
# how many custom ones someone has added.
FIXED_EXPENSE_CATEGORIES = [
    "food", "travel", "bills", "shopping", "health",
    "insurance", "entertainment", "groceries", "rent",
]
FIXED_INVESTMENT_CATEGORIES = [
    "mutual_fund", "stocks", "sip", "fixed_deposit", "recurring_deposit",
    "gold", "ppf_nps", "crypto",
]

TRIP_EXPENSE_CATEGORIES = [
    ("food", "🍛 Food"),
    ("fuel", "⛽ Fuel"),
    ("stay", "🏨 Stay"),
    ("toll", "🛣️ Toll"),
    ("parking", "🅿️ Parking"),
    ("tickets", "🎟️ Tickets"),
    ("transport", "🚕 Transport"),
    ("shopping", "🛍️ Shopping"),
    ("entertainment", "🎉 Entertainment"),
    ("vehicle", "🔧 Vehicle"),
    ("medical", "💊 Medical"),
    ("other", "📦 Other"),
]
TRIP_EXPENSE_CATEGORY_KEYS = {key for key, _ in TRIP_EXPENSE_CATEGORIES}



def get_categories(client, user_id, kind, fixed_list):
    """The chip list for one category kind ('expense' or 'investment'):
    the fixed defaults everyone gets, plus this user's own custom ones from
    user_categories, with 'other' always pinned last as a catch-all."""
    custom = (
        client.table("user_categories")
        .select("name")
        .eq("user_id", user_id)
        .eq("kind", kind)
        .order("name")
        .execute()
        .data
    )
    custom_names = [c["name"] for c in custom]
    return fixed_list + custom_names + ["other"]


def parse_money(raw, *, allow_zero=False, default=None):
    """A finite rupee amount rounded to paise, or None if it isn't usable.
    Rejects blanks (unless a default is given), text, nan/inf, negatives,
    zero (unless allow_zero) and absurdly large numbers."""
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(str(raw).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0 or value > MAX_AMOUNT:
        return None
    if value == 0 and not allow_zero:
        return None
    return round(value, 2)


def parse_iso_date(raw):
    """Parse Minto dates from ISO (YYYY-MM-DD) or UI (DD-MON-YYYY) input."""
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None

    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        pass

    parts = value.upper().split("-")
    if len(parts) != 3 or len(parts[0]) not in (1, 2) or len(parts[1]) != 3 or len(parts[2]) != 4:
        return None

    months = {
        "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
        "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
        "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    }
    month = months.get(parts[1])
    if month is None:
        return None

    try:
        return date(int(parts[2]), month, int(parts[0]))
    except (TypeError, ValueError):
        return None


def insert_entry_and_transaction(client, user_id, entry_text, txn_fields):
    """One entries row plus the transactions row that shares its id. If the
    second insert fails the first is removed, so a failure never leaves an
    orphan entry behind. Returns the new id."""
    entry_row = client.table("entries").insert({
        "user_id": user_id,
        "entry_text": entry_text,
        "mode": "manual",
    }).execute()
    entry_id = entry_row.data[0]["id"]
    try:
        client.table("transactions").insert({
            "id": entry_id,
            "user_id": user_id,
            "currency": "INR",
            **txn_fields,
        }).execute()
    except Exception:
        try:
            client.table("entries").delete().eq("id", entry_id).eq("user_id", user_id).execute()
        except Exception:
            pass
        raise
    return entry_id


def source_ids_for(client):
    """Ids of this user's accounts (RLS already limits the query to them)."""
    rows = client.table("user_sources").select("id").execute().data
    return {r["id"] for r in rows}


ENTRY_DIRECTIONS = ("in", "out")
ENTRY_CATEGORIES = ("expense", "income", "investment", "lending", "transfer")


def get_client() -> Client:
    """A plain (unauthenticated) client — used for signup/login itself."""
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


def get_service_client():
    """Server-only Supabase client for protected Net Worth metadata.
    This key is never sent to the browser. Net Worth password hashes are
    deliberately kept outside user-readable RLS."""
    if not SUPABASE_SERVICE_ROLE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def net_worth_is_unlocked():
    expires_at = session.get("net_worth_unlock_expires")
    try:
        if expires_at and time.time() < float(expires_at):
            return True
    except (TypeError, ValueError):
        pass
    session.pop("net_worth_unlocked", None)
    session.pop("net_worth_unlock_expires", None)
    return False


def unlock_net_worth_session():
    session["net_worth_unlocked"] = True
    session["net_worth_unlock_expires"] = time.time() + 15 * 60


def lock_net_worth_session():
    session.pop("net_worth_unlocked", None)
    session.pop("net_worth_unlock_expires", None)


def net_worth_password_hash_exists():
    service = get_service_client()
    if service is None:
        return False
    try:
        rows = service.table("net_worth_security").select("user_id").eq(
            "user_id", session["user_id"]
        ).limit(1).execute().data
        return bool(rows)
    except Exception:
        app.logger.exception("Could not check Net Worth password configuration")
        return False


def get_net_worth_snapshots(limit=12):
    service = get_service_client()
    if service is None:
        return []
    try:
        return (
            service.table("net_worth_snapshots")
            .select("*")
            .eq("user_id", session["user_id"])
            .order("snapshot_date", desc=True)
            .limit(limit)
            .execute()
            .data
        )
    except Exception:
        app.logger.exception("Could not load Net Worth snapshots")
        return []


def get_net_worth_manual_items():
    service = get_service_client()
    if service is None:
        return []
    try:
        return (
            service.table("net_worth_items")
            .select("*")
            .eq("user_id", session["user_id"])
            .eq("active", True)
            .order("item_type")
            .order("category")
            .order("name")
            .execute()
            .data
        )
    except Exception:
        app.logger.exception("Could not load Net Worth items")
        return []


def compute_net_worth_manual_totals(items):
    assets = sum(
        float(i.get("amount") or 0) for i in items
        if i.get("item_type") == "asset"
    )
    liabilities = sum(
        float(i.get("amount") or 0) for i in items
        if i.get("item_type") == "liability"
    )
    return assets, liabilities


def get_card_cycle_dates(statement_day, today=None):
    """Return the current billing-cycle boundaries and next bill dates.
    Statement day is the day the statement is generated. The active cycle is
    the day after the last statement through today."""
    if not statement_day:
        return None
    today = today or datetime.now(APP_TZ).date()
    statement_day = int(statement_day)

    def stmt_date(year, month):
        return date(
            year,
            month,
            min(statement_day, calendar.monthrange(year, month)[1]),
        )

    current_stmt = stmt_date(today.year, today.month)
    if today >= current_stmt:
        last_stmt = current_stmt
        if today.month == 12:
            next_stmt = stmt_date(today.year + 1, 1)
        else:
            next_stmt = stmt_date(today.year, today.month + 1)
    else:
        if today.month == 1:
            last_stmt = stmt_date(today.year - 1, 12)
        else:
            last_stmt = stmt_date(today.year, today.month - 1)
        next_stmt = current_stmt

    return {
        "last_statement_date": last_stmt,
        "next_statement_date": next_stmt,
        "cycle_start": last_stmt + timedelta(days=1),
    }


def get_active_cc_loans(client, user_id):
    """Active credit-card loan/EMI rows keyed by card/source id."""
    try:
        rows = (
            client.table("credit_card_loans")
            .select("*")
            .eq("user_id", user_id)
            .eq("active", True)
            .order("created_at", desc=True)
            .execute()
            .data
        )
    except Exception:
        app.logger.exception("Could not load credit-card loans")
        return {}

    loans = {}
    for row in rows:
        source_id = row.get("source_id")
        if source_id is not None and int(source_id) not in loans:
            loans[int(source_id)] = row
    return loans


def get_credit_card_forecasts(credit_cards, all_txns, today=None, cc_loans=None):
    """Calculate each card's expected next statement from transactions in the
    active billing cycle. Existing opening/current outstanding is never treated
    as the next bill; only new cycle activity is forecast."""
    today = today or datetime.now(APP_TZ).date()
    by_card = defaultdict(list)
    for txn in all_txns:
        sid = txn.get("source_id")
        if sid is not None:
            by_card[sid].append(txn)

    cc_loans = cc_loans or {}
    forecasts = []
    for card in credit_cards:
        item = dict(card)
        info = get_card_cycle_dates(card.get("statement_day"), today)
        loan = cc_loans.get(int(card["id"]))
        item["cc_loan"] = loan
        item["loan_outstanding"] = float(loan.get("outstanding_amount") or 0) if loan else 0.0
        item["loan_emi"] = min(
            float(loan.get("monthly_emi") or 0),
            item["loan_outstanding"],
        ) if loan else 0.0
        item["cycle_configured"] = bool(
            card.get("billing_cycle_enabled") and card.get("statement_day")
        )
        item["expected_bill"] = item["loan_emi"]
        item["expected_bill_date"] = None
        item["due_date"] = None

        if info and item["cycle_configured"]:
            expected = item["loan_emi"]
            for txn in by_card.get(card["id"], []):
                txn_date = parse_iso_date(txn.get("transaction_date"))
                if not txn_date or txn_date < info["cycle_start"] or txn_date > today:
                    continue
                if txn.get("is_previous_card_bill"):
                    continue
                if txn.get("affects_source_balance") is False:
                    continue
                if txn.get("category") == "transfer":
                    continue
                amount = float(txn.get("amount") or 0)
                if txn.get("direction") == "out":
                    expected += amount
                elif txn.get("direction") == "in":
                    expected -= amount
            item["expected_bill"] = max(round(expected, 2), 0.0)
            item["expected_bill_date"] = info["next_statement_date"]
            due_days = int(card.get("payment_due_days") or 0)
            item["due_date"] = info["next_statement_date"] + timedelta(days=due_days)
            item["cycle_start"] = info["cycle_start"]
            item["last_statement_date"] = info["last_statement_date"]

        forecasts.append(item)

    return forecasts


def get_upcoming_commitments(client, user_id, credit_card_forecasts, horizon_days=31):
    """Return currently expected payments due within the next horizon."""
    today = datetime.now(APP_TZ).date()
    cutoff = today + timedelta(days=horizon_days)
    commitments = []

    for card in credit_card_forecasts:
        due = card.get("due_date")
        amount = float(card.get("expected_bill") or 0)
        if due and amount > 0 and today <= due <= cutoff:
            commitments.append({
                "kind": "credit_card",
                "name": f"{card['name']} card bill",
                "amount": amount,
                "due_date": due,
            })

    # Load the current month first, then the following month so the
    # upcoming-commitments list works across a month boundary.
    current_month = today.replace(day=1)
    next_month = (current_month.replace(day=28) + timedelta(days=4)).replace(day=1)

    for month_anchor in (current_month, next_month):
        try:
            fixed = get_fixed_expenses_for_month(client, user_id, month_anchor.year, month_anchor.month)
        except Exception:
            fixed = []
        for item in fixed:
            due = item.get("due_date")
            if isinstance(due, datetime):
                due = due.date()
            if not due or item.get("paid") or due > cutoff:
                continue
            commitments.append({
                "kind": "investment" if item.get("kind") == "investment" else "fixed",
                "name": item["name"],
                "amount": float(item["amount"] or 0),
                "due_date": due,
            })

    commitments.sort(key=lambda x: (x["due_date"], x["name"]))
    return commitments


def compute_safe_to_spend(savings, commitments):
    liquid = sum(float(s.get("balance") or 0) for s in savings)
    committed = sum(float(x.get("amount") or 0) for x in commitments)
    return max(round(liquid - committed, 2), 0.0)


def refresh_if_needed(client):
    """Proactively swaps the access token for a fresh one via the stored
    refresh token when it's expired or about to be, so the user isn't
    bounced to /login every hour just because Supabase's JWTs are
    short-lived. Runs on a plain (unauthenticated) client — refresh_session
    talks to the Auth API, not postgrest, so it doesn't need the old token
    set first."""
    expires_at = session.get("expires_at")
    refresh_token = session.get("refresh_token")
    if not refresh_token or expires_at is None:
        return
    if datetime.now(timezone.utc).timestamp() < expires_at - 60:
        return  # still valid for at least another minute, nothing to do
    try:
        result = client.auth.refresh_session(refresh_token)
        session["access_token"] = result.session.access_token
        # Supabase rotates refresh tokens on every use — the old one stops
        # working, so this MUST be re-saved or the next refresh will fail.
        session["refresh_token"] = result.session.refresh_token
        session["expires_at"] = result.session.expires_at
    except Exception:
        # Refresh token itself is dead (e.g. expired after long inactivity,
        # or revoked) — nothing left to do but require a real login.
        session.clear()


def get_user_client() -> Client:
    """A client carrying the logged-in user's access token, so Supabase's
    row-level-security policies scope every query to that user automatically.
    Refreshes the token first if it's close to expiring (see refresh_if_needed)."""
    client = get_client()
    refresh_if_needed(client)
    token = session.get("access_token")
    if token:
        client.postgrest.auth(token)
    return client


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    # Ordinary web errors (unknown address, wrong method) used to fall through
    # to `raise e` below and turn into "Internal Server Error". Visitors who
    # aren't logged in get the public info page instead; everyone else gets
    # the normal error page.
    if isinstance(e, HTTPException):
        if (
            not session.get("user_id")
            and e.code in (404, 405)
            and not request.path.startswith("/static/")
        ):
            return redirect(url_for("info"))
        if request.path.startswith("/static/") or (e.code or 500) >= 500 or e.code in (301, 302, 303, 307, 308):
            return e
        # Logged-in users get a page that looks like the rest of Minto
        # instead of the bare browser-default error text.
        return render_template("error.html", code=e.code, title=e.name), e.code

    message = str(e).lower()
    session_is_dead = (
        # Access/refresh token itself is bad or expired.
        ("jwt" in message and ("expired" in message or "invalid" in message))
        # The logged-in user's auth.users row is gone (e.g. deleted from the
        # Supabase dashboard) but their browser still holds an old, technically
        # unexpired access token — inserts then fail with a foreign key
        # violation like 'is not present in table "users"', which isn't a JWT
        # error but means exactly the same thing: this session is no longer valid.
        or ("foreign key" in message and "users" in message)
    )
    if session_is_dead:
        session.clear()
        flash("Your session is no longer valid. Please log in again.")
        return redirect(url_for("login"))
    raise e


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("info"))

        if session.get("theme") not in ("light", "dark"):
            session["theme"] = load_saved_theme(get_user_client(), session["user_id"])
        if "genz_mode" not in session:
            session["genz_mode"] = load_genz_mode(get_user_client(), session["user_id"])

        now = datetime.now(timezone.utc).timestamp()
        last_verified = session.get("verified_at", 0)
        if now - last_verified > USER_VERIFY_INTERVAL_SECONDS:
            client = get_user_client()  # also refreshes the access token if it's stale
            try:
                # Asks Supabase's Auth service directly whether this user
                # still exists, rather than trusting the JWT's own claim to
                # still be valid — this is the actual source of truth that
                # a deleted user can no longer pass.
                client.auth.get_user(session["access_token"])
                session["verified_at"] = now
            except Exception:
                session.clear()
                flash("Your account is no longer valid. Please log in again.")
                return redirect(url_for("login"))

        return view(*args, **kwargs)
    return wrapped


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET" and session.get("user_id"):
        return redirect(home_url())

    if request.method == "POST":
        email = request.form["email"].strip()
        password = request.form["password"]
        client = get_client()
        try:
            result = client.auth.sign_up({"email": email, "password": password})
        except Exception as e:
            flash(f"Signup failed: {e}")
            return render_template("signup.html")

        if result.user is None:
            flash("Signup failed - check the email/password and try again.")
            return render_template("signup.html")

        user_id = result.user.id
        if result.session:
            client.postgrest.auth(result.session.access_token)
        try:
            client.table("profiles").insert({"id": user_id}).execute()
        except Exception:
            pass  # profile row may already exist, or email confirmation is pending

        # Show the onboarding/info page after this user's first successful login.
        session["show_info"] = True
        flash("Account created. Check your email if confirmation is required, then log in.")
        return redirect(url_for("login"))

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    # Never show the login form to someone who's already authenticated —
    # without this, a stale/cached copy of this page could sit alongside a
    # nav bar that still (correctly) shows "Log out", making it look like
    # the two are out of sync.
    if request.method == "GET" and session.get("user_id"):
        return redirect(home_url())

    if request.method == "POST":
        email = request.form["email"].strip()
        password = request.form["password"]
        client = get_client()
        try:
            result = client.auth.sign_in_with_password({"email": email, "password": password})
        except Exception as e:
            flash(f"Login failed: {e}")
            return render_template("login.html")

        session.permanent = True  # survive browser restarts, not just the tab
        session["user_id"] = result.user.id
        session["email"] = result.user.email
        session["access_token"] = result.session.access_token
        session["refresh_token"] = result.session.refresh_token
        session["expires_at"] = result.session.expires_at

        # Resume whichever mode they were last in (Trip mode survives a re-login).
        user_client = get_user_client()
        session["mode"] = load_saved_mode(user_client, result.user.id)
        session["theme"] = load_saved_theme(user_client, result.user.id)
        session["genz_mode"] = load_genz_mode(user_client, result.user.id)
        session["display_name"] = load_display_name(user_client, result.user.id)
        session["biometric_enabled"] = load_biometric_flag(user_client, result.user.id)

        # New users see Minto's short introduction once before entering the app.
        if session.pop("show_info", False):
            return redirect(url_for("info"))

        return redirect(home_url())

    return render_template("login.html")


def _jwt_exp(token):
    """The expiry baked into an access token (unix seconds)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return int(time.time()) + 3300


@app.route("/auth/passkey-login", methods=["POST"])
def passkey_login():
    """Finishes a Face ID / fingerprint sign-in. The browser runs the passkey
    ceremony with Supabase Auth, then hands us the session it got back. We
    don't trust it blindly: Supabase is asked whether the token is genuine
    before a Flask session is created for that user."""
    data = request.get_json(silent=True) or {}
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    if not access_token or not refresh_token:
        return jsonify(error="Missing session."), 400

    try:
        user = get_client().auth.get_user(access_token).user
    except Exception:
        return jsonify(error="Could not verify that sign-in."), 401
    if not user:
        return jsonify(error="Could not verify that sign-in."), 401

    session.clear()
    session.permanent = True
    session["user_id"] = user.id
    session["email"] = user.email
    session["access_token"] = access_token
    session["refresh_token"] = refresh_token
    session["expires_at"] = _jwt_exp(access_token)
    session["verified_at"] = time.time()
    user_client = get_user_client()
    session["mode"] = load_saved_mode(user_client, user.id)
    session["theme"] = load_saved_theme(user_client, user.id)
    session["display_name"] = load_display_name(user_client, user.id)
    session["biometric_enabled"] = load_biometric_flag(user_client, user.id)
    return jsonify(ok=True, redirect=home_url())


@app.route("/auth/passkey-flag", methods=["POST"])
@login_required
def passkey_flag():
    """Remembers whether biometric login is on for this account so the page
    can show the right option immediately. The passkeys themselves live in
    Supabase Auth; the browser lists and removes them (the Python client has
    no passkey API) and then reports the result here."""
    enabled = bool((request.get_json(silent=True) or {}).get("enabled"))
    try:
        get_user_client().table("profiles").upsert(
            {"id": session["user_id"], "biometric_enabled": enabled}
        ).execute()
    except Exception:
        return jsonify(ok=False), 200  # column not added yet: the page still works
    session["biometric_enabled"] = enabled
    return jsonify(ok=True)


@app.route("/auth/passkey-token", methods=["POST"])
@login_required
def passkey_token():
    """Hands the logged-in user's fresh tokens to the page so it can enrol a
    passkey. The session is refreshed first and the new tokens saved, so the
    page's copy never needs to refresh (which would rotate the refresh token
    and knock this Flask session out)."""
    try:
        result = get_client().auth.refresh_session(session["refresh_token"])
    except Exception:
        session.clear()
        return jsonify(error="Please log in again."), 401
    session["access_token"] = result.session.access_token
    session["refresh_token"] = result.session.refresh_token
    session["expires_at"] = result.session.expires_at
    return jsonify(
        access_token=result.session.access_token,
        refresh_token=result.session.refresh_token,
    )


def _format_member_since(created):
    if not created:
        return None
    if hasattr(created, "strftime"):
        return created.strftime("%d-%b-%Y").upper()
    return str(created)[:10]


def load_friend_center(client, query=""):
    """Load public friend data through RPCs that never return account UUIDs."""
    empty = {"friends": [], "incoming_requests": [], "outgoing_requests": [], "friend_search": []}
    try:
        empty["friends"] = client.rpc("list_minto_friends").execute().data or []
        for person in empty["friends"]:
            avatar = person.get("profile_emoji")
            if avatar in LEGACY_PROFILE_EMOJI_MAP:
                avatar = LEGACY_PROFILE_EMOJI_MAP[avatar]
            person["profile_avatar"] = avatar if avatar in PROFILE_AVATARS else "avatar-01"
        try:
            contacts = client.rpc("list_minto_friend_contacts").execute().data or []
            contact_by_username = {item.get("username"): item for item in contacts}
            for person in empty["friends"]:
                contact = contact_by_username.get(person.get("username"), {})
                person["phone"] = contact.get("phone")
                person["email"] = contact.get("email")
        except Exception:
            app.logger.exception("Could not load opted-in friend contact details")
        empty["incoming_requests"] = client.rpc(
            "list_minto_friend_requests", {"p_direction": "incoming"}
        ).execute().data or []
        empty["outgoing_requests"] = client.rpc(
            "list_minto_friend_requests", {"p_direction": "outgoing"}
        ).execute().data or []
        for person in empty["incoming_requests"] + empty["outgoing_requests"]:
            avatar = person.get("profile_emoji")
            if avatar in LEGACY_PROFILE_EMOJI_MAP:
                avatar = LEGACY_PROFILE_EMOJI_MAP[avatar]
            person["profile_avatar"] = avatar if avatar in PROFILE_AVATARS else "avatar-01"
        for person in empty["friend_search"]:
            avatar = person.get("profile_emoji")
            if avatar in LEGACY_PROFILE_EMOJI_MAP:
                avatar = LEGACY_PROFILE_EMOJI_MAP[avatar]
            person["profile_avatar"] = avatar if avatar in PROFILE_AVATARS else "avatar-01"
        normalized = (query or "").strip().lower()
        if len(normalized) >= 2:
            empty["friend_search"] = client.rpc(
                "search_minto_users", {"p_query": normalized}
            ).execute().data or []
    except Exception:
        # An unapplied migration should not make Profile or Personal Mode fail.
        app.logger.exception("Could not load the Minto Friends section")
    return empty


@app.route("/friends")
@login_required
def friends():
    client = get_user_client()
    try:
        rows = client.table("profiles").select("username").eq("id", session["user_id"]).limit(1).execute().data
        username = rows[0].get("username") if rows else ""
    except Exception:
        username = ""
    query = (request.args.get("friend_q") or "").strip()
    return render_template(
        "friends.html",
        username=username or "",
        profile_avatars=PROFILE_AVATARS,
        friend_query=query,
        **load_friend_center(client, query),
    )


@app.route("/profile")
@login_required
def profile():
    client = get_user_client()
    user_id = session["user_id"]
    email = session.get("email") or ""
    access_token = session.get("access_token")  # threads can't read the session

    def count(table):
        try:
            query = client.table(table).select("id", count="exact")
            if table != "trips":
                query = query.eq("user_id", user_id)
            return query.limit(1).execute().count
        except Exception:
            return None

    def saved_name():
        try:
            # "*" returns whichever columns exist, so one missing column
            # (say theme) can never hide the others (like the display name).
            rows = client.table("profiles").select("*").eq("id", user_id).execute().data
            return rows[0] if rows else {}
        except Exception:
            return None

    def member_since():
        try:
            return client.auth.get_user(access_token).user.created_at
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=5) as pool:
        accounts_f = pool.submit(count, "user_sources")
        transactions_f = pool.submit(count, "transactions")
        trips_f = pool.submit(count, "trips")
        name_f = pool.submit(saved_name)
        since_f = pool.submit(member_since)
        accounts = accounts_f.result()
        transactions = transactions_f.result()
        trips = trips_f.result()
        profile_data = name_f.result() or {}
        since = since_f.result()

    name = profile_data.get("display_name") or ""
    theme = profile_data.get("theme") if profile_data.get("theme") in ("light", "dark") else session.get("theme", "light")
    profile_avatar = profile_data.get("profile_emoji") if profile_data.get("profile_emoji") in PROFILE_AVATARS else load_profile_avatar(client, user_id)
    biometric_enabled = bool(profile_data.get("biometric_enabled"))
    genz_mode = bool(profile_data.get("genz_mode"))
    try:
        expense_accounts = (
            client.table("user_sources")
            .select("id,name,source_type")
            .eq("active", True)
            .order("name")
            .execute()
            .data
        )
    except Exception:
        expense_accounts = []
    session["display_name"] = name  # keeps the top bar in step
    session["profile_avatar"] = profile_avatar
    session["theme"] = theme
    session["biometric_enabled"] = biometric_enabled
    display_name = name or (email.split("@")[0] if email else "Minto user")
    return render_template(
        "profile.html",
        display_name=display_name,
        has_custom_name=bool(name),
        email=email,
        member_since=_format_member_since(since),
        accounts=accounts,
        transactions=transactions,
        trips=trips,
        theme=theme,
        profile_avatar=profile_avatar,
        profile_avatars=PROFILE_AVATARS,
        biometric_enabled=biometric_enabled,
        monthly_salary=profile_data.get("monthly_salary") or "",
        salary_day=profile_data.get("salary_day") or "",
        reminder_days_before=profile_data.get("reminder_days_before") or 5,
        genz_mode=genz_mode,
        username=profile_data.get("username") or "",
        expense_accounts=expense_accounts,
        default_expense_source_id=profile_data.get("default_expense_source_id"),
        contact_phone=profile_data.get("phone") or "",
        share_phone=bool(profile_data.get("share_phone_with_friends")),
        share_email=bool(profile_data.get("share_email_with_friends")),
    )


@app.route("/profile/contact-sharing", methods=["POST"])
@login_required
def update_profile_contact_sharing():
    phone = (request.form.get("phone") or "").strip()[:32]
    share_phone = request.form.get("share_phone") == "on"
    share_email = request.form.get("share_email") == "on"
    if phone and not re.fullmatch(r"[+0-9() .-]{7,32}", phone):
        flash("Enter a valid phone number or leave it blank.")
        return redirect(url_for("profile"))
    try:
        get_user_client().table("profiles").upsert({
            "id": session["user_id"],
            "phone": phone or None,
            "share_phone_with_friends": share_phone,
            "share_email_with_friends": share_email,
        }).execute()
        flash("Contact sharing preferences saved.")
    except Exception:
        app.logger.exception("Could not save friend contact preferences")
        flash("Couldn't save contact preferences. Run the contact-sharing SQL migration first.")
    return redirect(url_for("profile"))


@app.route("/profile/username", methods=["POST"])
@login_required
def update_profile_username():
    username = (request.form.get("username") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{3,24}", username):
        flash("Usernames must be 3–24 characters using letters, numbers, or underscores.")
        return redirect(url_for("friends"))
    try:
        get_user_client().table("profiles").upsert({
            "id": session["user_id"], "username": username,
        }).select("id").execute()
        flash("Your Minto username is saved.")
    except Exception as exc:
        message = str(exc).lower()
        if "duplicate" in message or "unique" in message:
            flash("That username is already in use. Try another one.")
        else:
            app.logger.exception("Could not save username")
            flash("Couldn't save your username. Please try again.")
    return redirect(url_for("friends"))


@app.route("/profile/default-expense-account", methods=["POST"])
@login_required
def update_default_expense_account():
    user_id = session["user_id"]
    raw_source_id = (request.form.get("source_id") or "").strip()
    source_id = None
    if raw_source_id:
        try:
            source_id = int(raw_source_id)
        except ValueError:
            flash("Choose a valid account.")
            return redirect(url_for("profile"))
        owned = get_user_client().table("user_sources").select("id").eq("id", source_id).eq("active", True).execute().data
        if not owned:
            flash("Choose one of your active accounts.")
            return redirect(url_for("profile"))
    try:
        get_user_client().table("profiles").upsert({
            "id": user_id, "default_expense_source_id": source_id,
        }).execute()
        flash("Default expense account updated." if source_id else "Default expense account cleared.")
    except Exception:
        app.logger.exception("Could not save default expense account")
        flash("Couldn't save that account preference. Run the Minto 2.0 migration first.")
    return redirect(url_for("profile"))


@app.route("/friends/requests", methods=["POST"])
@login_required
def send_friend_request():
    username = (request.form.get("username") or "").strip().lower()
    try:
        result = get_user_client().rpc("send_friend_request", {"p_username": username}).execute().data
        state = result[0] if isinstance(result, list) and result else result
        messages = {
            "sent": "Friend request sent.",
            "pending": "You already have a request waiting for this person.",
            "incoming": "They already sent you a request. Accept it below.",
            "friend": "You are already Minto friends.",
        }
        flash(messages.get(state, "Friend request updated."))
    except Exception as exc:
        message = str(exc).lower()
        if "no minto user" in message:
            flash("No Minto user found. Check the username and try again.")
        elif "yourself" in message:
            flash("You cannot add yourself as a friend.")
        else:
            app.logger.exception("Could not send Minto friend request")
            flash("Couldn't send that request. Check the username and try again.")
    return redirect(url_for("friends"))


@app.route("/friends/requests/<int:request_id>", methods=["POST"])
@login_required
def respond_friend_request(request_id):
    action = (request.form.get("action") or "").strip().lower()
    if action not in ("accept", "reject"):
        flash("Choose accept or reject.")
    else:
        try:
            get_user_client().rpc("respond_friend_request", {
                "p_request_id": request_id, "p_action": action,
            }).execute()
            flash("Friend request accepted." if action == "accept" else "Friend request rejected.")
        except Exception:
            app.logger.exception("Could not respond to Minto friend request")
            flash("Couldn't update that request. Refresh Friends and try again.")
    return redirect(url_for("friends"))


@app.route("/friends/remove", methods=["POST"])
@login_required
def remove_minto_friend():
    username = (request.form.get("username") or "").strip().lower()
    try:
        result = get_user_client().rpc("remove_minto_friend", {"p_username": username}).execute().data
        removed = result[0] if isinstance(result, list) and result else result
        flash("Friend removed. Shared trip history remains intact." if removed else "That friend was not found.")
    except Exception:
        app.logger.exception("Could not remove Minto friend")
        flash("Couldn't remove that friend. Please try again.")
    return redirect(url_for("friends"))


@app.route("/profile/genz-mode", methods=["POST"])
@login_required
def update_profile_genz_mode():
    wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    enabled = request.form.get("enabled", "").strip().lower() in ("1", "true", "on", "yes")
    saved = save_genz_mode(get_user_client(), session["user_id"], enabled)
    if saved:
        session["genz_mode"] = enabled
    if wants_json:
        return jsonify(ok=saved, enabled=enabled), (200 if saved else 500)
    flash("Gen Z mode " + ("on." if enabled else "off."))
    return redirect(url_for("profile"))


@app.route("/profile/emoji", methods=["POST"])
@login_required
def update_profile_emoji():
    avatar = request.form.get("profile_avatar", "").strip()
    if save_profile_avatar(get_user_client(), session["user_id"], avatar):
        session["profile_avatar"] = avatar
        flash("Profile avatar updated.")
    else:
        flash("Please choose a valid profile avatar.")
    return redirect(url_for("profile"))


@app.route("/profile/money-plan", methods=["POST"])
@login_required
def update_money_plan():
    client = get_user_client()
    user_id = session["user_id"]

    salary = parse_money(request.form.get("monthly_salary"))
    try:
        salary_day = int(request.form.get("salary_day", "").strip()) if request.form.get("salary_day", "").strip() else None
        reminder_days = int(request.form.get("reminder_days_before", "5").strip())
    except ValueError:
        salary_day, reminder_days = None, 0

    if salary is None:
        flash("Enter your monthly salary.")
    elif salary_day is None or not 1 <= salary_day <= 31:
        flash("Choose a salary day between 1 and 31.")
    elif not 1 <= reminder_days <= 30:
        flash("Reminder days must be between 1 and 30.")
    else:
        try:
            client.table("profiles").upsert({
                "id": user_id,
                "monthly_salary": salary,
                "salary_day": salary_day,
                "reminder_days_before": reminder_days,
            }).execute()
            flash("Money plan settings updated.")
        except Exception:
            app.logger.exception("Could not save money plan")
            flash("Couldn't save your money plan settings. Please run the latest database update first.")
    return redirect(url_for("profile"))


@app.route("/profile/theme", methods=["POST"])
@login_required
def update_profile_theme():
    # The profile page's toggle calls this with fetch(). It gets a plain
    # status code back; a flash message would otherwise sit in the session and
    # pop up on whatever page is opened next.
    wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    theme = request.form.get("theme", "light").strip().lower()
    if theme not in ("light", "dark"):
        if wants_json:
            return jsonify(ok=False), 400
        flash("Invalid theme selection.")
        return redirect(url_for("profile"))

    saved = save_theme(get_user_client(), session["user_id"], theme)
    if saved:
        session["theme"] = theme
    if wants_json:
        return jsonify(ok=saved), (200 if saved else 500)
    flash("Appearance updated." if saved else "Couldn't save your appearance preference. Please try again.")
    return redirect(url_for("profile"))


@app.route("/profile/name", methods=["POST"])
@login_required
def update_profile_name():
    name = " ".join(request.form.get("display_name", "").split())[:40]
    client = get_user_client()
    try:
        client.table("profiles").upsert(
            {"id": session["user_id"], "display_name": name or None}
        ).execute()
        session["display_name"] = name
        flash("Name updated.")
    except Exception:
        flash("Couldn't save your name. Please try again.")
    return redirect(url_for("profile"))


# ---------------------------------------------------------------------------
# Reports: the file is built on the fly and sent straight to the user's device.
# Only a small history row (range, row count, time) is saved, never the report.
# ---------------------------------------------------------------------------

def _parse_report_dates(args):
    try:
        d_from = date.fromisoformat(args.get("from", ""))
        d_to = date.fromisoformat(args.get("to", ""))
    except ValueError:
        return None, None, "Pick a start date and an end date."
    if d_from > d_to:
        return None, None, "The start date must be on or before the end date."
    return d_from, d_to, None


def _fetch_report_history(client, user_id, offset=0, limit=10):
    """Fetch report history in small pages, newest first."""
    try:
        offset = max(int(offset or 0), 0)
        limit = min(max(int(limit or 10), 1), 10)
        rows = (
            client.table("report_history")
            .select("id, date_from, date_to, row_count, file_format, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .range(offset, offset + limit)
            .execute()
            .data
        )
        # One extra row lets us tell the UI whether another page exists.
        return {
            "items": rows[:limit],
            "has_more": len(rows) > limit,
        }
    except Exception:
        return {"items": [], "has_more": False}



@app.route("/planner")
@login_required
def planner():
    """Current-month planning view built from salary, unpaid commitments and recorded spending."""
    client = get_user_client()
    user_id = session["user_id"]
    today = datetime.now(APP_TZ).date()
    month_start = today.replace(day=1)
    month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    profile_rows = (
        client.table("profiles")
        .select("monthly_salary, salary_day")
        .eq("id", user_id)
        .limit(1)
        .execute()
        .data
    )
    profile_settings = profile_rows[0] if profile_rows else {}
    salary_cycle = get_salary_cycle(profile_settings, today)

    fixed_items = get_fixed_expenses_for_month(client, user_id, today.year, today.month)
    unpaid_fixed = [
        item for item in fixed_items
        if not item.get("paid") and item.get("amount")
    ]
    unpaid_fixed.sort(key=lambda x: (x.get("due_date") or month_end, x.get("name") or ""))

    savings, credit_cards, all_txns = compute_source_balances(client, user_id)
    cc_loans = get_active_cc_loans(client, user_id)
    card_forecasts = get_credit_card_forecasts(credit_cards, all_txns, today, cc_loans)
    expected_bills = [
        {
            "kind": "credit_card",
            "name": f"{card['name']} card bill",
            "amount": float(card.get("expected_bill") or 0),
            "due_date": card.get("due_date"),
        }
        for card in card_forecasts
        if card.get("due_date")
        and card["due_date"].year == today.year
        and card["due_date"].month == today.month
        and float(card.get("expected_bill") or 0) > 0
    ]

    commitments = [
        {
            "kind": "investment" if item.get("kind") == "investment" else "fixed",
            "name": item["name"],
            "amount": float(item["amount"] or 0),
            "due_date": item.get("due_date"),
        }
        for item in unpaid_fixed
    ] + expected_bills
    commitments.sort(key=lambda x: (x.get("due_date") or month_end, x.get("name") or ""))

    commitment_total = round(sum(item["amount"] for item in commitments), 2)

    txns = (
        client.table("transactions")
        .select("direction, category, amount, transaction_date, expense_category")
        .eq("user_id", user_id)
        .gte("transaction_date", month_start.isoformat())
        .lte("transaction_date", today.isoformat())
        .order("transaction_date", desc=True)
        .execute()
        .data
    )
    excluded_categories = {"transfer", "lending", "trip_expense_payment", "trip_settlement"}
    spent_so_far = round(sum(
        float(txn.get("amount") or 0)
        for txn in txns
        if txn.get("direction") == "out"
        and txn.get("category") not in excluded_categories
        and txn.get("amount")
    ), 2)

    planned_salary = round(float(profile_settings.get("monthly_salary") or 0), 2)
    after_commitments = round(planned_salary - commitment_total, 2)
    remaining_plan = round(after_commitments - spent_so_far, 2)

    days_left = max((month_end - today).days + 1, 1)
    weeks_left = max(days_left / 7, 1)
    daily_plan = round(max(remaining_plan, 0) / days_left, 2)
    weekly_plan = round(max(remaining_plan, 0) / weeks_left, 2)

    due_items = []
    for item in commitments:
        due = item.get("due_date")
        days_until = (due - today).days if due else None
        due_items.append({
            **item,
            "days_until": days_until,
            "due_label": "Today" if days_until == 0 else (
                "Tomorrow" if days_until == 1 else (
                    f"In {days_until} days" if days_until is not None and days_until > 1 else "Due"
                )
            ),
        })

    return render_template(
        "planner.html",
        today=today,
        month_label=today.strftime("%B %Y"),
        month_start=month_start,
        month_end=month_end,
        salary_cycle=salary_cycle,
        planned_salary=planned_salary,
        commitment_total=commitment_total,
        after_commitments=after_commitments,
        spent_so_far=spent_so_far,
        remaining_plan=remaining_plan,
        days_left=days_left,
        daily_plan=daily_plan,
        weekly_plan=weekly_plan,
        commitments=due_items,
    )


@app.route("/reports")
@login_required
def reports():
    client = get_user_client()
    today = datetime.now(APP_TZ).date()
    return render_template(
        "reports.html",
        default_from=today.replace(day=1).isoformat(),
        default_to=today.isoformat(),
        history=_fetch_report_history(client, session["user_id"], offset=0, limit=10),
    )


@app.route("/reports/history")
@login_required
def reports_history():
    client = get_user_client()
    try:
        offset = max(int(request.args.get("offset", 0)), 0)
    except (TypeError, ValueError):
        offset = 0
    return jsonify(_fetch_report_history(client, session["user_id"], offset=offset, limit=10))


def _fetch_transactions(client, user_id, d_from, d_to, columns):
    """Every transaction in the range. Supabase returns at most 1000 rows per
    request, so this pages through them."""
    rows, start = [], 0
    while True:
        batch = (
            client.table("transactions")
            .select(columns)
            .eq("user_id", user_id)
            .gte("transaction_date", d_from.isoformat())
            .lte("transaction_date", d_to.isoformat())
            .order("transaction_date")
            .order("id")
            .range(start, start + 999)
            .execute()
            .data
        )
        rows.extend(batch)
        if len(batch) < 1000:
            return rows
        start += 1000


@app.route("/transactions/download.csv")
@login_required
def download_transactions_csv():
    d_from, d_to, error = _parse_report_dates(request.args)
    if error:
        flash(error)
        return redirect(url_for("dashboard"))

    direction = request.args.get("direction", "").strip()
    category = request.args.get("category", "").strip()
    if direction not in ("", "in", "out"):
        direction = ""
    allowed_categories = {"income", "expense", "investment", "transfer", "lending"}
    if category not in allowed_categories:
        category = ""

    client = get_user_client()
    user_id = session["user_id"]
    rows = _fetch_transactions(
        client, user_id, d_from, d_to,
        "id, transaction_date, direction, category, expense_category, investment_category, amount, currency, description, raw_text, source_id, user_sources(name, source_type)",
    )
    if direction:
        rows = [row for row in rows if row.get("direction") == direction]
    if category:
        rows = [row for row in rows if row.get("category") == category]

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "Transaction ID", "Date", "Direction", "Type", "Category",
        "Description", "Amount", "Currency", "Account", "Account Type",
    ])
    for row in rows:
        source = row.get("user_sources") or {}
        writer.writerow([
            row.get("id", ""),
            row.get("transaction_date", ""),
            row.get("direction", ""),
            row.get("category", ""),
            row.get("expense_category") or row.get("investment_category") or "",
            row.get("description") or row.get("raw_text") or "",
            row.get("amount", ""),
            row.get("currency") or "INR",
            source.get("name", ""),
            source.get("source_type", ""),
        ])

    filename = f"minto-transactions-{d_from.isoformat()}-to-{d_to.isoformat()}.csv"
    return Response(
        "\ufeff" + output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.route("/reports/download")
@login_required
def download_report():
    d_from, d_to, error = _parse_report_dates(request.args)
    if error:
        flash(error)
        return redirect(url_for("reports"))

    client = get_user_client()
    user_id = session["user_id"]

    rows = _fetch_transactions(
        client, user_id, d_from, d_to,
        "*, user_sources(name, source_type)",
    )
    if not rows:
        flash("No transactions between those dates.")
        return redirect(url_for("reports"))

    # The period right before this one (same length), for the "vs previous" stat.
    span = (d_to - d_from).days + 1
    prev_to = d_from - timedelta(days=1)
    prev_from = prev_to - timedelta(days=span - 1)
    try:
        prev_rows = _fetch_transactions(client, user_id, prev_from, prev_to, "amount, category")
        prev_spend = sum(float(r["amount"] or 0) for r in prev_rows if r.get("category") == "expense")
    except Exception:
        prev_spend = None

    name = (session.get("display_name") or "").strip() or (session.get("email") or "").split("@")[0] or "Minto user"
    try:
        pdf = build_report_pdf(name, d_from, d_to, rows, prev_spend)
    except Exception as e:
        app.logger.exception("Report PDF failed")
        flash("Couldn't build the report. Please try again.")
        return redirect(url_for("reports"))

    # Log that a report was made (range and size only, never the report itself).
    # Never blocks the download.
    try:
        client.table("report_history").insert({
            "user_id": user_id,
            "date_from": d_from.isoformat(),
            "date_to": d_to.isoformat(),
            "row_count": len(rows),
            "file_format": "pdf",
        }).execute()
    except Exception:
        pass

    filename = f"minto-report-{d_from.isoformat()}-to-{d_to.isoformat()}.pdf"
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# Monthly report email. A scheduler (for example a Railway cron) calls
# POST /internal/monthly-reports with the secret header once a month.
# ---------------------------------------------------------------------------

def _admin_client():
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is not configured.")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def _send_report_email(to_email, user_name, report_month, pdf_bytes):
    if not all((SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM)):
        raise RuntimeError("SMTP settings are not configured.")

    label = report_month.strftime("%B %Y")
    message = EmailMessage()
    message["Subject"] = f"Your Minto report for {label}"
    message["From"] = SMTP_FROM
    message["To"] = to_email
    message.set_content(
        f"Hi {user_name},\n\n"
        f"Attached is your Minto spending report for {label}.\n\n"
        "It includes your average daily spend, category-wise spending, "
        "spend by account and a few useful highlights.\n\n"
        "Minto"
    )
    message.add_attachment(
        pdf_bytes,
        maintype="application",
        subtype="pdf",
        filename=f"minto-report-{report_month.strftime('%Y-%m')}.pdf",
    )

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(message)


def _previous_month_range(today=None):
    """First and last day of the month before `today`, in Indian time."""
    today = today or datetime.now(APP_TZ).date()
    first_this = today.replace(day=1)
    last_previous = first_this - timedelta(days=1)
    return last_previous.replace(day=1), last_previous


def send_monthly_reports(max_sends=100):
    """Email last month's report to every confirmed user who has activity.

    Safe to call repeatedly: a row in monthly_report_sends is claimed *before*
    the email goes out (the unique rule means only one caller can claim it),
    and released again if anything fails so the next run retries. At most
    `max_sends` emails go out per call so one request never runs long; the
    response says when there is more to do."""
    month_from, month_to = _previous_month_range()
    span = (month_to - month_from).days + 1
    prev_to = month_from - timedelta(days=1)
    prev_from = prev_to - timedelta(days=span - 1)
    admin = _admin_client()

    users, page = [], 1
    while True:
        result = admin.auth.admin.list_users(page=page, per_page=1000)
        batch = result.users if hasattr(result, "users") else getattr(result, "data", result)
        batch = batch or []
        users.extend(batch)
        if len(batch) < 1000:
            break
        page += 1

    sent = skipped = failed = 0
    more = False
    for user in users:
        email = getattr(user, "email", None)
        if not email or not getattr(user, "email_confirmed_at", None):
            skipped += 1
            continue
        user_id = str(getattr(user, "id", ""))
        claimed = False
        try:
            already = admin.table("monthly_report_sends").select("id").eq(
                "user_id", user_id
            ).eq("report_month", month_from.isoformat()).limit(1).execute().data
            if already:
                skipped += 1
                continue

            rows = _fetch_transactions(
                admin, user_id, month_from, month_to, "*, user_sources(name, source_type)"
            )
            if not rows:
                skipped += 1
                continue

            if sent >= max_sends:
                more = True
                break

            # Claim first. A duplicate-key error here means another run got there first.
            admin.table("monthly_report_sends").insert({
                "user_id": user_id,
                "report_month": month_from.isoformat(),
            }).execute()
            claimed = True

            # The name chosen on the Profile page lives in profiles.display_name.
            profile = admin.table("profiles").select("display_name").eq("id", user_id).limit(1).execute().data
            name = ((profile[0].get("display_name") if profile else None)
                    or (getattr(user, "user_metadata", {}) or {}).get("display_name")
                    or email.split("@")[0])

            prev_rows = _fetch_transactions(admin, user_id, prev_from, prev_to, "amount, category")
            prev_spend = sum(float(r["amount"] or 0) for r in prev_rows if r.get("category") == "expense")

            pdf = build_report_pdf(name, month_from, month_to, rows, prev_spend)
            _send_report_email(email, name, month_from, pdf)
            sent += 1
        except Exception as exc:
            if claimed:
                try:
                    admin.table("monthly_report_sends").delete().eq(
                        "user_id", user_id
                    ).eq("report_month", month_from.isoformat()).execute()
                except Exception:
                    app.logger.exception("Could not release report claim for %s", user_id)
            if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
                skipped += 1
            else:
                failed += 1
                app.logger.exception("Monthly report failed for user %s", user_id)

    return {
        "sent": sent, "skipped": skipped, "failed": failed, "more": more,
        "report_month": month_from.isoformat(),
    }


@app.route("/internal/monthly-reports", methods=["POST"])
def monthly_reports_cron():
    """Cron endpoint. Protect with a long random secret; never expose the service key."""
    if not MONTHLY_REPORT_CRON_SECRET:
        return jsonify(error="Cron endpoint is not configured."), 503

    supplied = request.headers.get("X-Minto-Cron-Secret", "")
    # Compare as bytes: comparing non-ASCII text would raise instead of failing.
    if not secrets.compare_digest(supplied.encode("utf-8"), MONTHLY_REPORT_CRON_SECRET.encode("utf-8")):
        return jsonify(error="Unauthorized."), 401

    try:
        return jsonify(send_monthly_reports())
    except Exception:
        app.logger.exception("Monthly report run failed")
        return jsonify(error="Monthly report run failed. See the server logs."), 500


@app.route("/info")
def info():
    return render_template("info.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/net-worth", methods=["GET", "POST"])
@login_required
def net_worth():
    client = get_user_client()
    user_id = session["user_id"]
    service = get_service_client()

    if service is None:
        flash("Net Worth protection needs SUPABASE_SERVICE_ROLE_KEY on the server.")
        return redirect(url_for("dashboard"))

    password_configured = net_worth_password_hash_exists()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "setup-password":
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")
            if len(password) < 8:
                flash("Use a Net Worth password with at least 8 characters.")
            elif password != confirm:
                flash("The passwords do not match.")
            else:
                try:
                    service.table("net_worth_security").upsert({
                        "user_id": user_id,
                        "password_hash": generate_password_hash(password),
                    }).execute()
                    unlock_net_worth_session()
                    flash("Net Worth password created.")
                except Exception:
                    app.logger.exception("Could not save Net Worth password")
                    flash("Couldn't create the Net Worth password. Please run the database update first.")
            return redirect(url_for("net_worth"))

        if action == "unlock":
            password = request.form.get("password", "")
            try:
                rows = service.table("net_worth_security").select(
                    "password_hash"
                ).eq("user_id", user_id).limit(1).execute().data
                password_hash = rows[0]["password_hash"] if rows else None
                if password_hash and check_password_hash(password_hash, password):
                    unlock_net_worth_session()
                    flash("Net Worth unlocked for 15 minutes.")
                else:
                    flash("Incorrect Net Worth password.")
            except Exception:
                app.logger.exception("Could not verify Net Worth password")
                flash("Couldn't unlock Net Worth. Please try again.")
            return redirect(url_for("net_worth"))

        if action == "lock":
            lock_net_worth_session()
            return redirect(url_for("net_worth"))

        if not net_worth_is_unlocked():
            flash("Unlock Net Worth before changing its data.")
            return redirect(url_for("net_worth"))

        if action == "change-password":
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm = request.form.get("confirm_password", "")
            try:
                rows = service.table("net_worth_security").select(
                    "password_hash"
                ).eq("user_id", user_id).limit(1).execute().data
                stored_hash = rows[0]["password_hash"] if rows else None
                if not stored_hash or not check_password_hash(stored_hash, current_password):
                    flash("Current Net Worth password is incorrect.")
                elif len(new_password) < 8:
                    flash("Use a new Net Worth password with at least 8 characters.")
                elif new_password != confirm:
                    flash("The new passwords do not match.")
                else:
                    service.table("net_worth_security").update({
                        "password_hash": generate_password_hash(new_password),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }).eq("user_id", user_id).execute()
                    unlock_net_worth_session()
                    flash("Net Worth password changed.")
            except Exception:
                app.logger.exception("Could not change Net Worth password")
                flash("Couldn't change the Net Worth password.")
            return redirect(url_for("net_worth"))

        if action == "add-item":
            name = " ".join(request.form.get("name", "").split())[:80]
            item_type = request.form.get("item_type")
            category = request.form.get("category")
            amount = parse_money(request.form.get("amount"), allow_zero=True)
            item_date = parse_iso_date(request.form.get("as_of_date")) or datetime.now(APP_TZ).date()
            notes = (request.form.get("notes") or "").strip()[:300] or None
            allowed_categories = {
                "asset": {
                    "cash", "investment", "gold", "vehicle", "property",
                    "fixed_deposit", "recurring_deposit", "ppf_nps",
                    "loan_receivable", "other",
                },
                "liability": {"loan_payable", "other"},
            }
            if (
                not name
                or item_type not in allowed_categories
                or category not in allowed_categories[item_type]
                or amount is None
                or amount < 0
            ):
                flash("Check the Net Worth item details.")
            else:
                try:
                    service.table("net_worth_items").insert({
                        "user_id": user_id,
                        "name": name,
                        "item_type": item_type,
                        "category": category,
                        "amount": amount,
                        "as_of_date": item_date.isoformat(),
                        "notes": notes,
                    }).execute()
                    flash(f"{name} added to Net Worth.")
                except Exception:
                    app.logger.exception("Could not add Net Worth item")
                    flash("Couldn't add that item. Please run the database update first.")
            return redirect(url_for("net_worth"))

        if action == "save-snapshot":
            try:
                savings, credit_cards, all_txns = compute_source_balances(client, user_id)
                items = get_net_worth_manual_items()
                snapshot_wealth = compute_net_worth(
                    all_txns, savings, credit_cards, items, get_trip_payables(client, user_id)
                )
                snapshot_date = parse_iso_date(request.form.get("snapshot_date")) or datetime.now(APP_TZ).date()
                service.table("net_worth_snapshots").upsert({
                    "user_id": user_id,
                    "snapshot_date": snapshot_date.isoformat(),
                    "net_worth": snapshot_wealth["net_worth"],
                    "notes": (request.form.get("snapshot_notes") or "").strip()[:300] or None,
                }).execute()
                flash(f"Net Worth snapshot saved for {snapshot_date.strftime('%d-%b-%Y').upper()}.")
            except Exception:
                app.logger.exception("Could not save Net Worth snapshot")
                flash("Couldn't save the snapshot. Please run the database update first.")
            return redirect(url_for("net_worth"))

        if action == "delete-item":
            item_id = request.form.get("item_id")
            if item_id and item_id.isdigit():
                try:
                    service.table("net_worth_items").delete().eq(
                        "id", int(item_id)
                    ).eq("user_id", user_id).execute()
                    flash("Net Worth item removed.")
                except Exception:
                    flash("Couldn't remove that item.")
            return redirect(url_for("net_worth"))

    unlocked = net_worth_is_unlocked()
    manual_items = get_net_worth_manual_items() if unlocked else []
    savings, credit_cards, all_txns = compute_source_balances(client, user_id)
    trip_payables = get_trip_payables(client, user_id) if unlocked else 0
    wealth = compute_net_worth(all_txns, savings, credit_cards, manual_items, trip_payables) if unlocked else None
    cc_loans = get_active_cc_loans(client, user_id)
    card_forecasts = get_credit_card_forecasts(credit_cards, all_txns, cc_loans=cc_loans)
    snapshots = get_net_worth_snapshots() if unlocked else []
    return render_template(
        "net_worth.html",
        password_configured=password_configured,
        unlocked=unlocked,
        wealth=wealth,
        manual_items=manual_items,
        card_forecasts=card_forecasts,
        snapshots=snapshots,
        today=datetime.now(APP_TZ).date().isoformat(),
    )


@app.route("/net-worth/lock", methods=["POST"])
@login_required
def lock_net_worth():
    lock_net_worth_session()
    return redirect(url_for("dashboard"))


def get_lending_summary(client, user_id):
    """All-time per-person lending ledger (category='lending' only, not
    period-scoped): positive amount = they still owe you, negative = you've
    somehow taken in more than you gave out (rare, but shown honestly rather
    than hidden). Also returns every counterparty name ever used, settled or
    not, so the entry form can offer a pick-list and prevent spelling drift
    (e.g. "Rohan" vs "Rohan K") from splitting one person into two ledgers."""
    rows = (
        client.table("transactions")
        .select("counterparty, amount, direction")
        .eq("user_id", user_id)
        .eq("category", "lending")
        .execute()
        .data
    )
    lent_by_person = defaultdict(float)
    for t in rows:
        if not t.get("amount"):
            continue
        person = t.get("counterparty") or "Unspecified"
        lent_by_person[person] += float(t["amount"]) if t["direction"] == "out" else -float(t["amount"])

    known_names = sorted(lent_by_person.keys())
    outstanding = sorted(
        ({"name": n, "amount": a} for n, a in lent_by_person.items() if round(a, 2) != 0),
        key=lambda x: -x["amount"],
    )
    return known_names, outstanding


@app.route("/", methods=["GET", "POST"])
@login_required
def entry():
    client = get_user_client()
    user_id = session["user_id"]

    if request.method == "POST":
        raw_amount = request.form.get("amount")
        direction = request.form.get("direction")
        category = request.form.get("category")
        expense_category = request.form.get("expense_category") or None
        investment_category = request.form.get("investment_category") or None
        counterparty = (request.form.get("counterparty") or "").strip() or None
        source_id = request.form.get("source_id") or None
        source_type_filter = request.form.get("source_type_filter")
        notes = (request.form.get("notes") or "").strip() or None
        raw_date = request.form.get("transaction_date") or None

        amount = parse_money(raw_amount)
        problem = None
        if amount is None:
            problem = "Enter an amount greater than zero."
        elif direction not in ENTRY_DIRECTIONS or category not in ENTRY_CATEGORIES:
            problem = "Choose In or Out and a transaction type."
        elif category == "expense" and not expense_category:
            problem = "Choose an expense category."
        elif category == "expense" and expense_category not in get_categories(client, user_id, "expense", FIXED_EXPENSE_CATEGORIES):
            problem = "Choose a valid expense category."
        elif category == "investment" and not investment_category:
            problem = "Choose an investment category."
        elif category == "investment" and investment_category not in get_categories(client, user_id, "investment", FIXED_INVESTMENT_CATEGORIES):
            problem = "Choose a valid investment category."
        elif category == "lending" and not counterparty:
            problem = "Add who the money was lent to or came back from."
        elif source_type_filter not in ("savings", "credit_card"):
            problem = "Choose whether this entry uses cash/bank or a card."
        elif raw_date and parse_iso_date(raw_date) is None:
            problem = "That date doesn't look right."
        elif category in ("expense", "income", "investment", "lending", "transfer") and not source_id:
            problem = "Choose a cash/bank account or card."
        elif source_id:
            try:
                selected_source_id = int(source_id)
                source_rows = (
                    client.table("user_sources")
                    .select("id, source_type")
                    .eq("user_id", user_id)
                    .eq("active", True)
                    .eq("id", selected_source_id)
                    .limit(1)
                    .execute()
                    .data
                )
                if not source_rows:
                    problem = "Pick one of your active accounts."
                elif (
                    (source_type_filter == "savings" and source_rows[0]["source_type"] not in ("savings", "cash"))
                    or
                    (source_type_filter == "credit_card" and source_rows[0]["source_type"] != "credit_card")
                ):
                    problem = "Choose an account matching the selected source type."
            except (TypeError, ValueError):
                problem = "Pick one of your accounts."
        if problem:
            flash(problem)
            return redirect(url_for("entry"))

        transaction_row = {
            "direction": direction,
            "category": category,
            "expense_category": expense_category if category == "expense" else None,
            "investment_category": investment_category if category == "investment" else None,
            "counterparty": counterparty if category == "lending" else None,
            "source_id": int(source_id) if source_id else None,
            "amount": amount,
            "description": notes,
            "raw_text": notes,
        }
        if raw_date:
            transaction_row["transaction_date"] = raw_date
        # else: omitted entirely so the column's own DB default (today) applies —
        # explicitly sending null here would fail the not-null constraint.
        try:
            insert_entry_and_transaction(
                client, user_id, notes or f"{direction} {amount} {category}", transaction_row
            )
        except Exception:
            flash("Couldn't save that entry. Please try again.")
            return redirect(url_for("entry"))

        flash("Saved.")
        return redirect(url_for("entry"))

    sources = (
        client.table("user_sources")
        .select("*")
        .eq("active", True)
        .order("name")
        .execute()
        .data
    )
    savings_sources = [s for s in sources if s["source_type"] in ("savings", "cash")]
    cc_sources = [s for s in sources if s["source_type"] == "credit_card"]
    known_counterparties, outstanding_loans = get_lending_summary(client, user_id)
    expense_categories = get_categories(client, user_id, "expense", FIXED_EXPENSE_CATEGORIES)
    investment_categories = get_categories(client, user_id, "investment", FIXED_INVESTMENT_CATEGORIES)
    return render_template(
        "entry.html",
        savings_sources=savings_sources,
        cc_sources=cc_sources,
        known_counterparties=known_counterparties,
        outstanding_loans=outstanding_loans,
        expense_categories=expense_categories,
        investment_categories=investment_categories,
        today=datetime.now(APP_TZ).date().isoformat(),
    )


@app.route("/pay-cc-bill", methods=["GET", "POST"])
@login_required
def pay_cc_bill():
    """Record a card payment as linked bank/cash -> card transfer legs.
    Optional CC Loan / EMI allocation reduces the separate loan balance while
    the transfer still reduces the card's actual outstanding amount."""
    client = get_user_client()
    user_id = session["user_id"]

    sources = (
        client.table("user_sources")
        .select("*")
        .eq("active", True)
        .order("name")
        .execute()
        .data
    )
    savings_sources = [s for s in sources if s["source_type"] in ("savings", "cash")]
    cc_sources = [s for s in sources if s["source_type"] == "credit_card"]
    cc_loans = get_active_cc_loans(client, user_id)

    # First card payment asks whether it is for spending before Minto started.
    ask_previous_bill = not load_credit_card_setup_completed(client, user_id)

    def render_form():
        return render_template(
            "pay_cc_bill.html",
            savings_sources=savings_sources,
            cc_sources=cc_sources,
            cc_loans=cc_loans,
            ask_previous_bill=ask_previous_bill,
            form=request.form,
        )

    if request.method == "POST":
        amount = parse_money(request.form.get("amount"))
        from_source_id = request.form.get("from_source_id")
        to_source_id = request.form.get("to_source_id")
        notes = (request.form.get("notes") or "").strip() or None
        previous_card_bill = request.form.get("previous_card_bill")
        payment_type = request.form.get("payment_type") or "regular"

        pay_from_ids = {str(s["id"]) for s in savings_sources}
        card_ids = {str(s["id"]) for s in cc_sources}

        if amount is None:
            flash("Enter an amount greater than zero.")
            return render_form()
        if from_source_id not in pay_from_ids or to_source_id not in card_ids:
            flash("Pick an account to pay from and a card to pay off.")
            return render_form()
        if payment_type not in ("regular", "loan"):
            flash("Choose regular card bill or CC loan / EMI.")
            return render_form()
        if ask_previous_bill and previous_card_bill not in ("yes", "no"):
            flash("Say whether this bill is for spending from before you started using Minto.")
            return render_form()

        selected_loan = cc_loans.get(int(to_source_id)) if payment_type == "loan" else None
        if payment_type == "loan" and not selected_loan:
            flash("Set up a CC loan / EMI for this card first.")
            return render_form()

        if selected_loan:
            loan_outstanding = float(selected_loan.get("outstanding_amount") or 0)
            if amount > loan_outstanding:
                flash("The payment cannot be greater than the remaining CC loan balance.")
                return render_form()

        is_previous_card_bill = ask_previous_bill and previous_card_bill == "yes"
        transfer_group = str(uuid.uuid4())
        description = notes or ("CC loan / EMI payment" if payment_type == "loan" else "Credit card bill payment")

        def leg(direction, source_id):
            fields = {
                "direction": direction,
                "category": "transfer",
                "source_id": int(source_id),
                "amount": amount,
                "description": description,
                "raw_text": description,
                "transfer_group": transfer_group,
            }
            if is_previous_card_bill:
                fields["is_previous_card_bill"] = True
            return insert_entry_and_transaction(client, user_id, description, fields)

        entry_ids = []
        try:
            entry_ids.append(leg("out", from_source_id))
            entry_ids.append(leg("in", to_source_id))

            if selected_loan:
                old_loan_outstanding = float(selected_loan.get("outstanding_amount") or 0)
                new_loan_outstanding = max(round(old_loan_outstanding - amount, 2), 0.0)

                client.table("credit_card_loans").update({
                    "outstanding_amount": new_loan_outstanding,
                    "active": new_loan_outstanding > 0,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", selected_loan["id"]).eq("user_id", user_id).execute()

                client.table("credit_card_loan_payments").insert({
                    "loan_id": selected_loan["id"],
                    "user_id": user_id,
                    "amount": amount,
                    "payment_date": datetime.now(APP_TZ).date().isoformat(),
                    "transfer_group": transfer_group,
                }).execute()

        except Exception:
            app.logger.exception("Could not record card payment")
            try:
                if selected_loan:
                    client.table("credit_card_loans").update({
                        "outstanding_amount": old_loan_outstanding,
                        "active": old_loan_outstanding > 0,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }).eq("id", selected_loan["id"]).eq("user_id", user_id).execute()
            except Exception:
                pass

            try:
                if entry_ids:
                    client.table("entries").delete().in_("id", entry_ids).eq("user_id", user_id).execute()
            except Exception:
                pass

            flash("Couldn't record the payment. Please try again.")
            return render_form()

        if ask_previous_bill:
            save_credit_card_setup_completed(client, user_id)

        if payment_type == "loan":
            flash("CC loan / EMI payment recorded. Bank balance, card outstanding and loan balance updated.")
        elif is_previous_card_bill:
            flash("Previous card bill recorded. It lowers your bank and card balances but is not counted as spending.")
        else:
            flash("Payment recorded. Bank and card balances both updated.")

        return redirect(url_for("sources"))

    return render_form()


@app.route("/categories/add", methods=["POST"])
@login_required
def add_category():
    """Adds a custom expense/investment category for this user only, on top
    of the fixed defaults everyone gets (see FIXED_EXPENSE_CATEGORIES /
    FIXED_INVESTMENT_CATEGORIES). Called via fetch() from the Add Entry page
    so a mid-entry category addition doesn't lose whatever else was already
    filled in on that form — hence a small JSON response instead of a
    redirect."""
    client = get_user_client()
    user_id = session["user_id"]
    kind = request.form.get("kind")
    name = (request.form.get("name") or "").strip().lower()

    if kind not in ("expense", "investment") or not name or name == "other":
        return {"ok": False, "error": "That's not a valid category name."}, 400

    try:
        client.table("user_categories").insert({
            "user_id": user_id,
            "kind": kind,
            "name": name,
        }).execute()
    except Exception:
        # Most likely already exists for this user (unique constraint on
        # user_id + kind + name) — not an error from their point of view.
        pass

    return {"ok": True, "name": name}


@app.route("/transactions/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete_transaction(entry_id):
    """Deletes a transaction. If it's one leg of a linked transfer (a Pay CC
    Bill payment or a cash withdrawal), both legs are deleted together —
    removing just one would silently strand the other, leaving a balance
    that no longer reflects a real withdrawal or payment on either side."""
    client = get_user_client()
    user_id = session["user_id"]

    txn = (
        client.table("transactions")
        .select("id, transfer_group")
        .eq("id", entry_id)
        .eq("user_id", user_id)
        .execute()
        .data
    )
    if not txn:
        flash("That transaction wasn't found.")
        return redirect(request.referrer or url_for("dashboard"))

    transfer_group = txn[0].get("transfer_group")
    ids_to_delete = [entry_id]
    if transfer_group:
        paired = (
            client.table("transactions")
            .select("id")
            .eq("user_id", user_id)
            .eq("transfer_group", transfer_group)
            .execute()
            .data
        )
        ids_to_delete = [p["id"] for p in paired]

    for tid in ids_to_delete:
        # If this transaction came from a fixed expense, removing the actual
        # transaction should make the monthly commitment payable again.
        try:
            client.table("fixed_expense_payments").delete().eq("transaction_id", tid).eq("user_id", user_id).execute()
        except Exception:
            # Older databases may not have the fixed-expense tables yet.
            pass
        # Deleting the entries row cascades to its transactions row too.
        client.table("entries").delete().eq("id", tid).eq("user_id", user_id).execute()

    flash("Deleted both linked legs of that transfer." if len(ids_to_delete) > 1 else "Transaction deleted.")
    return redirect(request.referrer or url_for("dashboard"))


# ---------------------------------------------------------------------------
# Trips: a shared-expense splitter, separate from sources/balances on purpose.
# All money math below is done in whole paise (integers) so splits always add
# up to exactly the expense total, with no float drift like 33.33 x 3 = 99.99.
# ---------------------------------------------------------------------------

def _to_paise(value):
    return int(round(float(value) * 100))


def split_equally_paise(total_paise, names):
    """Divides total_paise among names as evenly as whole paise allow. When it
    doesn't divide cleanly, the leftover paise go one each to the first names
    in the list, so the shares always sum to exactly the total."""
    base, remainder = divmod(total_paise, len(names))
    return {name: base + (1 if i < remainder else 0) for i, name in enumerate(names)}


def compute_trip_summary(names, expenses, splits):
    """Per person: what they paid out of pocket, what their share of all the
    expenses came to, and the difference (net). Positive net means the group
    owes them; negative means they owe the group."""
    paid = {n: 0 for n in names}
    owed = {n: 0 for n in names}
    for e in expenses:
        if e["paid_by"] in paid:
            paid[e["paid_by"]] += _to_paise(e["amount"])
    for s in splits:
        if s["participant_name"] in owed:
            owed[s["participant_name"]] += _to_paise(s["share_amount"])
    net = {n: paid[n] - owed[n] for n in names}
    return paid, owed, net


def simplify_settlements(net_paise):
    """Turns each person's net position into the fewest payments that settle
    everyone: the biggest debtor pays the biggest creditor as much as either
    can cover, and so on down the line. For N people that's at most N-1
    payments, instead of everyone paying everyone they individually owe."""
    creditors = sorted(([n, v] for n, v in net_paise.items() if v > 0), key=lambda x: -x[1])
    debtors = sorted(([n, -v] for n, v in net_paise.items() if v < 0), key=lambda x: -x[1])
    payments = []
    i = j = 0
    while i < len(creditors) and j < len(debtors):
        amount = min(creditors[i][1], debtors[j][1])
        payments.append({"from": debtors[j][0], "to": creditors[i][0], "amount": amount / 100})
        creditors[i][1] -= amount
        debtors[j][1] -= amount
        if creditors[i][1] == 0:
            i += 1
        if debtors[j][1] == 0:
            j += 1
    return payments


def parse_friend_names(raw):
    """'Rohan, Priya\\nAman' -> ['Rohan', 'Priya', 'Aman']. Drops blanks and
    duplicates (case-insensitive), and any name that would clash with the
    implicit 'You' member every trip already has."""
    names, seen = [], {"you"}
    for part in raw.replace("\n", ",").split(","):
        name = part.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names


def get_trip_or_none(client, user_id, trip_id):
    rows = (
        client.table("trips")
        .select("id, name, is_group, active, created_at, destination, start_date, end_date, budget, status")
        .eq("id", trip_id)
        .execute()
        .data
    )
    if not rows:
        return None
    return dict(rows[0])


def current_trip_member_id(client, trip_id):
    rows = client.rpc("current_trip_member_id", {"p_trip_id": trip_id}).execute().data
    if isinstance(rows, list):
        return rows[0] if rows else None
    return rows


def is_trip_owner(client, trip_id):
    result = client.rpc("is_trip_owner", {"p_trip_id": trip_id}).execute().data
    return bool(result[0] if isinstance(result, list) and result else result)


def get_trip_expense_permissions(client, trip_id):
    rows = client.rpc("current_trip_expense_permissions", {"p_trip_id": trip_id}).execute().data or []
    return {row["expense_id"]: row for row in rows}


def get_trip_share_links(client, trip_id):
    rows = client.rpc("current_trip_share_links", {"p_trip_id": trip_id}).execute().data or []
    return {row["share_id"]: row for row in rows}


def get_trip_settlement_links(client, trip_id):
    rows = client.rpc("current_trip_settlement_links", {"p_trip_id": trip_id}).execute().data or []
    return {row["settlement_id"]: row for row in rows}


def trip_rpc(client, name, args):
    """Call a trip RPC and return its scalar result where possible."""
    result = client.rpc(name, args).execute().data
    if isinstance(result, list) and len(result) == 1:
        return result[0]
    return result


def trip_action_error(exc, fallback):
    """Return a short public error without exposing PostgREST/SQL internals."""
    message = str(exc).lower()
    known = (
        ("archived trips are read-only", "Archived trips are read-only."),
        ("choose an account for this personal expense", "Choose an expense account in Profile or select one here."),
        ("choose an account to receive the payment", "Choose the account where you received the payment."),
        ("choose the account used to pay", "Choose the account used to pay."),
        ("choose an active account that belongs to you", "Choose one of your active accounts."),
        ("only the person paying can mark this paid", "Only the person paying can mark this settlement paid."),
        ("only the recipient can record this receipt", "Only the recipient can record this receipt."),
        ("that expense share is not yours", "That share is not assigned to your account."),
        ("this share was rejected", "This share was rejected. Ask the payer to reassign it."),
        ("only the payer can reassign", "Only the person who added the expense can reassign this share."),
        ("only the trip owner can add members", "Only the trip owner can add members."),
        ("add this person as a minto friend first", "Add this person as a Minto friend before inviting them to the trip."),
        ("no minto user found", "No Minto user found. Check the username and try again."),
        ("larger than the current balance", "That amount is larger than the current balance between these members."),
    )
    for needle, safe_message in known:
        if needle in message:
            return safe_message
    return fallback


def get_active_sources(client):
    return client.table("user_sources").select("id, name, source_type").eq("active", True).order("name").execute().data


def get_trip_payables(client, user_id):
    """Accepted trip shares that are still owed by this account."""
    try:
        result = client.rpc("current_trip_payables").execute().data
        if isinstance(result, list):
            result = result[0] if result else 0
        return float(result or 0)
    except Exception:
        app.logger.exception("Could not load trip payables")
        return 0


@app.route("/mode", methods=["GET", "POST"])
@login_required
def set_mode():
    mode = request.values.get("mode")
    if mode in MODES:
        session["mode"] = mode
        save_mode(get_user_client(), session["user_id"], mode)
    return redirect(home_url())


@app.route("/trip-home")
@login_required
def trip_home():
    """Lands on trip details directly: the trip you last had open, else your
    most recent one, else the trips page so you can create the first."""
    client = get_user_client()
    user_id = session["user_id"]

    active = session.get("active_trip")
    if active and get_trip_or_none(client, user_id, active):
        return redirect(url_for("trip_detail", trip_id=active))
    session.pop("active_trip", None)

    latest = client.table("trips").select("id").order("created_at", desc=True).execute().data
    if latest:
        return redirect(url_for("trip_detail", trip_id=latest[0]["id"]))
    return redirect(url_for("trips"))


def get_trip_people(client, user_id, trip_id):
    """Return safe member snapshots and stable member-id/name mappings."""
    current_id = current_trip_member_id(client, trip_id)
    members = (
        client.table("trip_members")
        .select("id, trip_id, guest_name, display_name, profile_emoji, role, active, removed_at")
        .eq("trip_id", trip_id)
        .order("id")
        .execute()
        .data
    )
    for member in members:
        member["name"] = "You" if member["id"] == current_id else member.get("display_name") or member.get("guest_name") or "Trip member"
        member["is_you"] = member["id"] == current_id
        member["is_minto"] = member.get("guest_name") is None
    active = [m for m in members if m.get("active")]
    friends = [m for m in active if not m.get("is_you")]
    people = {str(m["id"]): m["name"] for m in active}
    return friends, people, members, current_id


def get_trip_settlement_rows(client, user_id, trip_id):
    rows = (
        client.table("trip_settlements")
        .select("id, trip_id, from_person, to_person, amount, settlement_date, status, paid_at, created_at, from_member_id, to_member_id, received_at")
        .eq("trip_id", trip_id)
        .order("settlement_date", desc=True)
        .order("id", desc=True)
        .execute()
        .data
    )
    links = get_trip_settlement_links(client, trip_id)
    for row in rows:
        row.update(links.get(row["id"], {}))
    return rows


def apply_paid_trip_settlements(net_paise, settlement_rows):
    """Reduce expense-derived balances by settlements that are actually paid.
    Pending settlements remain visible but deliberately do not change the debt."""
    for s in settlement_rows:
        if s.get("status") != "paid":
            continue
        amount = _to_paise(s.get("amount"))
        from_person = s.get("from_person")
        to_person = s.get("to_person")
        if amount <= 0 or from_person not in net_paise or to_person not in net_paise:
            continue
        net_paise[from_person] -= amount
        net_paise[to_person] += amount
    return net_paise


def trip_date_summary(trip, today):
    start = parse_iso_date(trip.get("start_date"))
    end = parse_iso_date(trip.get("end_date"))
    days = None
    days_left = None
    if start and end:
        days = (end - start).days + 1
        days_left = max((end - today).days, 0)
    elif start:
        days_left = max((start - today).days, 0)
    return {
        "start": start,
        "end": end,
        "days": days,
        "days_left": days_left,
    }


def trip_status_label(status):
    return {
        "planned": "Planned",
        "active": "Active",
        "completed": "Completed",
        "archived": "Archived",
    }.get(status, "Active")


def build_trip_expense_form(client, user_id, trip, require_source=True):
    """Validate the add/edit form and return its normalized expense data."""
    description = (request.form.get("description") or "").strip()
    amount = parse_money(request.form.get("amount"))
    category = (request.form.get("category") or "other").strip().lower()
    expense_date = parse_iso_date(request.form.get("expense_date")) or datetime.now(APP_TZ).date()

    if not description:
        return None, None, "An expense needs a description."
    if amount is None:
        return None, None, "Enter a valid amount greater than zero."
    if category not in TRIP_EXPENSE_CATEGORY_KEYS:
        return None, None, "Pick a valid expense category."

    payer_name = "You"
    shares = None
    payer_member_id = None
    source_id = None

    if trip["is_group"]:
        friends, people, members, current_id = get_trip_people(client, user_id, trip["id"])
        active_members = [m for m in members if m.get("active")]
        member_by_id = {str(m["id"]): m for m in active_members}
        payer = member_by_id.get(str(request.form.get("paid_by") or current_id))
        if not payer:
            return None, None, "Pick who paid."
        payer_member_id = payer["id"]
        payer_name = payer["name"]
        if payer.get("is_you"):
            try:
                source_id = int(request.form.get("source_id") or 0)
            except (TypeError, ValueError):
                source_id = 0
            if source_id <= 0 and require_source:
                return None, None, "Choose the account used to pay."
            if source_id <= 0:
                source_id = None
        elif payer.get("is_minto"):
            return None, None, "Only the person who paid can add a Minto member's linked expense."

        split_type = request.form.get("split_type", "equal")
        if split_type == "equal":
            shares = {int(mid): share for mid, share in split_equally_paise(_to_paise(amount), {str(m["id"]): m["name"] for m in active_members}).items()}
        elif split_type == "subset":
            chosen = [member_by_id[k] for k in request.form.getlist("split_with") if k in member_by_id]
            if not chosen:
                return None, None, "Pick at least one person to split this between."
            shares = {int(mid): share for mid, share in split_equally_paise(_to_paise(amount), {str(m["id"]): m["name"] for m in chosen}).items()}
        elif split_type == "custom":
            shares = {}
            for member in active_members:
                key = str(member["id"])
                raw = (request.form.get(f"share_{key}") or "").strip()
                if not raw:
                    continue
                try:
                    paise = _to_paise(raw)
                except (TypeError, ValueError):
                    return None, None, f"'{raw}' isn't a valid amount."
                if paise < 0:
                    return None, None, "Custom amounts can't be negative."
                if paise > 0:
                    shares[int(member["id"])] = paise
            if sum(shares.values()) != _to_paise(amount):
                return (
                    None,
                    None,
                    f"The custom amounts add up to {sum(shares.values()) / 100:.2f}, "
                    f"but the expense is {amount:.2f}. They need to match.",
                )
        else:
            return None, None, "Pick how to split this expense."

    payload = {
        "description": description,
        "amount": amount,
        "paid_by": payer_name,
        "category": category,
        "expense_date": expense_date.isoformat(),
    }
    if trip["is_group"]:
        payload["payer_member_id"] = payer_member_id
        payload["source_id"] = source_id
    return payload, shares, None


def load_trip_financials(client, user_id, trip_id):
    """Load the full trip ledger needed for settlement validation and totals."""
    expenses = (
        client.table("trip_expenses")
        .select("id, trip_id, description, amount, paid_by, expense_date, category, created_at, payer_member_id")
        .eq("trip_id", trip_id)
        .order("expense_date", desc=True)
        .order("id", desc=True)
        .execute()
        .data
    )
    expense_ids = [e["id"] for e in expenses]
    splits = []
    shares = []
    if expense_ids:
        splits = (
            client.table("trip_expense_splits")
            .select("id, trip_expense_id, participant_name, share_amount, trip_member_id")
            .in_("trip_expense_id", expense_ids)
            .execute()
            .data
        )
        shares = client.table("trip_expense_shares").select("id, trip_expense_id, trip_member_id, amount, status, created_at, responded_at").in_("trip_expense_id", expense_ids).execute().data
    settlement_rows = get_trip_settlement_rows(client, user_id, trip_id)
    friends, people, members, current_id = get_trip_people(client, user_id, trip_id)
    paid, owed, net = calculate_trip_balances(members, expenses, shares, settlement_rows, splits)
    return {
        "expenses": expenses,
        "splits": splits,
        "shares": shares,
        "settlements": settlement_rows,
        "friends": friends,
        "people": people,
        "members": members,
        "current_member_id": current_id,
        "paid": paid,
        "owed": owed,
        "net": net,
    }


@app.route("/trips", methods=["GET", "POST"])
@login_required
def trips():
    client = get_user_client()
    user_id = session["user_id"]
    today = datetime.now(APP_TZ).date()

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        destination = (request.form.get("destination") or "").strip() or None
        start_date = parse_iso_date(request.form.get("start_date"))
        end_date = parse_iso_date(request.form.get("end_date"))
        budget = parse_money(request.form.get("budget"), allow_zero=True)

        if not name:
            flash("Give the trip a name.")
            return redirect(url_for("trips"))
        if request.form.get("start_date") and start_date is None:
            flash("Use DD-MON-YYYY for the trip start date.")
            return redirect(url_for("trips"))
        if request.form.get("end_date") and end_date is None:
            flash("Use DD-MON-YYYY for the trip end date.")
            return redirect(url_for("trips"))
        if start_date and end_date and end_date < start_date:
            flash("The trip end date cannot be before the start date.")
            return redirect(url_for("trips"))
        if request.form.get("budget") and budget is None:
            flash("Enter a valid trip budget.")
            return redirect(url_for("trips"))

        status = "planned" if start_date and start_date > today else "active"
        created = client.table("trips").insert({
            "user_id": user_id,
            "name": name,
            "destination": destination,
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
            "budget": budget,
            "is_group": request.form.get("trip_type") == "group",
            "status": status,
            "active": True,
        }).select("id").execute()
        trip_id = created.data[0]["id"]

        if request.form.get("trip_type") == "group":
            friend_names = parse_friend_names(request.form.get("guest_names") or request.form.get("friends") or "")
            if friend_names:
                try:
                    client.table("trip_members").insert([
                        {"trip_id": trip_id, "guest_name": n, "display_name": n, "role": "member"}
                        for n in friend_names
                    ]).select("id").execute()
                except Exception:
                    flash("The trip was created, but the members list couldn't be saved. Add them below.")
            for username in parse_friend_names(request.form.get("friend_usernames") or ""):
                try:
                    trip_rpc(client, "add_trip_minto_member", {"p_trip_id": trip_id, "p_username": username})
                except Exception as e:
                    app.logger.info("Trip friend invite failed: %s", e)
                    flash(f"Could not add @{username}. Confirm the username and that you are Minto friends.")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    trip_rows = (
        client.table("trips")
        .select("id, name, is_group, active, created_at, destination, start_date, end_date, budget, status")
        .order("created_at", desc=True)
        .execute()
        .data
    )
    expense_rows = (
        client.table("trip_expenses")
        .select("trip_id, amount")
        .execute()
        .data
    )
    member_rows = client.table("trip_members").select("trip_id").eq("active", True).execute().data
    totals = defaultdict(int)
    for e in expense_rows:
        totals[e["trip_id"]] += _to_paise(e["amount"])
    friend_counts = defaultdict(int)
    for f in member_rows:
        friend_counts[f["trip_id"]] += 1
    for t in trip_rows:
        t["total"] = totals[t["id"]] / 100
        t["friend_count"] = max(friend_counts[t["id"]] - 1, 0)
        t["date_summary"] = trip_date_summary(t, today)
        budget = float(t.get("budget") or 0)
        t["budget_remaining"] = max(budget - t["total"], 0) if budget > 0 else None
        t["budget_pct"] = min(round((t["total"] / budget) * 100, 1), 100) if budget > 0 else None
        t["status_label"] = trip_status_label(t.get("status"))
        t["is_owner"] = is_trip_owner(client, t["id"])

    return render_template("trips.html", trips=trip_rows, today=today.isoformat())


@app.route("/trips/<int:trip_id>/update", methods=["POST"])
@login_required
def update_trip(trip_id):
    client = get_user_client()
    user_id = session["user_id"]
    trip = get_trip_or_none(client, user_id, trip_id)
    if not trip:
        flash("That trip wasn't found.")
        return redirect(url_for("trips"))
    if not is_trip_owner(client, trip_id):
        flash("Only the trip owner can change trip settings.")
        return redirect(url_for("trip_detail", trip_id=trip_id))
    if trip.get("status") == "archived":
        flash("Archived trips are read-only.")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    name = (request.form.get("name") or "").strip()
    destination = (request.form.get("destination") or "").strip() or None
    start_date = parse_iso_date(request.form.get("start_date"))
    end_date = parse_iso_date(request.form.get("end_date"))
    budget = parse_money(request.form.get("budget"), allow_zero=True)
    status = request.form.get("status")

    if not name:
        flash("Give the trip a name.")
        return redirect(url_for("trip_detail", trip_id=trip_id))
    if request.form.get("start_date") and start_date is None:
        flash("Use DD-MON-YYYY for the trip start date.")
        return redirect(url_for("trip_detail", trip_id=trip_id))
    if request.form.get("end_date") and end_date is None:
        flash("Use DD-MON-YYYY for the trip end date.")
        return redirect(url_for("trip_detail", trip_id=trip_id))
    if start_date and end_date and end_date < start_date:
        flash("The trip end date cannot be before the start date.")
        return redirect(url_for("trip_detail", trip_id=trip_id))
    if request.form.get("budget") and budget is None:
        flash("Enter a valid trip budget.")
        return redirect(url_for("trip_detail", trip_id=trip_id))
    if status not in ("planned", "active", "completed", "archived"):
        flash("Pick a valid trip status.")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    client.table("trips").update({
        "name": name,
        "destination": destination,
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
        "budget": budget,
        "status": status,
        "active": status != "archived",
    }).eq("id", trip_id).select("id").execute()

    flash("Trip details updated.")
    return redirect(url_for("trip_detail", trip_id=trip_id))


@app.route("/trips/<int:trip_id>/friends", methods=["POST"])
@login_required
def add_trip_friends(trip_id):
    client = get_user_client()
    user_id = session["user_id"]
    trip = get_trip_or_none(client, user_id, trip_id)

    if not trip or not trip["is_group"]:
        flash("That trip wasn't found, or it's a solo trip.")
        return redirect(url_for("trips"))
    if not is_trip_owner(client, trip_id):
        flash("Only the trip owner can manage trip members.")
        return redirect(url_for("trip_detail", trip_id=trip_id))
    if trip.get("status") == "archived":
        flash("Archived trips are read-only.")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    guest_names = parse_friend_names(request.form.get("guest_names") or request.form.get("friends") or "")
    existing = {m.get("guest_name", "").casefold() for m in client.table("trip_members").select("guest_name").eq("trip_id", trip_id).execute().data if m.get("guest_name")}
    new_names = [n for n in guest_names if n.casefold() not in existing]
    if new_names:
        client.table("trip_members").insert([
            {"trip_id": trip_id, "guest_name": n, "display_name": n, "role": "member"} for n in new_names
        ]).select("id").execute()
    usernames = parse_friend_names(request.form.get("friend_usernames") or "")
    for username in usernames:
        try:
            trip_rpc(client, "add_trip_minto_member", {"p_trip_id": trip_id, "p_username": username})
        except Exception as e:
            app.logger.info("Trip friend invite failed: %s", e)
            flash(f"Could not add @{username}. Confirm the username and that you are Minto friends.")
    if not new_names and not usernames:
        flash("Enter a guest name or a Minto friend username.")
    return redirect(url_for("trip_detail", trip_id=trip_id))


@app.route("/trips/<int:trip_id>/friends/<int:friend_id>/delete", methods=["POST"])
@login_required
def remove_trip_friend(trip_id, friend_id):
    client = get_user_client()
    user_id = session["user_id"]
    trip = get_trip_or_none(client, user_id, trip_id)
    if not trip:
        flash("That trip wasn't found.")
        return redirect(url_for("trips"))
    if not is_trip_owner(client, trip_id):
        flash("Only the trip owner can remove trip members.")
        return redirect(url_for("trip_detail", trip_id=trip_id))
    if trip.get("status") == "archived":
        flash("Archived trips are read-only.")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    friend_rows = (
        client.table("trip_members")
        .select("id, display_name, guest_name, role, active")
        .eq("id", friend_id)
        .eq("trip_id", trip_id)
        .execute()
        .data
    )
    if not friend_rows or friend_rows[0].get("role") == "owner" or not friend_rows[0].get("active"):
        flash("That member wasn't found.")
        return redirect(url_for("trip_detail", trip_id=trip_id))
    name = friend_rows[0].get("display_name") or friend_rows[0].get("guest_name") or "Member"
    client.table("trip_members").update({"active": False, "removed_at": datetime.now(timezone.utc).isoformat()}).eq("id", friend_id).eq("trip_id", trip_id).select("id").execute()
    flash(f"{name} was removed. Their past shares and settlements remain in the trip history.")
    return redirect(url_for("trip_detail", trip_id=trip_id))


@app.route("/trips/<int:trip_id>/expenses", methods=["POST"])
@login_required
def add_trip_expense(trip_id):
    client = get_user_client()
    user_id = session["user_id"]
    back = redirect(url_for("trip_detail", trip_id=trip_id))

    trip = get_trip_or_none(client, user_id, trip_id)
    if not trip:
        flash("That trip wasn't found.")
        return redirect(url_for("trips"))
    if trip.get("status") == "archived":
        flash("Archived trips are read-only.")
        return back

    payload, shares, error = build_trip_expense_form(client, user_id, trip)
    if error:
        flash(error)
        return back

    try:
        if trip["is_group"]:
            expense_id = trip_rpc(client, "create_shared_trip_expense", {
                "p_trip_id": trip_id,
                "p_description": payload["description"],
                "p_amount": payload["amount"],
                "p_category": payload["category"],
                "p_expense_date": payload["expense_date"],
                "p_payer_member_id": payload["payer_member_id"],
                "p_source_id": payload["source_id"],
                "p_shares": [
                    {"member_id": member_id, "amount": paise / 100}
                    for member_id, paise in shares.items()
                ],
            })
        else:
            created = client.table("trip_expenses").insert({
                "trip_id": trip_id,
                "user_id": user_id,
                **payload,
            }).select("id").execute()
            expense_id = created.data[0]["id"]
    except Exception as e:
        app.logger.info("Trip expense creation failed: %s", e)
        flash(trip_action_error(e, "Couldn't save that expense. Check the payer, account, and share amounts."))
        return back

    flash("Expense added.")
    return back


@app.route("/trips/<int:trip_id>/expenses/<int:expense_id>/edit", methods=["GET", "POST"])
@login_required
def edit_trip_expense(trip_id, expense_id):
    client = get_user_client()
    user_id = session["user_id"]
    trip = get_trip_or_none(client, user_id, trip_id)
    if not trip:
        flash("That trip wasn't found.")
        return redirect(url_for("trips"))
    if trip.get("status") == "archived":
        flash("Archived trips are read-only.")
        return redirect(url_for("trip_detail", trip_id=trip_id))
    rows = (
        client.table("trip_expenses")
        .select("id, trip_id, description, amount, paid_by, expense_date, category, created_at, payer_member_id")
        .eq("id", expense_id)
        .eq("trip_id", trip_id)
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        flash("That expense wasn't found.")
        return redirect(url_for("trip_detail", trip_id=trip_id))
    expense = rows[0]
    expense_permission = get_trip_expense_permissions(client, trip_id).get(expense_id, {})
    if not expense_permission.get("is_author"):
        flash("Only the person who added this expense can edit it.")
        return redirect(url_for("trip_detail", trip_id=trip_id))
    expense["is_author"] = True
    expense["payment_transaction_id"] = expense_permission.get("payment_transaction_id")

    friends, people, members, current_id = get_trip_people(client, user_id, trip_id)
    splits = (
        client.table("trip_expense_splits")
        .select("id, trip_expense_id, participant_name, share_amount, trip_member_id")
        .eq("trip_expense_id", expense_id)
        .execute()
        .data
    )

    if request.method == "POST":
        if expense.get("payment_transaction_id"):
            flash("This linked expense has already affected account records. Add a correcting expense instead of changing its history.")
            return redirect(url_for("trip_detail", trip_id=trip_id))
        payload, shares, error = build_trip_expense_form(client, user_id, trip, require_source=False)
        if error:
            flash(error)
            return render_template(
                "trip_expense_edit.html",
                trip=trip,
                expense=expense,
                friends=friends,
                people=people,
                members=[m for m in members if m.get("active")],
                expense_accounts=get_active_sources(client),
                splits=splits,
                trip_expense_categories=TRIP_EXPENSE_CATEGORIES,
                today=datetime.now(APP_TZ).date().isoformat(),
            )

        payload.pop("source_id", None)
        try:
            if trip["is_group"]:
                trip_rpc(client, "update_unlinked_trip_expense", {
                    "p_expense_id": expense_id,
                    "p_description": payload["description"],
                    "p_amount": payload["amount"],
                    "p_category": payload["category"],
                    "p_expense_date": payload["expense_date"],
                    "p_payer_member_id": payload["payer_member_id"],
                    "p_shares": [{"member_id": member_id, "amount": paise / 100} for member_id, paise in shares.items()],
                })
            else:
                trip_rpc(client, "update_solo_trip_expense", {
                    "p_expense_id": expense_id,
                    "p_description": payload["description"],
                    "p_amount": payload["amount"],
                    "p_category": payload["category"],
                    "p_expense_date": payload["expense_date"],
                })
        except Exception as e:
            app.logger.info("Trip expense update failed: %s", e)
            flash(trip_action_error(e, "Couldn't update that expense. Check the details and try again."))
            return redirect(url_for("trip_detail", trip_id=trip_id))

        flash("Expense updated.")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    return render_template(
        "trip_expense_edit.html",
        trip=trip,
        expense=expense,
        friends=friends,
        people=people,
        members=[m for m in members if m.get("active")],
        expense_accounts=get_active_sources(client),
        splits=splits,
        trip_expense_categories=TRIP_EXPENSE_CATEGORIES,
        today=datetime.now(APP_TZ).date().isoformat(),
    )


@app.route("/trips/<int:trip_id>/expenses/<int:expense_id>/delete", methods=["POST"])
@login_required
def delete_trip_expense(trip_id, expense_id):
    client = get_user_client()
    user_id = session["user_id"]
    trip = get_trip_or_none(client, user_id, trip_id)
    if trip and trip.get("status") == "archived":
        flash("Archived trips are read-only.")
        return redirect(url_for("trip_detail", trip_id=trip_id))
    if not trip:
        flash("That trip wasn't found.")
        return redirect(url_for("trips"))
    try:
        if trip.get("is_group"):
            trip_rpc(client, "delete_unlinked_trip_expense", {"p_expense_id": expense_id})
        else:
            trip_rpc(client, "delete_unlinked_trip_expense", {"p_expense_id": expense_id})
    except Exception as e:
        app.logger.info("Trip expense deletion failed: %s", e)
        flash(trip_action_error(e, "Couldn't delete that expense. Account-linked expense history is protected."))
        return redirect(url_for("trip_detail", trip_id=trip_id))
    flash("Expense deleted.")
    return redirect(url_for("trip_detail", trip_id=trip_id))


@app.route("/trips/<int:trip_id>/settlements", methods=["POST"])
@login_required
def add_trip_settlement(trip_id):
    client = get_user_client()
    user_id = session["user_id"]
    back = redirect(url_for("trip_detail", trip_id=trip_id))
    trip = get_trip_or_none(client, user_id, trip_id)
    if not trip:
        flash("That trip wasn't found.")
        return redirect(url_for("trips"))
    if trip.get("status") == "archived":
        flash("Archived trips are read-only.")
        return back

    data = load_trip_financials(client, user_id, trip_id)
    from_person = (request.form.get("from_person") or "").strip()
    to_person = (request.form.get("to_person") or "").strip()
    amount = parse_money(request.form.get("amount"))
    settlement_date = parse_iso_date(request.form.get("settlement_date")) or datetime.now(APP_TZ).date()
    if from_person not in data["people"] or to_person not in data["people"] or from_person == to_person:
        flash("Pick two different trip members.")
        return back
    if amount is None:
        flash("Enter a valid settlement amount.")
        return back
    try:
        trip_rpc(client, "create_trip_settlement", {
            "p_trip_id": trip_id,
            "p_from_member_id": int(from_person),
            "p_to_member_id": int(to_person),
            "p_amount": amount,
            "p_settlement_date": settlement_date.isoformat(),
        })
        flash("Pending settlement recorded. Mark it paid when the money moves.")
    except Exception as e:
        app.logger.info("Trip settlement creation failed: %s", e)
        flash(trip_action_error(e, "Couldn't record that settlement. Check the members and amount."))
    return back


@app.route("/trips/<int:trip_id>/settlements/<int:settlement_id>/status", methods=["POST"])
@login_required
def update_trip_settlement_status(trip_id, settlement_id):
    client = get_user_client()
    user_id = session["user_id"]
    back = redirect(url_for("trip_detail", trip_id=trip_id))
    trip = get_trip_or_none(client, user_id, trip_id)
    if not trip:
        flash("That trip wasn't found.")
        return redirect(url_for("trips"))

    rows = (
        client.table("trip_settlements")
        .select("id, trip_id, from_member_id, to_member_id, amount, settlement_date, status, paid_at")
        .eq("id", settlement_id)
        .eq("trip_id", trip_id)
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        flash("That settlement wasn't found.")
        return back

    settlement = rows[0]
    new_status = request.form.get("status")
    if new_status not in ("pending", "paid"):
        flash("Pick Pending or Paid.")
        return back
    if trip.get("status") == "archived":
        flash("Archived trips are read-only.")
        return back

    raw_source = (request.form.get("source_id") or "").strip()
    try:
        source_id = int(raw_source) if raw_source else None
        trip_rpc(client, "set_trip_settlement_paid", {
            "p_settlement_id": settlement_id,
            "p_source_id": source_id,
            "p_new_status": new_status,
        })
        flash("Settlement updated. Account records were adjusted with it.")
    except Exception as e:
        app.logger.info("Trip settlement update failed: %s", e)
        flash(trip_action_error(e, "Couldn't update that settlement. Check the account and current status."))
    return back


@app.route("/trips/<int:trip_id>/shares/<int:share_id>/accept", methods=["POST"])
@login_required
def accept_trip_expense_share(trip_id, share_id):
    client = get_user_client()
    try:
        raw_source = (request.form.get("source_id") or "").strip()
        source_id = int(raw_source) if raw_source else None
        trip_rpc(client, "accept_trip_expense_share", {"p_share_id": share_id, "p_source_id": source_id})
        flash("Your share is now recorded as a personal expense. Account balances were not changed.")
    except Exception as e:
        app.logger.info("Trip share accept failed: %s", e)
        flash(trip_action_error(e, "Couldn't accept that share. Choose an account and try again."))
    return redirect(url_for("trip_detail", trip_id=trip_id))


@app.route("/trips/<int:trip_id>/shares/<int:share_id>/reject", methods=["POST"])
@login_required
def reject_trip_expense_share(trip_id, share_id):
    client = get_user_client()
    try:
        trip_rpc(client, "reject_trip_expense_share", {"p_share_id": share_id})
        flash("Share rejected. The trip owner can reassign it to another member.")
    except Exception as e:
        app.logger.info("Trip share rejection failed: %s", e)
        flash(trip_action_error(e, "Couldn't reject that share. Refresh and try again."))
    return redirect(url_for("trip_detail", trip_id=trip_id))


@app.route("/trips/<int:trip_id>/shares/<int:share_id>/reassign", methods=["POST"])
@login_required
def reassign_rejected_trip_share(trip_id, share_id):
    client = get_user_client()
    try:
        replacement_id = int(request.form.get("member_id") or 0)
        trip_rpc(client, "reassign_rejected_trip_share", {
            "p_share_id": share_id,
            "p_replacement_member_id": replacement_id,
        })
        flash("Rejected share reassigned.")
    except Exception as e:
        app.logger.info("Trip share reassignment failed: %s", e)
        flash(trip_action_error(e, "Couldn't reassign that share. Choose an active member."))
    return redirect(url_for("trip_detail", trip_id=trip_id))


@app.route("/trips/<int:trip_id>/settlements/<int:settlement_id>/receipt", methods=["POST"])
@login_required
def record_trip_settlement_receipt(trip_id, settlement_id):
    client = get_user_client()
    try:
        source_id = int(request.form.get("source_id") or 0)
        trip_rpc(client, "record_trip_settlement_receipt", {
            "p_settlement_id": settlement_id,
            "p_source_id": source_id,
        })
        flash("Settlement receipt recorded in your selected account.")
    except Exception as e:
        app.logger.info("Trip settlement receipt failed: %s", e)
        flash(trip_action_error(e, "Couldn't record that receipt. Choose an active account."))
    return redirect(url_for("trip_detail", trip_id=trip_id))


@app.route("/transactions/<int:transaction_id>/linked")
@login_required
def linked_transaction(transaction_id):
    client = get_user_client()
    rows = client.table("transactions").select("id, direction, category, amount, currency, description, transaction_date, source_id, trip_expense_id, trip_expense_share_id, trip_settlement_id").eq("id", transaction_id).eq("user_id", session["user_id"]).limit(1).execute().data
    if not rows:
        flash("That linked transaction wasn't found.")
        return redirect(url_for("trip_home"))
    return render_template("linked_transaction.html", transaction=rows[0])


@app.route("/trips/<int:trip_id>/delete", methods=["POST"])
@login_required
def delete_trip(trip_id):
    client = get_user_client()
    user_id = session["user_id"]
    trip = get_trip_or_none(client, user_id, trip_id)
    if not trip or not is_trip_owner(client, trip_id):
        flash("Only the trip owner can delete this trip.")
        return redirect(url_for("trips"))
    if trip.get("status") == "archived":
        flash("Archived trips are read-only.")
        return redirect(url_for("trip_detail", trip_id=trip_id))
    client.table("trips").delete().eq("id", trip_id).select("id").execute()
    if session.get("active_trip") == trip_id:
        session.pop("active_trip", None)
    flash("Trip deleted.")
    return redirect(url_for("trips"))


@app.route("/trips/<int:trip_id>/status", methods=["POST"])
@login_required
def update_trip_status(trip_id):
    client = get_user_client()
    user_id = session["user_id"]
    trip = get_trip_or_none(client, user_id, trip_id)
    if not trip:
        flash("That trip wasn't found.")
        return redirect(url_for("trips"))
    if not is_trip_owner(client, trip_id):
        flash("Only the trip owner can change trip status.")
        return redirect(url_for("trip_detail", trip_id=trip_id))
    if trip.get("status") == "archived":
        flash("Archived trips are read-only.")
        return redirect(url_for("trip_detail", trip_id=trip_id))
    status = request.form.get("status")
    if status not in ("planned", "active", "completed", "archived"):
        flash("Pick a valid trip status.")
        return redirect(url_for("trip_detail", trip_id=trip_id))
    client.table("trips").update({
        "status": status,
        "active": status != "archived",
    }).eq("id", trip_id).select("id").execute()
    flash("Trip status updated.")
    return redirect(url_for("trip_detail", trip_id=trip_id))


@app.route("/trips/<int:trip_id>")
@login_required
def trip_detail(trip_id):
    client = get_user_client()
    user_id = session["user_id"]
    today = datetime.now(APP_TZ).date()

    trip = get_trip_or_none(client, user_id, trip_id)
    if not trip:
        flash("That trip wasn't found.")
        return redirect(url_for("trips"))

    session["active_trip"] = trip_id
    trip["status_label"] = trip_status_label(trip.get("status"))
    trip_owner = is_trip_owner(client, trip_id)
    current_id = current_trip_member_id(client, trip_id)
    friends, people, members, current_id = get_trip_people(client, user_id, trip_id)
    expenses = (
        client.table("trip_expenses")
        .select("id, trip_id, description, amount, paid_by, expense_date, category, created_at, payer_member_id")
        .eq("trip_id", trip_id)
        .order("expense_date", desc=True)
        .order("id", desc=True)
        .execute()
        .data
    )
    settlement_rows = get_trip_settlement_rows(client, user_id, trip_id)
    expense_ids = [e["id"] for e in expenses]
    shares = client.table("trip_expense_shares").select("id, trip_expense_id, trip_member_id, amount, status, created_at, responded_at").in_("trip_expense_id", expense_ids).execute().data if expense_ids else []
    splits = client.table("trip_expense_splits").select("trip_expense_id, trip_member_id, participant_name, share_amount").in_("trip_expense_id", expense_ids).execute().data if expense_ids else []
    share_links = get_trip_share_links(client, trip_id)
    for share in shares:
        share.update(share_links.get(share["id"], {}))
    expense_permissions = get_trip_expense_permissions(client, trip_id)
    for expense in expenses:
        permission = expense_permissions.get(expense["id"], {})
        expense["is_author"] = bool(permission.get("is_author"))
        if expense["is_author"]:
            expense["payment_transaction_id"] = permission.get("payment_transaction_id")
    linked_share_expense_ids = {s["trip_expense_id"] for s in shares if s.get("personal_transaction_id") or s.get("payer_transaction_id")}

    member_by_id = {m["id"]: m for m in members}
    shares_by_expense = defaultdict(list)
    authored_expense_ids = {e["id"] for e in expenses if e.get("is_author")}
    for share in shares:
        member = member_by_id.get(share["trip_member_id"], {})
        share["member_name"] = member.get("name") or member.get("display_name") or "Former member"
        share["is_you"] = share.get("trip_member_id") == current_id
        share["can_respond"] = share["is_you"] and share.get("status") == "pending"
        # Personal transaction ids are private to that account. The payer's
        # receivable id is visible only to the account that created it.
        if not share["is_you"]:
            share.pop("personal_transaction_id", None)
        if share.get("trip_expense_id") not in authored_expense_ids:
            share.pop("payer_transaction_id", None)
        shares_by_expense[share["trip_expense_id"]].append(share)

    splits_by_expense = defaultdict(list)
    for split in splits:
        splits_by_expense[split["trip_expense_id"]].append(split)
    for expense in expenses:
        expense["shares"] = shares_by_expense[expense["id"]]
        expense["splits"] = splits_by_expense[expense["id"]]
        expense["can_edit"] = expense.get("is_author") and not expense.get("payment_transaction_id") and expense["id"] not in linked_share_expense_ids
        payer_member = member_by_id.get(expense.get("payer_member_id"), {})
        expense["payer_name"] = payer_member.get("name") or expense.get("paid_by") or "Former member"
        if not expense["is_author"]:
            expense.pop("payment_transaction_id", None)

    paid, owed, net = calculate_trip_balances(members, expenses, shares, settlement_rows, splits)
    total_paise = sum(_to_paise(e["amount"]) for e in expenses)
    people_rows = []
    for member in members:
        member_id = member["id"]
        people_rows.append({
            "member_id": member_id,
            "name": member.get("name") or "Former member",
            "paid": paid.get(member_id, 0) / 100,
            "owed": owed.get(member_id, 0) / 100,
            "net": net.get(member_id, 0) / 100,
            "active": member.get("active", False),
        })
    settlements = []
    for suggestion in suggested_settlements(net):
        settlements.append({
            "from": member_by_id.get(suggestion["from_member_id"], {}).get("name", "Former member"),
            "to": member_by_id.get(suggestion["to_member_id"], {}).get("name", "Former member"),
            "from_member_id": suggestion["from_member_id"],
            "to_member_id": suggestion["to_member_id"],
            "amount": suggestion["amount"],
        })
    for row in settlement_rows:
        from_member = member_by_id.get(row.get("from_member_id"), {})
        to_member = member_by_id.get(row.get("to_member_id"), {})
        row["from_name"] = from_member.get("name") or row.get("from_person") or "Former member"
        row["to_name"] = to_member.get("name") or row.get("to_person") or "Former member"
        row["is_your_payment"] = row.get("from_member_id") == current_id
        row["is_your_receipt"] = row.get("to_member_id") == current_id
        row["can_mark_paid"] = row["is_your_payment"] or (trip_owner and not from_member.get("is_minto", True))
        row["can_undo_paid"] = row["is_your_payment"] or (trip_owner and not from_member.get("is_minto", True))
        if not row["is_your_payment"]:
            row.pop("from_transaction_id", None)
        if not row["is_your_receipt"]:
            row.pop("to_transaction_id", None)
    pending_settlements = [s for s in settlement_rows if s.get("status") == "pending"]
    paid_settlements = [s for s in settlement_rows if s.get("status") == "paid"]

    filter_category = request.args.get("category", "").strip().lower()
    filter_payer = request.args.get("payer", "").strip()
    filter_q = request.args.get("q", "").strip()
    filter_from = parse_iso_date(request.args.get("from"))
    filter_to = parse_iso_date(request.args.get("to"))

    filtered_expenses = []
    for e in expenses:
        if filter_category and e.get("category", "other") != filter_category:
            continue
        if filter_payer and str(e.get("payer_member_id")) != filter_payer and e.get("paid_by") != filter_payer:
            continue
        if filter_q and filter_q.lower() not in (e.get("description") or "").lower():
            continue
        d = parse_iso_date(e.get("expense_date"))
        if filter_from and (not d or d < filter_from):
            continue
        if filter_to and (not d or d > filter_to):
            continue
        filtered_expenses.append(e)

    date_summary = trip_date_summary(trip, today)
    budget = float(trip.get("budget") or 0)
    budget_remaining = max(budget - total_paise / 100, 0) if budget > 0 else None
    budget_over = max(total_paise / 100 - budget, 0) if budget > 0 else 0
    budget_pct = min(round((total_paise / 100 / budget) * 100, 1), 100) if budget > 0 else None

    return render_template(
        "trip_detail.html",
        trip=trip,
        friends=friends,
        people=people,
        members=members,
        current_member_id=current_id,
        trip_owner=trip_owner,
        expense_accounts=get_active_sources(client),
        expenses=filtered_expenses,
        all_expense_count=len(expenses),
        filtered_expense_count=len(filtered_expenses),
        total=total_paise / 100,
        people_rows=people_rows,
        settlements=settlements,
        pending_settlements=pending_settlements,
        paid_settlements=paid_settlements,
        trip_settlements=settlement_rows,
        your_share=owed.get(current_id, 0) / 100,
        your_paid=paid.get(current_id, 0) / 100,
        date_summary=date_summary,
        budget=budget,
        budget_remaining=budget_remaining,
        budget_over=budget_over,
        budget_pct=budget_pct,
        filter_category=filter_category,
        filter_payer=filter_payer,
        filter_q=filter_q,
        filter_from=filter_from,
        filter_to=filter_to,
        trip_expense_categories=TRIP_EXPENSE_CATEGORIES,
        today=today.isoformat(),
    )


@app.route("/withdraw-cash", methods=["GET", "POST"])
@login_required
def withdraw_cash():
    """Record cash withdrawn from a bank account as a linked pair of transfer
    legs: money OUT of a savings source and the same amount IN to a cash
    source — the only way cash is meant to increase in this app. (Being
    handed cash directly by someone is different — that's just an ordinary
    income or lending-repayment entry with Cash as the source; no pairing
    needed there since no bank balance is meant to drop alongside it.)

    Without this route, someone would have to create both legs by hand and
    could easily create only one — inflating cash on hand with no matching
    drop in the bank balance it actually came from.
    """
    client = get_user_client()
    user_id = session["user_id"]

    sources = (
        client.table("user_sources")
        .select("*")
        .eq("active", True)
        .order("name")
        .execute()
        .data
    )
    bank_sources = [s for s in sources if s["source_type"] == "savings"]
    cash_sources = [s for s in sources if s["source_type"] == "cash"]

    def render_form():
        return render_template(
            "withdraw_cash.html",
            bank_sources=bank_sources,
            cash_sources=cash_sources,
            form=request.form,
        )

    if request.method == "POST":
        amount = parse_money(request.form.get("amount"))
        from_source_id = request.form.get("from_source_id")
        to_source_id = request.form.get("to_source_id")
        notes = (request.form.get("notes") or "").strip() or None

        if amount is None:
            flash("Enter an amount greater than zero.")
            return render_form()
        if (from_source_id not in {str(s["id"]) for s in bank_sources}
                or to_source_id not in {str(s["id"]) for s in cash_sources}):
            flash("Pick a bank account to take it from and the cash account it goes into.")
            return render_form()

        transfer_group = str(uuid.uuid4())
        description = notes or "Cash withdrawal"

        def leg(direction, source_id):
            return insert_entry_and_transaction(client, user_id, description, {
                "direction": direction,
                "category": "transfer",
                "source_id": int(source_id),
                "amount": amount,
                "description": description,
                "raw_text": description,
                "transfer_group": transfer_group,
            })

        out_entry_id = None
        try:
            out_entry_id = leg("out", from_source_id)
            leg("in", to_source_id)
        except Exception:
            if out_entry_id is not None:
                try:
                    client.table("entries").delete().eq("id", out_entry_id).eq("user_id", user_id).execute()
                except Exception:
                    pass
            flash("Couldn't record the cash out. Please try again.")
            return render_form()

        flash("Cash out recorded. Bank and cash balances both updated.")
        return redirect(url_for("sources"))

    return render_form()



@app.route("/bank-transfer", methods=["GET", "POST"])
@login_required
def bank_transfer():
    """Move money between two of your own bank accounts. Recorded as two linked
    transfer entries (out of one, into the other), so both balances update and
    it never shows up as spending or income."""
    client = get_user_client()
    user_id = session["user_id"]

    sources = (
        client.table("user_sources")
        .select("*")
        .eq("active", True)
        .eq("source_type", "savings")
        .order("name")
        .execute()
        .data
    )

    def render_form():
        return render_template("bank_transfer.html", bank_sources=sources, form=request.form)

    if request.method == "POST":
        amount = parse_money(request.form.get("amount"))
        from_source_id = request.form.get("from_source_id")
        to_source_id = request.form.get("to_source_id")
        notes = (request.form.get("notes") or "").strip() or None
        bank_ids = {str(s["id"]) for s in sources}

        if amount is None:
            flash("Enter an amount greater than zero.")
            return render_form()
        if from_source_id not in bank_ids or to_source_id not in bank_ids:
            flash("Pick the bank account to send from and the one to send to.")
            return render_form()
        if from_source_id == to_source_id:
            flash("Choose two different bank accounts.")
            return render_form()

        transfer_group = str(uuid.uuid4())
        description = notes or "Bank transfer"

        def leg(direction, source_id):
            return insert_entry_and_transaction(client, user_id, description, {
                "direction": direction,
                "category": "transfer",
                "source_id": int(source_id),
                "amount": amount,
                "description": description,
                "raw_text": description,
                "transfer_group": transfer_group,
            })

        out_entry_id = None
        try:
            out_entry_id = leg("out", from_source_id)
            leg("in", to_source_id)
        except Exception:
            # Without the second leg, don't leave a dangling half-transfer.
            if out_entry_id is not None:
                try:
                    client.table("entries").delete().eq("id", out_entry_id).eq("user_id", user_id).execute()
                except Exception:
                    pass
            flash("Couldn't record the bank transfer. Please try again.")
            return render_form()

        flash("Bank transfer recorded. Both account balances updated.")
        return redirect(url_for("sources"))

    return render_form()


def compute_source_balances(client, user_id):
    """All-time balances/outstanding per source. Not period-scoped —
    these are running totals since the source was created, not tied to
    whatever date range the dashboard's period filter is showing."""
    all_sources = (
        client.table("user_sources")
        .select("*")
        .eq("active", True)
        .order("name")
        .execute()
        .data
    )

    all_txns = (
        client.table("transactions")
        .select("source_id, amount, direction, category, transaction_date, is_previous_card_bill, affects_source_balance, trip_expense_share_id, trip_share_status")
        .eq("user_id", user_id)
        .execute()
        .data
    )

    flows = defaultdict(lambda: {"in": 0.0, "out": 0.0})
    for t in all_txns:
        sid = t.get("source_id")
        if sid is None or not t.get("amount") or t.get("affects_source_balance") is False:
            continue
        flows[sid][t["direction"]] += float(t["amount"])

    savings, credit_cards = [], []
    for s in all_sources:
        f = flows[s["id"]]
        opening = float(s.get("opening_balance") or 0)

        # Cash behaves exactly like a savings account for balance math — an
        # opening amount plus whatever's flowed in or out — it's only kept
        # visually separate (see sources.html) because it isn't a bank.
        if s["source_type"] in ("savings", "cash"):
            s["balance"] = opening + f["in"] - f["out"]
            s["minimum_balance"] = float(s.get("minimum_balance") or 0)
            s["below_minimum"] = (
                s["minimum_balance"] > 0 and s["balance"] < s["minimum_balance"]
            )
            savings.append(s)
        else:
            limit = float(s["credit_limit"]) if s.get("credit_limit") else 0
            outstanding = max(opening + f["out"] - f["in"], 0)
            s["outstanding"] = outstanding
            s["limit"] = limit
            s["limit_left"] = max(limit - outstanding, 0) if limit else None
            s["limit_pct"] = round((outstanding / limit) * 100, 1) if limit else None
            credit_cards.append(s)

    return savings, credit_cards, all_txns


def compute_net_worth(all_txns, savings, credit_cards, manual_items=None, trip_payables=0):
    total_savings = sum(s["balance"] for s in savings)
    total_cc_debt = sum(s["outstanding"] for s in credit_cards)
    total_cc_limit = sum(s["limit"] for s in credit_cards if s.get("limit"))
    overall_utilization_pct = (
        round((total_cc_debt / total_cc_limit) * 100, 1) if total_cc_limit else None
    )

    invested_out = sum(
        float(t["amount"]) for t in all_txns
        if t.get("category") == "investment" and t.get("direction") == "out" and t.get("amount")
    )
    invested_in = sum(
        float(t["amount"]) for t in all_txns
        if t.get("category") == "investment" and t.get("direction") == "in" and t.get("amount")
    )
    total_invested = invested_out - invested_in

    def counts_as_lending_asset(transaction):
        if transaction.get("trip_expense_share_id") is not None:
            return transaction.get("trip_share_status") in ("accepted", "settled")
        return True

    lent_out = sum(
        float(t["amount"]) for t in all_txns
        if t.get("category") == "lending" and t.get("direction") == "out" and t.get("amount") and counts_as_lending_asset(t)
    )
    lent_in = sum(
        float(t["amount"]) for t in all_txns
        if t.get("category") == "lending" and t.get("direction") == "in" and t.get("amount") and counts_as_lending_asset(t)
    )
    total_lent = lent_out - lent_in

    manual_items = manual_items or []
    manual_assets, manual_liabilities = compute_net_worth_manual_totals(manual_items)

    return {
        "total_savings": total_savings,
        "total_invested": total_invested,
        "total_lent": total_lent,
        "trip_payables": float(trip_payables or 0),
        "total_cc_debt": total_cc_debt,
        "manual_assets": manual_assets,
        "manual_liabilities": manual_liabilities,
        "overall_utilization_pct": overall_utilization_pct,
        "net_worth": (
            total_savings
            + total_invested
            + total_lent
            + manual_assets
            - total_cc_debt
            - manual_liabilities
            - float(trip_payables or 0)
        ),
    }


def get_salary_cycle(profile_data, today=None):
    """Return the user's current salary-cycle dates without treating future
    salary as money already available."""
    today = today or datetime.now(APP_TZ).date()
    salary = float(profile_data.get("monthly_salary") or 0)
    salary_day = profile_data.get("salary_day")
    if not salary or not salary_day:
        return None

    salary_day = int(salary_day)

    def salary_date(year, month):
        return date(
            year,
            month,
            min(salary_day, calendar.monthrange(year, month)[1]),
        )

    current_salary = salary_date(today.year, today.month)
    if today >= current_salary:
        cycle_start = current_salary
        if today.month == 12:
            next_salary = salary_date(today.year + 1, 1)
        else:
            next_salary = salary_date(today.year, today.month + 1)
    else:
        if today.month == 1:
            cycle_start = salary_date(today.year - 1, 12)
        else:
            cycle_start = salary_date(today.year, today.month - 1)
        next_salary = current_salary

    return {
        "monthly_salary": salary,
        "salary_day": salary_day,
        "cycle_start": cycle_start,
        "next_salary": next_salary,
        "days_to_salary": max((next_salary - today).days, 0),
    }


def get_category_budget_status(client, user_id, all_txns, today=None):
    """Current-month budget status. Missing budgets are simply omitted."""
    today = today or datetime.now(APP_TZ).date()
    month_start = today.replace(day=1)
    try:
        budgets = (
            client.table("category_budgets")
            .select("*")
            .eq("user_id", user_id)
            .eq("month_start", month_start.isoformat())
            .order("category")
            .execute()
            .data
        )
    except Exception:
        app.logger.exception("Category budgets unavailable")
        return []

    spent = defaultdict(float)
    for txn in all_txns:
        d = parse_iso_date(txn.get("transaction_date"))
        if (
            d
            and d.year == today.year
            and d.month == today.month
            and txn.get("category") == "expense"
            and txn.get("amount")
        ):
            spent[txn.get("expense_category") or "other"] += float(txn["amount"])

    status = []
    for budget in budgets:
        limit = float(budget.get("amount") or 0)
        used = spent.get(budget.get("category"), 0.0)
        status.append({
            **budget,
            "spent": round(used, 2),
            "remaining": round(limit - used, 2),
            "percent": round((used / limit) * 100, 1) if limit else 0,
        })
    return status


def get_due_soon_commitments(commitments, today=None, days_before=5):
    today = today or datetime.now(APP_TZ).date()
    horizon = today + timedelta(days=max(int(days_before or 5), 1))
    return [
        item for item in commitments
        if item.get("due_date") and today <= item["due_date"] <= horizon
    ]


def get_dashboard_alerts(savings, credit_cards, txns):
    """Plain, threshold-based notices for the top of Overview."""
    alerts = []

    for source in savings:
        if source.get("source_type") == "savings" and source.get("below_minimum"):
            alerts.append({
                "kind": "warning",
                "title": f"{source['name']} is below its minimum balance",
                "message": (
                    f"Current balance is {inr_filter(source['balance'])}. "
                    f"Keep at least {inr_filter(source['minimum_balance'])} in this account."
                ),
            })

    # A card near its limit is worth a warning even if nothing new was spent
    # on it this period.
    near_limit = [
        c for c in credit_cards
        if c.get("limit_pct") is not None and c["limit_pct"] >= CC_UTILIZATION_WARNING
    ]
    if near_limit:
        detail = ", ".join(f"{c['name']} ({c['limit_pct']:.0f}% used)" for c in near_limit[:3])
        alerts.append({
            "kind": "warning",
            "title": "Credit card close to its limit",
            "message": f"{detail}. Consider paying the bill down soon.",
        })

    period_expenses = sum(
        float(t.get("amount") or 0) for t in txns
        if t.get("category") == "expense" and t.get("amount")
    )
    card_spend = sum(
        float(t["amount"]) for t in txns
        if t.get("category") == "expense" and t.get("amount")
        and (t.get("user_sources") or {}).get("source_type") == "credit_card"
    )
    share = (card_spend / period_expenses * 100) if period_expenses else 0
    if card_spend and share >= CC_SPEND_SHARE_WARNING:
        alerts.append({
            "kind": "warning",
            "title": "High credit-card spending",
            "message": (
                f"Credit-card purchases are {share:.0f}% of your expenses in this period. "
                "Review them and make sure the balance stays manageable."
            ),
        })

    return alerts


def fixed_expense_due_date(year, month, due_day):
    """Return this month's due date, clamping 29-31 to the month's last day."""
    return date(year, month, min(int(due_day), calendar.monthrange(year, month)[1]))


def get_fixed_expenses_for_month(client, user_id, year=None, month=None):
    try:
        return _get_fixed_expenses_for_month(client, user_id, year, month)
    except Exception:
        app.logger.exception("Fixed expenses unavailable (has the database update been run?)")
        return []


def get_fixed_incomes_for_month(client, user_id, year=None, month=None):
    """Return this month's recurring income items and whether each was received."""
    today = datetime.now(APP_TZ).date()
    year = year or today.year
    month = month or today.month
    month_start = date(year, month, 1)
    try:
        rows = (
            client.table("fixed_incomes")
            .select("*, user_sources(name, source_type)")
            .eq("user_id", user_id)
            .eq("active", True)
            .order("due_day")
            .execute()
            .data
        )
        payments = (
            client.table("fixed_income_payments")
            .select("fixed_income_id, transaction_id, received_at")
            .eq("user_id", user_id)
            .eq("due_month", month_start.isoformat())
            .execute()
            .data
        )
    except Exception:
        app.logger.exception("Fixed income unavailable (has the database update been run?)")
        return []
    paid_by_id = {p["fixed_income_id"]: p for p in payments}
    result = []
    for row in rows:
        item = dict(row)
        item["due_date"] = fixed_expense_due_date(year, month, row["due_day"])
        item["payment"] = paid_by_id.get(row["id"])
        item["paid"] = bool(item["payment"])
        result.append(item)
    return result


def _get_fixed_expenses_for_month(client, user_id, year=None, month=None):
    today = datetime.now(APP_TZ).date()
    year = year or today.year
    month = month or today.month
    month_start = date(year, month, 1)
    rows = (
        client.table("fixed_expenses")
        .select("*, user_sources(name, source_type)")
        .eq("user_id", user_id)
        .eq("active", True)
        .order("due_day")
        .execute()
        .data
    )
    payments = (
        client.table("fixed_expense_payments")
        .select("fixed_expense_id, transaction_id, paid_at")
        .eq("user_id", user_id)
        .eq("due_month", month_start.isoformat())
        .execute()
        .data
    )
    paid_by_id = {p["fixed_expense_id"]: p for p in payments}
    result = []
    for row in rows:
        due = fixed_expense_due_date(year, month, row["due_day"])
        paid = paid_by_id.get(row["id"])
        item = dict(row)
        item["due_date"] = due
        item["paid"] = bool(paid)
        item["payment"] = paid
        result.append(item)
    return result


@app.route("/budgets", methods=["GET", "POST"])
@login_required
def budgets():
    client = get_user_client()
    user_id = session["user_id"]
    today = datetime.now(APP_TZ).date()
    month_start = today.replace(day=1)

    expense_categories = get_categories(client, user_id, "expense", FIXED_EXPENSE_CATEGORIES)

    if request.method == "POST":
        category = (request.form.get("category") or "").strip()
        amount = parse_money(request.form.get("amount"))

        if category not in expense_categories:
            flash("Pick a valid expense category.")
            return redirect(url_for("budgets"))
        if amount is None:
            flash("Enter a budget amount greater than zero.")
            return redirect(url_for("budgets"))

        try:
            client.table("category_budgets").upsert({
                "user_id": user_id,
                "month_start": month_start.isoformat(),
                "category": category,
                "amount": amount,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="user_id,month_start,category").execute()
            flash(f"{category.replace('_', ' ').title()} budget saved for {today.strftime('%B %Y')}.")
        except Exception:
            app.logger.exception("Could not save category budget")
            flash("Couldn't save the budget. Please run the latest database update first.")
        return redirect(url_for("budgets"))

    savings, credit_cards, all_txns = compute_source_balances(client, user_id)
    status = get_category_budget_status(client, user_id, all_txns, today)
    return render_template(
        "budgets.html",
        today=today,
        month_label=today.strftime("%B %Y"),
        categories=expense_categories,
        budgets=status,
    )


@app.route("/budgets/<int:budget_id>/delete", methods=["POST"])
@login_required
def delete_budget(budget_id):
    client = get_user_client()
    user_id = session["user_id"]
    try:
        client.table("category_budgets").delete().eq("id", budget_id).eq("user_id", user_id).execute()
        flash("Budget removed.")
    except Exception:
        flash("Couldn't remove that budget.")
    return redirect(url_for("budgets"))


@app.route("/fixed-expenses", methods=["GET", "POST"])
@login_required
def fixed_expenses():
    client = get_user_client()
    user_id = session["user_id"]

    expense_categories = get_categories(client, user_id, "expense", FIXED_EXPENSE_CATEGORIES)
    investment_categories = get_categories(client, user_id, "investment", FIXED_INVESTMENT_CATEGORIES)
    categories = expense_categories + [c for c in investment_categories if c not in expense_categories]

    if request.method == "POST":
        name = request.form.get("name", "").strip()[:60]
        amount = parse_money(request.form.get("amount"))
        category = (request.form.get("category") or "other").strip()
        # The form posts "kind:category" (for example "investment:sip"), so a
        # name like "Other" that exists in both lists can never be mistaken
        # for the wrong one. A plain value (older page) counts as an expense.
        kind, _, picked = category.partition(":")
        if not picked:
            kind, picked = "expense", category
        if kind not in ("expense", "investment"):
            kind, picked = "expense", category
        category = picked
        source_id = request.form.get("source_id") or None
        try:
            due_day = int(request.form.get("due_day", "").strip())
        except ValueError:
            due_day = 0
        pay_ids = {r["id"] for r in client.table("user_sources").select("id, source_type")
                   .in_("source_type", ["savings", "cash"]).execute().data}
        if not name or amount is None or not 1 <= due_day <= 31:
            flash("Couldn't add that fixed expense. Check the name, amount and due day.")
        elif category not in (investment_categories if kind == "investment" else expense_categories):
            flash("Pick one of the listed categories.")
        elif source_id and (not source_id.isdigit() or int(source_id) not in pay_ids):
            flash("Pick one of your bank or cash accounts.")
        else:
            row = {
                "user_id": user_id,
                "name": name,
                "amount": amount,
                "due_day": due_day,
                "category": category,
                "kind": kind,
                "source_id": int(source_id) if source_id else None,
            }
            try:
                try:
                    client.table("fixed_expenses").insert(row).execute()
                except Exception:
                    if kind != "expense":
                        raise
                    # Database not updated with the "kind" column yet: plain
                    # expenses still save without it.
                    row.pop("kind")
                    client.table("fixed_expenses").insert(row).execute()
                flash(f"Added {name} as a monthly {'SIP / investment' if kind == 'investment' else 'fixed expense'}.")
            except Exception:
                flash("Couldn't add that. Please try again."
                      + (" Run the latest database update first." if kind == "investment" else ""))
        return redirect(url_for("fixed_expenses"))

    today = datetime.now(APP_TZ).date()
    expenses = get_fixed_expenses_for_month(client, user_id, today.year, today.month)
    fixed_incomes = get_fixed_incomes_for_month(client, user_id, today.year, today.month)
    sources = (
        client.table("user_sources")
        .select("id, name, source_type")
        .eq("user_id", user_id)
        .eq("active", True)
        .in_("source_type", ["savings", "cash"])
        .order("name")
        .execute()
        .data
    )
    return render_template(
        "fixed_expenses.html",
        expenses=expenses,
        fixed_incomes=fixed_incomes,
        sources=sources,
        categories=categories,
        expense_categories=expense_categories,
        investment_categories=investment_categories,
        month_label=today.strftime("%B %Y"),
        today=today,
    )


@app.route("/fixed-expenses/<int:fixed_expense_id>/pay", methods=["POST"])
@login_required
def pay_fixed_expense(fixed_expense_id):
    client = get_user_client()
    user_id = session["user_id"]
    today = datetime.now(APP_TZ).date()
    month_start = today.replace(day=1)

    rows = (
        client.table("fixed_expenses")
        .select("*")
        .eq("id", fixed_expense_id)
        .eq("user_id", user_id)
        .eq("active", True)
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        flash("That fixed expense could not be found.")
        return redirect(url_for("fixed_expenses"))
    expense = rows[0]
    source_id = request.form.get("source_id") or expense.get("source_id")
    if not source_id:
        flash("Choose the account used to pay this expense.")
        return redirect(url_for("fixed_expenses"))
    pay_ids = {str(r["id"]) for r in client.table("user_sources").select("id")
               .in_("source_type", ["savings", "cash"]).execute().data}
    if str(source_id) not in pay_ids:
        flash("Pick one of your bank or cash accounts.")
        return redirect(url_for("fixed_expenses"))

    existing = (
        client.table("fixed_expense_payments")
        .select("id")
        .eq("fixed_expense_id", fixed_expense_id)
        .eq("due_month", month_start.isoformat())
        .limit(1)
        .execute()
        .data
    )
    if existing:
        flash("This fixed expense is already marked paid for this month.")
        return redirect(url_for("fixed_expenses"))

    entry_id = None
    try:
        is_investment = (expense.get("kind") or "expense") == "investment"
        entry_label = "Fixed investment" if is_investment else "Fixed expense"
        entry_id = insert_entry_and_transaction(
            client, user_id, f"{entry_label}: {expense['name']}", {
                "direction": "out",
                "category": expense.get("kind") or "expense",
                "expense_category": None if is_investment else (expense.get("category") or "other"),
                "investment_category": (expense.get("category") or "other") if is_investment else None,
                "source_id": int(source_id),
                "amount": float(expense["amount"]),
                "description": expense["name"],
                "raw_text": f"{entry_label}: {expense['name']}",
                "transaction_date": today.isoformat(),
            },
        )
        client.table("fixed_expense_payments").insert({
            "fixed_expense_id": fixed_expense_id,
            "user_id": user_id,
            "due_month": month_start.isoformat(),
            "transaction_id": entry_id,
        }).execute()
        destination = "investments" if is_investment else "expenses"
        flash(f"Marked {expense['name']} as paid and added it to your {destination}.")
    except Exception as e:
        # Undo the expense if the payment row could not be saved (for example
        # a double tap that lost the race against the unique rule), so the
        # same bill is never counted twice.
        if entry_id is not None:
            try:
                client.table("entries").delete().eq("id", entry_id).eq("user_id", user_id).execute()
            except Exception:
                pass
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            flash("This fixed expense is already marked paid for this month.")
        else:
            flash("Couldn't mark that fixed expense as paid. Please try again.")
    return redirect(url_for("fixed_expenses"))


@app.route("/fixed-expenses/<int:fixed_expense_id>/delete", methods=["POST"])
@login_required
def delete_fixed_expense(fixed_expense_id):
    client = get_user_client()
    user_id = session["user_id"]
    client.table("fixed_expenses").delete().eq("id", fixed_expense_id).eq("user_id", user_id).execute()
    flash("Fixed expense removed.")
    return redirect(url_for("fixed_expenses"))


@app.route("/fixed-income/add", methods=["POST"])
@login_required
def add_fixed_income():
    client = get_user_client()
    user_id = session["user_id"]
    name = request.form.get("name", "").strip()[:60]
    amount = parse_money(request.form.get("amount"))
    source_id = request.form.get("source_id") or None
    try:
        due_day = int(request.form.get("due_day", "").strip())
    except (TypeError, ValueError):
        due_day = 0

    valid_destinations = {
        str(r["id"]) for r in client.table("user_sources").select("id")
        .eq("active", True).in_("source_type", ["savings", "cash"]).execute().data
    }
    if not name or amount is None or not 1 <= due_day <= 31:
        flash("Enter an income name, a valid amount, and a due day from 1 to 31.")
    elif not source_id or str(source_id) not in valid_destinations:
        flash("Choose the bank or cash account where this income arrives.")
    else:
        try:
            client.table("fixed_incomes").insert({
                "user_id": user_id, "name": name, "amount": amount,
                "due_day": due_day, "source_id": int(source_id),
            }).execute()
            flash(f"Added {name} as recurring monthly income.")
        except Exception:
            app.logger.exception("Could not add fixed income")
            flash("Couldn't add recurring income. Check that the latest database update has been run.")
    return redirect(url_for("fixed_expenses"))


@app.route("/fixed-income/<int:fixed_income_id>/receive", methods=["POST"])
@login_required
def receive_fixed_income(fixed_income_id):
    client = get_user_client()
    user_id = session["user_id"]
    today = datetime.now(APP_TZ).date()
    month_start = today.replace(day=1)
    rows = (
        client.table("fixed_incomes").select("*")
        .eq("id", fixed_income_id).eq("user_id", user_id).eq("active", True)
        .limit(1).execute().data
    )
    if not rows:
        flash("That recurring income could not be found.")
        return redirect(url_for("fixed_expenses"))
    income = rows[0]
    source_id = request.form.get("source_id") or income.get("source_id")
    valid_destinations = {
        str(r["id"]) for r in client.table("user_sources").select("id")
        .eq("active", True).in_("source_type", ["savings", "cash"]).execute().data
    }
    if not source_id or str(source_id) not in valid_destinations:
        flash("Choose the bank or cash account where the income arrived.")
        return redirect(url_for("fixed_expenses"))
    existing = (
        client.table("fixed_income_payments").select("id")
        .eq("fixed_income_id", fixed_income_id).eq("due_month", month_start.isoformat())
        .limit(1).execute().data
    )
    if existing:
        flash("This recurring income is already recorded for this month.")
        return redirect(url_for("fixed_expenses"))

    entry_id = None
    try:
        entry_id = insert_entry_and_transaction(client, user_id, f"Fixed income: {income['name']}", {
            "direction": "in", "category": "income", "expense_category": None,
            "source_id": int(source_id), "amount": float(income["amount"]),
            "description": income["name"], "raw_text": f"Fixed income: {income['name']}",
            "transaction_date": today.isoformat(),
        })
        client.table("fixed_income_payments").insert({
            "fixed_income_id": fixed_income_id, "user_id": user_id,
            "due_month": month_start.isoformat(), "transaction_id": entry_id,
        }).execute()
        flash(f"Recorded {income['name']} as received and added it to your income.")
    except Exception as e:
        if entry_id is not None:
            try:
                client.table("entries").delete().eq("id", entry_id).eq("user_id", user_id).execute()
            except Exception:
                pass
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            flash("This recurring income is already recorded for this month.")
        else:
            app.logger.exception("Could not record recurring income")
            flash("Couldn't record that income. Please try again.")
    return redirect(url_for("fixed_expenses"))


@app.route("/fixed-income/<int:fixed_income_id>/delete", methods=["POST"])
@login_required
def delete_fixed_income(fixed_income_id):
    client = get_user_client()
    client.table("fixed_incomes").delete().eq("id", fixed_income_id).eq("user_id", session["user_id"]).execute()
    flash("Recurring income removed.")
    return redirect(url_for("fixed_expenses"))


@app.route("/api/fixed-reminders", methods=["GET"])
@login_required
def fixed_reminders():
    """Due/overdue monthly commitments for the persistent on-open reminder."""
    if session.get("mode") == "trip":
        return jsonify({"items": []})
    client = get_user_client()
    user_id = session["user_id"]
    today = datetime.now(APP_TZ).date()
    # Show commitments one day before they are due, and keep showing them
    # while overdue until their payment/receipt is recorded.
    reminder_through = today + timedelta(days=1)
    expenses = get_fixed_expenses_for_month(client, user_id, today.year, today.month)
    incomes = get_fixed_incomes_for_month(client, user_id, today.year, today.month)
    sources = (
        client.table("user_sources").select("id, name, source_type")
        .eq("user_id", user_id).eq("active", True)
        .in_("source_type", ["savings", "cash"]).order("name").execute().data
    )
    accounts = [{"id": str(s["id"]), "name": s["name"]} for s in sources]
    items = []
    for item in expenses:
        if item.get("paid") or item["due_date"] > reminder_through:
            continue
        items.append({
            "id": int(item["id"]), "name": item["name"], "amount": float(item["amount"]),
            "kind": item.get("kind") or "expense", "due_date": item["due_date"].isoformat(),
            "source_id": str(item.get("source_id") or ""), "accounts": accounts,
        })
    for item in incomes:
        if item.get("paid") or item["due_date"] > reminder_through:
            continue
        items.append({
            "id": int(item["id"]), "name": item["name"], "amount": float(item["amount"]),
            "kind": "income", "due_date": item["due_date"].isoformat(),
            "source_id": str(item.get("source_id") or ""), "accounts": accounts,
        })
    items.sort(key=lambda item: (item["due_date"], item["kind"], item["name"].lower()))
    return jsonify({"items": items, "today": today.isoformat()})


@app.route("/sources/<int:source_id>/cc-loan", methods=["POST"])
@login_required
def update_cc_loan(source_id):
    client = get_user_client()
    user_id = session["user_id"]
    action = request.form.get("loan_action", "save")

    card = (
        client.table("user_sources")
        .select("id, source_type")
        .eq("id", source_id)
        .eq("user_id", user_id)
        .eq("active", True)
        .limit(1)
        .execute()
        .data
    )
    if not card or card[0]["source_type"] != "credit_card":
        flash("That credit card could not be found.")
        return redirect(url_for("sources"))

    try:
        if action == "remove":
            client.table("credit_card_loans").update({
                "active": False,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("source_id", source_id).eq("user_id", user_id).eq("active", True).execute()
            flash("CC loan / EMI removed.")
            return redirect(url_for("sources"))

        original_amount = parse_money(request.form.get("original_amount"))
        outstanding_amount = parse_money(request.form.get("outstanding_amount"), allow_zero=True)
        monthly_emi = parse_money(request.form.get("monthly_emi"))
        start_date = parse_iso_date(request.form.get("start_date"))
        if start_date is None:
            start_date = datetime.now(APP_TZ).date()

        if original_amount is None or outstanding_amount is None or monthly_emi is None:
            flash("Enter the original amount, current outstanding amount and monthly EMI.")
            return redirect(url_for("sources"))

        if outstanding_amount > original_amount:
            flash("Current loan outstanding cannot be greater than the original amount.")
            return redirect(url_for("sources"))

        name = " ".join((request.form.get("loan_name") or "Credit card loan").split())[:80]
        row = {
            "user_id": user_id,
            "source_id": source_id,
            "name": name or "Credit card loan",
            "original_amount": original_amount,
            "outstanding_amount": outstanding_amount,
            "monthly_emi": monthly_emi,
            "start_date": start_date.isoformat(),
            "active": outstanding_amount > 0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        existing = (
            client.table("credit_card_loans")
            .select("id")
            .eq("source_id", source_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
            .data
        )

        if existing:
            client.table("credit_card_loans").update(row).eq(
                "id", existing[0]["id"]
            ).eq("user_id", user_id).execute()
        else:
            client.table("credit_card_loans").insert(row).execute()

        flash("CC loan / EMI details saved.")
    except Exception:
        app.logger.exception("Could not save CC loan")
        flash("Couldn't save the CC loan details. Please run the database update first.")

    return redirect(url_for("sources"))


@app.route("/sources/<int:source_id>/card-cycle", methods=["POST"])
@login_required
def update_card_cycle(source_id):
    client = get_user_client()
    user_id = session["user_id"]
    try:
        statement_day = int(request.form.get("statement_day", "").strip())
        payment_due_days = int(request.form.get("payment_due_days", "").strip())
    except (TypeError, ValueError):
        flash("Enter a valid statement day and days until payment.")
        return redirect(url_for("sources"))

    if not 1 <= statement_day <= 31 or not 0 <= payment_due_days <= 60:
        flash("Statement day must be 1–31 and payment due days must be 0–60.")
        return redirect(url_for("sources"))

    try:
        client.table("user_sources").update({
            "statement_day": statement_day,
            "payment_due_days": payment_due_days,
            "billing_cycle_enabled": True,
        }).eq("id", source_id).eq("user_id", user_id).eq("source_type", "credit_card").execute()
        flash("Credit-card billing cycle updated.")
    except Exception:
        flash("Couldn't save the card cycle. Run the latest database update first.")
    return redirect(url_for("sources"))


@app.route("/sources", methods=["GET", "POST"])
@login_required
def sources():
    client = get_user_client()
    user_id = session["user_id"]

    if request.method == "POST":
        name = request.form.get("name", "").strip()[:60]
        source_type = request.form.get("source_type")
        if not name or source_type not in ("savings", "cash", "credit_card"):
            flash("Add a name and choose an account type.")
            return redirect(url_for("sources"))

        row = {"user_id": user_id, "name": name, "source_type": source_type}
        bad = False
        if source_type == "savings":
            opening = parse_money(request.form.get("opening_balance"), allow_zero=True, default=0.0)
            minimum = parse_money(request.form.get("minimum_balance"), allow_zero=True, default=0.0)
            bad = opening is None or minimum is None
            row["opening_balance"], row["minimum_balance"] = opening, minimum
        elif source_type == "cash":
            opening = parse_money(request.form.get("cash_opening_balance"), allow_zero=True, default=0.0)
            bad = opening is None
            row["opening_balance"] = opening
        else:
            limit = parse_money(request.form.get("credit_limit"))
            outstanding = parse_money(request.form.get("outstanding"), allow_zero=True, default=0.0)
            try:
                statement_day = int(request.form.get("statement_day", "").strip()) if request.form.get("statement_day", "").strip() else None
                payment_due_days = int(request.form.get("payment_due_days", "").strip()) if request.form.get("payment_due_days", "").strip() else None
            except ValueError:
                statement_day, payment_due_days = None, None
            bad = (
                outstanding is None
                or (request.form.get("credit_limit") and limit is None)
                or (statement_day is not None and not 1 <= statement_day <= 31)
                or (payment_due_days is not None and not 0 <= payment_due_days <= 60)
            )
            row["credit_limit"] = limit
            row["opening_balance"] = outstanding
            row["statement_day"] = statement_day
            row["payment_due_days"] = payment_due_days
            row["billing_cycle_enabled"] = bool(statement_day is not None)
        if bad:
            flash("Check the amounts. They must be plain numbers, zero or more.")
            return redirect(url_for("sources"))
        try:
            client.table("user_sources").insert(row).execute()
        except Exception as e:
            if "duplicate key" in str(e).lower():
                flash(f"You already have an account named '{name}'.")
            else:
                flash("Couldn't add that account. Please try again.")
        return redirect(url_for("sources"))

    savings, credit_cards, _ = compute_source_balances(client, user_id)
    cash = [s for s in savings if s["source_type"] == "cash"]
    savings = [s for s in savings if s["source_type"] == "savings"]
    cc_loans = get_active_cc_loans(client, user_id)
    return render_template(
        "sources.html",
        savings=savings,
        cash=cash,
        credit_cards=credit_cards,
        cc_loans=cc_loans,
        today=datetime.now(APP_TZ).date().isoformat(),
    )


@app.template_filter("inr")
def inr_filter(value):
    """12345.6 -> ₹12,345.60 (Indian digit grouping, sign in front)."""
    try:
        x = round(float(value), 2)
    except (TypeError, ValueError):
        x = 0.0
    whole, frac = f"{abs(x):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return ("-" if x < 0 else "") + "₹" + whole + "." + frac


@app.template_filter("nice_date")
def nice_date(value):
    """Format ISO date strings and Python date/datetime values for UI."""
    if not value:
        return ""
    if isinstance(value, datetime):
        d = value.date()
    elif isinstance(value, date):
        d = value
    else:
        try:
            d = datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return str(value)
    return d.strftime("%d-%b-%Y").upper()


def get_period_start(period):
    now = datetime.now(APP_TZ)
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        return now - timedelta(days=7)
    elif period == "year":
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "all":
        return None
    else:  # month is the default
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@app.route("/dashboard")
@login_required
def dashboard():
    client = get_user_client()
    user_id = session["user_id"]
    period = request.args.get("period", "month")
    if period not in ("today", "week", "month", "year", "all"):
        period = "month"

    query = (
        client.table("transactions")
        .select("*, user_sources(name, source_type)")
        .eq("user_id", user_id)
    )
    start = get_period_start(period)
    if start:
        query = query.gte("transaction_date", start.date().isoformat())
    txns = query.order("transaction_date", desc=True).order("created_at", desc=True).execute().data

    # Transfers (e.g. a credit-card bill payment moving money from savings to
    # the card) and lending (money handed to a friend, or repaid by one)
    # aren't real income or spending — they just move money between your own
    # sources, or convert cash into a receivable you'll get back — so both
    # are excluded from these period totals to avoid inflating "money in/out"
    # with money that never actually left your net worth.
    # Trip payments and settlements change account balances, but the linked
    # share transaction is the consumption entry. Excluding these categories
    # keeps Dashboard and reports from counting the full bill twice.
    non_flow_categories = ("transfer", "lending", "trip_expense_payment", "trip_settlement")
    total_in = sum(
        float(t["amount"]) for t in txns
        if t["direction"] == "in" and t["amount"] and t["category"] not in non_flow_categories
    )
    total_out = sum(
        float(t["amount"]) for t in txns
        if t["direction"] == "out" and t["amount"] and t["category"] not in non_flow_categories
    )
    net = total_in - total_out

    expense_by_category = defaultdict(float)
    for t in txns:
        if t["category"] == "expense" and t["amount"]:
            key = t.get("expense_category") or "other"
            expense_by_category[key] += float(t["amount"])

    spend_by_source = defaultdict(float)
    for t in txns:
        source = t.get("user_sources")
        if source and t["amount"] and t["direction"] == "out" and t["category"] not in non_flow_categories:
            spend_by_source[source["name"]] += float(t["amount"])

    recent = txns[:5]

    savings, credit_cards, all_txns = compute_source_balances(client, user_id)
    manual_items = get_net_worth_manual_items() if net_worth_is_unlocked() else []
    try:
        profile_settings_rows = client.table("profiles").select("monthly_salary, salary_day, reminder_days_before").eq("id", user_id).limit(1).execute().data
        profile_settings = profile_settings_rows[0] if profile_settings_rows else {}
    except Exception:
        profile_settings = {}
    trip_payables = get_trip_payables(client, user_id)
    wealth = compute_net_worth(all_txns, savings, credit_cards, manual_items, trip_payables)

    # Fixed monthly commitments plus expected CC statements form the dashboard's
    # future obligations. They are independent of the period tabs.
    today = datetime.now(APP_TZ).date()
    cc_loans = get_active_cc_loans(client, user_id)
    card_forecasts = get_credit_card_forecasts(credit_cards, all_txns, today, cc_loans)
    try:
        upcoming_commitments = get_upcoming_commitments(client, user_id, card_forecasts)
    except Exception:
        app.logger.exception("Upcoming commitments could not be loaded")
        upcoming_commitments = []

    salary_cycle = get_salary_cycle(profile_settings, today)
    category_budgets = get_category_budget_status(client, user_id, all_txns, today)
    reminder_days_before = int(profile_settings.get("reminder_days_before") or 5)
    due_soon_commitments = get_due_soon_commitments(
        upcoming_commitments,
        today,
        reminder_days_before,
    )

    # Safe to Spend reserves only commitments still due during the current
    # calendar month. Future-month expenses should not reduce this month's
    # spendable balance.
    month_end = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    safe_spend_commitments = [
        x for x in upcoming_commitments
        if (
            x.get("due_date")
            and x["due_date"].year == today.year
            and x["due_date"].month == today.month
            and x["due_date"] <= month_end
        )
    ]

    try:
        fixed_expenses = get_fixed_expenses_for_month(client, user_id, today.year, today.month)
    except Exception:
        # An extra on this page: if its tables are missing or unreachable, the
        # Overview should still open instead of showing an error page.
        app.logger.exception("Fixed expenses could not be loaded for the Overview")
        fixed_expenses = []
    fixed_total = sum(
        float(x["amount"] or 0) for x in fixed_expenses
        if not x["paid"] and (x.get("kind") or "expense") == "expense"
    )
    fixed_investment_total = sum(
        float(x["amount"] or 0) for x in fixed_expenses
        if not x["paid"] and (x.get("kind") or "expense") == "investment"
    )
    fixed_paid_total = sum(float(x["amount"] or 0) for x in fixed_expenses if x["paid"])
    fixed_commitments_total = fixed_total + fixed_investment_total
    fixed_bank_cash = float(wealth["total_savings"])
    fixed_remaining = max(round(fixed_bank_cash - fixed_commitments_total, 2), 0.0)
    fixed_shortfall = max(round(fixed_commitments_total - fixed_bank_cash, 2), 0.0)
    safe_to_spend = compute_safe_to_spend(savings, safe_spend_commitments)
    safe_spend_bank_cash = sum(float(s.get("balance") or 0) for s in savings)
    safe_spend_committed = sum(float(x.get("amount") or 0) for x in safe_spend_commitments)
    safe_spend_shortfall = max(round(safe_spend_committed - safe_spend_bank_cash, 2), 0.0)
    safe_to_spend_after_salary = (
        round(safe_to_spend + salary_cycle["monthly_salary"], 2)
        if salary_cycle else None
    )
    safe_spend_fixed = sum(
        float(x.get("amount") or 0)
        for x in safe_spend_commitments
        if x.get("kind") == "fixed"
    )
    safe_spend_investments = sum(
        float(x.get("amount") or 0)
        for x in safe_spend_commitments
        if x.get("kind") == "investment"
    )
    safe_spend_cc = sum(
        float(x.get("amount") or 0)
        for x in safe_spend_commitments
        if x.get("kind") == "credit_card"
    )
    alerts = get_dashboard_alerts(savings, credit_cards, txns)

    expected_cc_total = sum(
        float(c.get("expected_bill") or 0) for c in card_forecasts
    )
    if expected_cc_total > 0:
        alerts.append({
            "kind": "info",
            "title": "Expected credit-card bills",
            "message": f"Upcoming statement bills are {inr_filter(expected_cc_total)} based on current billing-cycle spending.",
        })

    # Outstanding loans by person — all-time, like net worth, not scoped to
    # the period tabs. Only people with a nonzero balance are shown; fully
    # repaid loans (out - in == 0) drop off automatically.
    _, outstanding_loans = get_lending_summary(client, user_id)

    return render_template(
        "dashboard.html",
        period=period,
        total_in=total_in,
        total_out=total_out,
        net=net,
        txn_count=len(txns),
        expense_labels=list(expense_by_category.keys()),
        expense_values=list(expense_by_category.values()),
        source_labels=list(spend_by_source.keys()),
        source_values=list(spend_by_source.values()),
        recent=recent,
        wealth=wealth,
        wealth_unlocked=net_worth_is_unlocked(),
        outstanding_loans=outstanding_loans,
        fixed_expenses=fixed_expenses,
        fixed_total=fixed_total,
        fixed_investment_total=fixed_investment_total,
        fixed_paid_total=fixed_paid_total,
        card_forecasts=card_forecasts,
        expected_cc_total=expected_cc_total,
        upcoming_commitments=upcoming_commitments,
        safe_to_spend=safe_to_spend,
        fixed_commitments_total=fixed_commitments_total,
        fixed_remaining=fixed_remaining,
        fixed_shortfall=fixed_shortfall,
        safe_spend_bank_cash=safe_spend_bank_cash,
        safe_spend_shortfall=safe_spend_shortfall,
        safe_spend_fixed=safe_spend_fixed,
        safe_spend_investments=safe_spend_investments,
        safe_spend_cc=safe_spend_cc,
        salary_cycle=salary_cycle,
        safe_to_spend_after_salary=safe_to_spend_after_salary,
        category_budgets=category_budgets,
        due_soon_commitments=due_soon_commitments,
        reminder_days_before=reminder_days_before,
        alerts=alerts,
        report_from=datetime.now(APP_TZ).date().replace(day=1).isoformat(),
        report_to=datetime.now(APP_TZ).date().isoformat(),
    )


@app.route("/sw.js")
def service_worker():
    # Served from the site root (not /static/) so the service worker's scope
    # covers the whole app, which installing to a home screen requires.
    response = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/manifest.json")
def web_manifest():
    response = send_from_directory(app.static_folder, "manifest.json", mimetype="application/manifest+json")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/favicon.ico")
def favicon():
    # Some browsers request /favicon.ico by convention no matter what the
    # <head> <link> tags say — this is what was showing up as harmless but
    # noisy 404s in the deploy logs before.
    return redirect(url_for("static", filename="favicon.svg"))


@app.route("/health")
def health():
    return "OK"


if __name__ == "__main__":
    app.run(debug=True)
