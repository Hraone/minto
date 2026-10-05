import os
import math
import smtplib
import secrets
import uuid
import json
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

load_dotenv()

app = Flask(__name__)
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
PROFILE_EMOJIS = [
    "😀", "😎", "🤓", "🦊", "🐼", "🐸", "🐱", "🦁", "🐯", "🐨",
    "🦄", "🌟", "🔥", "🎯", "🚀", "💰", "🎵", "🌈", "🍀", "⚡"
]

# In Trip mode the app shows trip pages and nothing else. This is an allow
# list rather than a block list on purpose: any page added later is hidden in
# Trip mode by default instead of leaking personal finances onto a screen
# that's being shared around a group.
TRIP_MODE_ENDPOINTS = {
    "trips", "trip_detail", "trip_home", "add_trip_friends", "remove_trip_friend",
    "add_trip_expense", "delete_trip_expense", "delete_trip", "set_mode",
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


def load_biometric_flag(client, user_id):
    """Whether this account turned biometric login on. Safe on databases that
    don't have the column yet."""
    try:
        rows = client.table("profiles").select("biometric_enabled").eq("id", user_id).execute().data
        return bool(rows and rows[0].get("biometric_enabled"))
    except Exception:
        return False


def load_profile_emoji(client, user_id):
    """Return the user's saved profile emoji, assigning one once if missing."""
    try:
        rows = client.table("profiles").select("profile_emoji").eq("id", user_id).execute().data
        emoji = rows[0].get("profile_emoji") if rows else None
        if emoji in PROFILE_EMOJIS:
            return emoji

        emoji = random.choice(PROFILE_EMOJIS)
        client.table("profiles").upsert({"id": user_id, "profile_emoji": emoji}).execute()
        return emoji
    except Exception:
        return "👤"


def save_profile_emoji(client, user_id, emoji):
    if emoji not in PROFILE_EMOJIS:
        return False
    try:
        client.table("profiles").upsert({"id": user_id, "profile_emoji": emoji}).execute()
        return True
    except Exception:
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

    profile_emoji = "👤"
    if session.get("user_id"):
        profile_emoji = session.get("profile_emoji")
        if not profile_emoji:
            profile_emoji = load_profile_emoji(get_user_client(), session["user_id"])
            session["profile_emoji"] = profile_emoji

    return {
        "asset_version": "1",
        "app_mode": mode,
        "user_theme": session.get("theme", "light") if session.get("user_id") else "light",
        "nav_name": name,
        "profile_emoji": profile_emoji,
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
    try:
        return date.fromisoformat(str(raw).strip())
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

    for month_offset in (0, 1):
        # Derive the target month from a date arithmetic anchor so December
        # rolls cleanly into January.
        month_anchor = (today.replace(day=28) + timedelta(days=4 + 31 * month_offset)).replace(day=1)
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
        return created.strftime("%d %b %Y")
    return str(created)[:10]


@app.route("/profile")
@login_required
def profile():
    client = get_user_client()
    user_id = session["user_id"]
    email = session.get("email") or ""
    access_token = session.get("access_token")  # threads can't read the session

    def count(table):
        try:
            return (
                client.table(table)
                .select("id", count="exact")
                .eq("user_id", user_id)
                .limit(1)
                .execute()
                .count
            )
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
    profile_emoji = profile_data.get("profile_emoji") if profile_data.get("profile_emoji") in PROFILE_EMOJIS else load_profile_emoji(client, user_id)
    biometric_enabled = bool(profile_data.get("biometric_enabled"))
    session["display_name"] = name  # keeps the top bar in step
    session["profile_emoji"] = profile_emoji
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
        profile_emoji=profile_emoji,
        profile_emojis=PROFILE_EMOJIS,
        biometric_enabled=biometric_enabled,
    )


@app.route("/profile/emoji", methods=["POST"])
@login_required
def update_profile_emoji():
    emoji = request.form.get("profile_emoji", "").strip()
    if save_profile_emoji(get_user_client(), session["user_id"], emoji):
        session["profile_emoji"] = emoji
        flash("Profile emoji updated.")
    else:
        flash("Please choose a valid profile emoji.")
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


def _fetch_report_history(client, user_id):
    try:
        return (
            client.table("report_history")
            .select("id, date_from, date_to, row_count, file_format, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(10)
            .execute()
            .data
        )
    except Exception:
        return []  # history table not created yet: the page still works


@app.route("/reports")
@login_required
def reports():
    client = get_user_client()
    today = datetime.now(APP_TZ).date()
    return render_template(
        "reports.html",
        default_from=today.replace(day=1).isoformat(),
        default_to=today.isoformat(),
        history=_fetch_report_history(client, session["user_id"]),
    )


@app.route("/reports/history")
@login_required
def reports_history():
    client = get_user_client()
    return jsonify(_fetch_report_history(client, session["user_id"]))


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
                snapshot_wealth = compute_net_worth(all_txns, savings, credit_cards, items)
                snapshot_date = parse_iso_date(request.form.get("snapshot_date")) or datetime.now(APP_TZ).date()
                service.table("net_worth_snapshots").upsert({
                    "user_id": user_id,
                    "snapshot_date": snapshot_date.isoformat(),
                    "net_worth": snapshot_wealth["net_worth"],
                    "notes": (request.form.get("snapshot_notes") or "").strip()[:300] or None,
                }).execute()
                flash(f"Net Worth snapshot saved for {snapshot_date.strftime('%d %b %Y')}.")
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
    wealth = compute_net_worth(all_txns, savings, credit_cards, manual_items) if unlocked else None
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
        .select("*")
        .eq("id", trip_id)
        .eq("user_id", user_id)
        .execute()
        .data
    )
    return rows[0] if rows else None


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

    latest = (
        client.table("trips")
        .select("id")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
        .data
    )
    if latest:
        return redirect(url_for("trip_detail", trip_id=latest[0]["id"]))
    return redirect(url_for("trips"))


@app.route("/trips", methods=["GET", "POST"])
@login_required
def trips():
    client = get_user_client()
    user_id = session["user_id"]

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        is_group = request.form.get("trip_type") == "group"
        if not name:
            flash("Give the trip a name.")
            return redirect(url_for("trips"))

        created = client.table("trips").insert({
            "user_id": user_id,
            "name": name,
            "is_group": is_group,
        }).execute()
        trip_id = created.data[0]["id"]

        if is_group:
            friend_names = parse_friend_names(request.form.get("friends") or "")
            if friend_names:
                try:
                    client.table("trip_participants").insert([
                        {"trip_id": trip_id, "user_id": user_id, "name": n}
                        for n in friend_names
                    ]).execute()
                except Exception:
                    flash("The trip was created, but the members list couldn't be saved. Add them below.")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    trip_rows = (
        client.table("trips")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
        .data
    )
    expense_rows = (
        client.table("trip_expenses")
        .select("trip_id, amount")
        .eq("user_id", user_id)
        .execute()
        .data
    )
    friend_rows = (
        client.table("trip_participants")
        .select("trip_id")
        .eq("user_id", user_id)
        .execute()
        .data
    )
    totals = defaultdict(int)
    for e in expense_rows:
        totals[e["trip_id"]] += _to_paise(e["amount"])
    friend_counts = defaultdict(int)
    for f in friend_rows:
        friend_counts[f["trip_id"]] += 1
    for t in trip_rows:
        t["total"] = totals[t["id"]] / 100
        t["friend_count"] = friend_counts[t["id"]]

    return render_template("trips.html", trips=trip_rows)


@app.route("/trips/<int:trip_id>")
@login_required
def trip_detail(trip_id):
    client = get_user_client()
    user_id = session["user_id"]

    # The trip, its members and its expenses don't depend on each other, so
    # they're fetched at the same time instead of one after another. Each
    # Supabase call is a network round trip, and that's where the time goes.
    with ThreadPoolExecutor(max_workers=3) as pool:
        trip_f = pool.submit(get_trip_or_none, client, user_id, trip_id)
        friends_f = pool.submit(
            lambda: client.table("trip_participants")
            .select("*")
            .eq("trip_id", trip_id)
            .eq("user_id", user_id)
            .order("id")
            .execute()
            .data
        )
        expenses_f = pool.submit(
            lambda: client.table("trip_expenses")
            .select("*")
            .eq("trip_id", trip_id)
            .eq("user_id", user_id)
            .order("expense_date", desc=True)
            .order("id", desc=True)
            .execute()
            .data
        )
        trip = trip_f.result()
        friends = friends_f.result()
        expenses = expenses_f.result()

    if not trip:
        flash("That trip wasn't found.")
        return redirect(url_for("trips"))
    session["active_trip"] = trip_id

    expense_ids = [e["id"] for e in expenses]
    splits = []
    if expense_ids:
        splits = (
            client.table("trip_expense_splits")
            .select("*")
            .eq("user_id", user_id)
            .in_("trip_expense_id", expense_ids)
            .execute()
            .data
        )

    splits_by_expense = defaultdict(list)
    for s in splits:
        splits_by_expense[s["trip_expense_id"]].append(s)
    for e in expenses:
        e["splits"] = splits_by_expense[e["id"]]

    total_paise = sum(_to_paise(e["amount"]) for e in expenses)

    people_rows, settlements, your_share = [], [], None
    if trip["is_group"]:
        names = ["You"] + [f["name"] for f in friends]
        paid, owed, net = compute_trip_summary(names, expenses, splits)
        people_rows = [
            {"name": n, "paid": paid[n] / 100, "owed": owed[n] / 100, "net": net[n] / 100}
            for n in names
        ]
        settlements = simplify_settlements(net)
        your_share = owed["You"] / 100

    return render_template(
        "trip_detail.html",
        trip=trip,
        friends=friends,
        expenses=expenses,
        total=total_paise / 100,
        people_rows=people_rows,
        settlements=settlements,
        your_share=your_share,
        today=datetime.now(APP_TZ).date().isoformat(),
    )


@app.route("/trips/<int:trip_id>/friends", methods=["POST"])
@login_required
def add_trip_friends(trip_id):
    client = get_user_client()
    user_id = session["user_id"]

    trip = get_trip_or_none(client, user_id, trip_id)
    if not trip or not trip["is_group"]:
        flash("That trip wasn't found, or it's a solo trip.")
        return redirect(url_for("trips"))

    existing = {
        f["name"].lower()
        for f in client.table("trip_participants")
        .select("name")
        .eq("trip_id", trip_id)
        .eq("user_id", user_id)
        .execute()
        .data
    }
    new_names = [
        n for n in parse_friend_names(request.form.get("friends") or "")
        if n.lower() not in existing
    ]
    if not new_names:
        flash("Enter at least one new name.")
    else:
        client.table("trip_participants").insert([
            {"trip_id": trip_id, "user_id": user_id, "name": n} for n in new_names
        ]).execute()
    return redirect(url_for("trip_detail", trip_id=trip_id))


@app.route("/trips/<int:trip_id>/friends/<int:friend_id>/delete", methods=["POST"])
@login_required
def remove_trip_friend(trip_id, friend_id):
    """Only allowed while that friend hasn't paid for or been split into any
    expense. Removing someone who's already part of the math would quietly
    change what everyone else owes."""
    client = get_user_client()
    user_id = session["user_id"]

    friend_rows = (
        client.table("trip_participants")
        .select("*")
        .eq("id", friend_id)
        .eq("trip_id", trip_id)
        .eq("user_id", user_id)
        .execute()
        .data
    )
    if not friend_rows:
        flash("That member wasn't found.")
        return redirect(url_for("trip_detail", trip_id=trip_id))
    name = friend_rows[0]["name"]

    trip_expenses = (
        client.table("trip_expenses")
        .select("id, paid_by")
        .eq("trip_id", trip_id)
        .eq("user_id", user_id)
        .execute()
        .data
    )
    in_use = any(e["paid_by"] == name for e in trip_expenses)
    if not in_use and trip_expenses:
        in_use = bool(
            client.table("trip_expense_splits")
            .select("id")
            .eq("user_id", user_id)
            .eq("participant_name", name)
            .in_("trip_expense_id", [e["id"] for e in trip_expenses])
            .execute()
            .data
        )
    if in_use:
        flash(f"{name} is part of existing expenses. Delete those first to remove them.")
    else:
        client.table("trip_participants").delete().eq("id", friend_id).eq("user_id", user_id).execute()
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

    description = (request.form.get("description") or "").strip()
    try:
        total_paise = _to_paise(request.form.get("amount"))
    except (TypeError, ValueError):
        total_paise = 0
    if not description or total_paise <= 0:
        flash("An expense needs a description and an amount above zero.")
        return back

    payer_name = "You"
    shares = None  # {name: paise}; stays None for solo trips (nothing to split)

    if trip["is_group"]:
        friends = (
            client.table("trip_participants")
            .select("*")
            .eq("trip_id", trip_id)
            .eq("user_id", user_id)
            .order("id")
            .execute()
            .data
        )
        # Form values use "you" or the friend's id, so a name that contains
        # odd characters (or a friend added in another tab) can't misroute a share.
        people = {"you": "You"}
        people.update({str(f["id"]): f["name"] for f in friends})

        payer_name = people.get(request.form.get("paid_by"))
        if not payer_name:
            flash("Pick who paid.")
            return back

        split_type = request.form.get("split_type", "equal")
        if split_type == "equal":
            shares = split_equally_paise(total_paise, list(people.values()))
        elif split_type == "subset":
            chosen = [people[k] for k in request.form.getlist("split_with") if k in people]
            if not chosen:
                flash("Pick at least one person to split this between.")
                return back
            shares = split_equally_paise(total_paise, chosen)
        elif split_type == "custom":
            shares = {}
            for key, name in people.items():
                raw = (request.form.get(f"share_{key}") or "").strip()
                if not raw:
                    continue
                try:
                    paise = _to_paise(raw)
                except ValueError:
                    flash(f"'{raw}' isn't a valid amount.")
                    return back
                if paise < 0:
                    flash("Custom amounts can't be negative.")
                    return back
                if paise > 0:
                    shares[name] = paise
            if sum(shares.values()) != total_paise:
                flash(
                    f"The custom amounts add up to {sum(shares.values()) / 100:.2f}, "
                    f"but the expense is {total_paise / 100:.2f}. They need to match."
                )
                return back
        else:
            flash("Pick how to split this expense.")
            return back

    expense_row = {
        "trip_id": trip_id,
        "user_id": user_id,
        "description": description,
        "amount": total_paise / 100,
        "paid_by": payer_name,
    }
    expense_date = request.form.get("expense_date")
    if expense_date:
        expense_row["expense_date"] = expense_date

    created = client.table("trip_expenses").insert(expense_row).execute()
    expense_id = created.data[0]["id"]

    if shares:
        try:
            client.table("trip_expense_splits").insert([
                {
                    "trip_expense_id": expense_id,
                    "user_id": user_id,
                    "participant_name": name,
                    "share_amount": paise / 100,
                }
                for name, paise in shares.items()
            ]).execute()
        except Exception as e:
            # An expense without its splits would silently skew everyone's
            # balance, so undo it rather than leave it half-recorded.
            client.table("trip_expenses").delete().eq("id", expense_id).eq("user_id", user_id).execute()
            flash(f"Couldn't save that expense, please try again. ({e})")
            return back

    flash("Expense added.")
    return back


@app.route("/trips/<int:trip_id>/expenses/<int:expense_id>/delete", methods=["POST"])
@login_required
def delete_trip_expense(trip_id, expense_id):
    client = get_user_client()
    user_id = session["user_id"]
    # Deleting the expense cascades to its split rows.
    client.table("trip_expenses").delete().eq("id", expense_id).eq("trip_id", trip_id).eq("user_id", user_id).execute()
    flash("Expense deleted.")
    return redirect(url_for("trip_detail", trip_id=trip_id))


@app.route("/trips/<int:trip_id>/delete", methods=["POST"])
@login_required
def delete_trip(trip_id):
    client = get_user_client()
    user_id = session["user_id"]
    client.table("trips").delete().eq("id", trip_id).eq("user_id", user_id).execute()
    if session.get("active_trip") == trip_id:
        session.pop("active_trip", None)
    flash("Trip deleted.")
    return redirect(url_for("trips"))


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
        .select("source_id, amount, direction, category, transaction_date, is_previous_card_bill")
        .eq("user_id", user_id)
        .execute()
        .data
    )

    flows = defaultdict(lambda: {"in": 0.0, "out": 0.0})
    for t in all_txns:
        sid = t.get("source_id")
        if sid is None or not t.get("amount"):
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


def compute_net_worth(all_txns, savings, credit_cards, manual_items=None):
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

    lent_out = sum(
        float(t["amount"]) for t in all_txns
        if t.get("category") == "lending" and t.get("direction") == "out" and t.get("amount")
    )
    lent_in = sum(
        float(t["amount"]) for t in all_txns
        if t.get("category") == "lending" and t.get("direction") == "in" and t.get("amount")
    )
    total_lent = lent_out - lent_in

    manual_items = manual_items or []
    manual_assets, manual_liabilities = compute_net_worth_manual_totals(manual_items)

    return {
        "total_savings": total_savings,
        "total_invested": total_invested,
        "total_lent": total_lent,
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
        ),
    }


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
    this_year = datetime.now(APP_TZ).date().year
    return d.strftime("%d %b" if d.year == this_year else "%d %b %Y")


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
    non_flow_categories = ("transfer", "lending")
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

    recent = txns[:10]

    savings, credit_cards, all_txns = compute_source_balances(client, user_id)
    manual_items = get_net_worth_manual_items() if net_worth_is_unlocked() else []
    wealth = compute_net_worth(all_txns, savings, credit_cards, manual_items)

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

    # Safe to Spend reserves only commitments still due during the current
    # calendar month. Future-month expenses should not reduce this month's
    # spendable balance.
    month_end = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    safe_spend_commitments = [
        x for x in upcoming_commitments
        if x.get("due_date") and today <= x["due_date"] <= month_end
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
    fixed_remaining = float(wealth["total_savings"]) - fixed_total - fixed_investment_total
    safe_to_spend = compute_safe_to_spend(savings, safe_spend_commitments)
    safe_spend_bank_cash = sum(float(s.get("balance") or 0) for s in savings)
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
        fixed_remaining=fixed_remaining,
        card_forecasts=card_forecasts,
        expected_cc_total=expected_cc_total,
        upcoming_commitments=upcoming_commitments,
        safe_to_spend=safe_to_spend,
        safe_spend_bank_cash=safe_spend_bank_cash,
        safe_spend_fixed=safe_spend_fixed,
        safe_spend_investments=safe_spend_investments,
        safe_spend_cc=safe_spend_cc,
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
