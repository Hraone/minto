import os
import io
import csv
import uuid
import json
import time
import base64
from functools import wraps
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone, date
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory, jsonify, Response
from supabase import create_client, Client
from dotenv import load_dotenv
from werkzeug.exceptions import HTTPException

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
# How long a logged-in session survives with no activity at all — separate
# from the Supabase access token's 1-hour life, which refresh_if_needed()
# renews automatically as long as this outer session is still alive.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)


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
    return {
        "asset_version": "1",
        "app_mode": mode,
        "supabase_url": SUPABASE_URL,
        "supabase_anon_key": SUPABASE_ANON_KEY,
    }


SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]

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
    "mutual_fund", "stocks", "fixed_deposit", "recurring_deposit",
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


def get_client() -> Client:
    """A plain (unauthenticated) client — used for signup/login itself."""
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


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
        return e

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
        session["mode"] = load_saved_mode(get_user_client(), result.user.id)

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
    session["mode"] = load_saved_mode(get_user_client(), user.id)
    return jsonify(ok=True, redirect=home_url())


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
            rows = client.table("profiles").select("display_name").eq("id", user_id).execute().data
            return rows[0].get("display_name") if rows else None
        except Exception:
            return None  # column not added yet: fall back to the email name

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
        name = name_f.result()
        since = since_f.result()

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
    )


@app.route("/profile/name", methods=["POST"])
@login_required
def update_profile_name():
    name = " ".join(request.form.get("display_name", "").split())[:40]
    client = get_user_client()
    try:
        client.table("profiles").upsert(
            {"id": session["user_id"], "display_name": name or None}
        ).execute()
        flash("Name updated.")
    except Exception:
        flash("Couldn't save your name. Please try again.")
    return redirect(url_for("profile"))


# ---------------------------------------------------------------------------
# Reports: the file is built on the fly and sent straight to the user's device.
# Only a small history row (range, row count, time) is saved, never the report.
# ---------------------------------------------------------------------------

REPORT_TYPE_LABELS = {"lending": "Lent"}
REPORT_ACCOUNT_LABELS = {"savings": "Bank", "credit_card": "Card", "cash": "Cash"}


def _csv_text(value):
    """Plain text for a CSV cell. A leading = + - @ would be run as a formula
    by Excel or Sheets, so such values get a leading apostrophe."""
    if value is None:
        return ""
    text = str(value).strip()
    if text[:1] in ("=", "+", "-", "@"):
        return "'" + text
    return text


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
    today = datetime.now(timezone.utc).date()
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


@app.route("/reports/download")
@login_required
def download_report():
    d_from, d_to, error = _parse_report_dates(request.args)
    if error:
        flash(error)
        return redirect(url_for("reports"))

    client = get_user_client()
    user_id = session["user_id"]

    # Supabase returns at most 1000 rows per request, so page through them.
    rows, start = [], 0
    while True:
        batch = (
            client.table("transactions")
            .select("*, user_sources(name, source_type)")
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
            break
        start += 1000

    if not rows:
        flash("No transactions between those dates.")
        return redirect(url_for("reports"))

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Date", "Type", "Category", "In or Out", "Account", "Account type", "Person", "Amount", "Description"])

    total_in = total_out = 0.0
    for t in rows:
        source = t.get("user_sources") or {}
        category = t.get("category") or ""
        amount = float(t["amount"]) if t.get("amount") is not None else 0.0
        direction = t.get("direction") or ""
        if category not in ("transfer", "lending"):
            if direction == "in":
                total_in += amount
            elif direction == "out":
                total_out += amount
        detail = t.get("expense_category") or t.get("investment_category") or ""
        writer.writerow([
            t.get("transaction_date") or "",
            REPORT_TYPE_LABELS.get(category, category.capitalize()),
            _csv_text(detail.replace("_", " ").title()),
            direction.capitalize(),
            _csv_text(source.get("name")),
            REPORT_ACCOUNT_LABELS.get(source.get("source_type"), ""),
            _csv_text(t.get("counterparty")),
            f"{amount:.2f}",
            _csv_text(t.get("description")),
        ])

    writer.writerow([])
    summary = [
        ("Transactions", str(len(rows))),
        ("Total in (excludes transfers and lent)", f"{total_in:.2f}"),
        ("Total out (excludes transfers and lent)", f"{total_out:.2f}"),
        ("Net", f"{total_in - total_out:.2f}"),
    ]
    for label, value in summary:
        writer.writerow([label, "", "", "", "", "", "", value, ""])

    # Log that a report was made (range and size only). Never blocks the download.
    try:
        client.table("report_history").insert({
            "user_id": user_id,
            "date_from": d_from.isoformat(),
            "date_to": d_to.isoformat(),
            "row_count": len(rows),
            "file_format": "csv",
        }).execute()
    except Exception:
        pass

    filename = f"minto-report-{d_from.isoformat()}-to-{d_to.isoformat()}.csv"
    return Response(
        buffer.getvalue().encode("utf-8-sig"),  # BOM so Excel reads the rupee sign and accents
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.route("/info")
def info():
    return render_template("info.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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
        amount = request.form.get("amount")
        direction = request.form.get("direction")
        category = request.form.get("category")
        expense_category = request.form.get("expense_category") or None
        investment_category = request.form.get("investment_category") or None
        counterparty = request.form.get("counterparty") or None
        source_id = request.form.get("source_id") or None
        notes = request.form.get("notes") or None
        transaction_date = request.form.get("transaction_date") or None

        entry_row = client.table("entries").insert({
            "user_id": user_id,
            "entry_text": notes or f"{direction} {amount} {category}",
            "mode": "manual",
        }).execute()
        entry_id = entry_row.data[0]["id"]

        transaction_row = {
            "id": entry_id,
            "user_id": user_id,
            "direction": direction,
            "category": category,
            "expense_category": expense_category,
            "investment_category": investment_category,
            "counterparty": counterparty if category == "lending" else None,
            "source_id": int(source_id) if source_id else None,
            "amount": float(amount) if amount else None,
            "currency": "INR",
            "description": notes,
            "raw_text": notes,
        }
        if transaction_date:
            transaction_row["transaction_date"] = transaction_date
        # else: omitted entirely so the column's own DB default (today) applies —
        # explicitly sending null here would fail the not-null constraint.
        client.table("transactions").insert(transaction_row).execute()

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
        today=datetime.now(timezone.utc).date().isoformat(),
    )


@app.route("/pay-cc-bill", methods=["GET", "POST"])
@login_required
def pay_cc_bill():
    """Record a credit-card bill payment as a linked pair of transfer legs:
    money OUT of a savings source and the same amount IN to the card, so the
    savings balance and the card's outstanding both move together and net
    worth is unaffected (paying down debt with cash isn't a gain or a loss).

    A single 'expense' entry from savings does NOT reduce the card's
    outstanding balance — that's the mistake this route exists to prevent.
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
    savings_sources = [s for s in sources if s["source_type"] in ("savings", "cash")]
    cc_sources = [s for s in sources if s["source_type"] == "credit_card"]

    if request.method == "POST":
        amount = request.form.get("amount")
        from_source_id = request.form.get("from_source_id")
        to_source_id = request.form.get("to_source_id")
        notes = request.form.get("notes") or None

        if not amount or not from_source_id or not to_source_id:
            flash("Pick an amount, a bank or cash account to pay from, and a card to pay off.")
            return render_template(
                "pay_cc_bill.html",
                savings_sources=savings_sources,
                cc_sources=cc_sources,
            )

        amount = float(amount)
        transfer_group = str(uuid.uuid4())
        description = notes or "Credit card bill payment"

        def insert_leg(direction, source_id):
            entry_row = client.table("entries").insert({
                "user_id": user_id,
                "entry_text": description,
                "mode": "manual",
            }).execute()
            entry_id = entry_row.data[0]["id"]
            client.table("transactions").insert({
                "id": entry_id,
                "user_id": user_id,
                "direction": direction,
                "category": "transfer",
                "source_id": int(source_id),
                "amount": amount,
                "currency": "INR",
                "description": description,
                "raw_text": description,
                "transfer_group": transfer_group,
            }).execute()
            return entry_id

        out_entry_id = None
        try:
            out_entry_id = insert_leg("out", from_source_id)
            insert_leg("in", to_source_id)
        except Exception as e:
            # Best-effort rollback: without the first leg, don't leave a
            # dangling half-transfer sitting in the savings account.
            if out_entry_id is not None:
                try:
                    client.table("entries").delete().eq("id", out_entry_id).execute()
                except Exception:
                    pass
            flash(f"Couldn't record the payment. Please try again. ({e})")
            return render_template(
                "pay_cc_bill.html",
                savings_sources=savings_sources,
                cc_sources=cc_sources,
            )

        flash("Payment recorded. Bank and card balances both updated.")
        return redirect(url_for("sources"))

    return render_template(
        "pay_cc_bill.html",
        savings_sources=savings_sources,
        cc_sources=cc_sources,
    )


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
        today=datetime.now(timezone.utc).date().isoformat(),
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

    if request.method == "POST":
        amount = request.form.get("amount")
        from_source_id = request.form.get("from_source_id")
        to_source_id = request.form.get("to_source_id")
        notes = request.form.get("notes") or None

        if not amount or not from_source_id or not to_source_id:
            flash("Pick an amount, a bank account to take it from, and which cash account it goes into.")
            return render_template(
                "withdraw_cash.html",
                bank_sources=bank_sources,
                cash_sources=cash_sources,
            )

        amount = float(amount)
        transfer_group = str(uuid.uuid4())
        description = notes or "Cash withdrawal"

        def insert_leg(direction, source_id):
            entry_row = client.table("entries").insert({
                "user_id": user_id,
                "entry_text": description,
                "mode": "manual",
            }).execute()
            entry_id = entry_row.data[0]["id"]
            client.table("transactions").insert({
                "id": entry_id,
                "user_id": user_id,
                "direction": direction,
                "category": "transfer",
                "source_id": int(source_id),
                "amount": amount,
                "currency": "INR",
                "description": description,
                "raw_text": description,
                "transfer_group": transfer_group,
            }).execute()
            return entry_id

        out_entry_id = None
        try:
            out_entry_id = insert_leg("out", from_source_id)
            insert_leg("in", to_source_id)
        except Exception as e:
            # Best-effort rollback: without the first leg, don't leave a
            # dangling half-transfer sitting in the bank account.
            if out_entry_id is not None:
                try:
                    client.table("entries").delete().eq("id", out_entry_id).execute()
                except Exception:
                    pass
            flash(f"Couldn't record the cash out. Please try again. ({e})")
            return render_template(
                "withdraw_cash.html",
                bank_sources=bank_sources,
                cash_sources=cash_sources,
            )

        flash("Cash out recorded. Bank and cash balances both updated.")
        return redirect(url_for("sources"))

    return render_template(
        "withdraw_cash.html",
        bank_sources=bank_sources,
        cash_sources=cash_sources,
    )


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
        .select("source_id, amount, direction, category")
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


def compute_net_worth(all_txns, savings, credit_cards):
    total_savings = sum(s["balance"] for s in savings)
    total_cc_debt = sum(s["outstanding"] for s in credit_cards)
    total_cc_limit = sum(s["limit"] for s in credit_cards if s.get("limit"))
    # Only meaningful if at least one card has a limit set — otherwise leave
    # it out rather than showing a misleading 0%.
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

    # Money lent to other people (e.g. a friend) leaves your account like an
    # expense would, but unlike an expense you expect it back — so it's
    # counted as a receivable asset here, the same way an investment is,
    # rather than as spending. A repayment ("in") shrinks it back down.
    lent_out = sum(
        float(t["amount"]) for t in all_txns
        if t.get("category") == "lending" and t.get("direction") == "out" and t.get("amount")
    )
    lent_in = sum(
        float(t["amount"]) for t in all_txns
        if t.get("category") == "lending" and t.get("direction") == "in" and t.get("amount")
    )
    total_lent = lent_out - lent_in

    return {
        "total_savings": total_savings,
        "total_invested": total_invested,
        "total_lent": total_lent,
        "total_cc_debt": total_cc_debt,
        "overall_utilization_pct": overall_utilization_pct,
        "net_worth": total_savings + total_invested + total_lent - total_cc_debt,
    }


@app.route("/sources", methods=["GET", "POST"])
@login_required
def sources():
    client = get_user_client()
    user_id = session["user_id"]

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        source_type = request.form.get("source_type")
        if name and source_type:
            row = {
                "user_id": user_id,
                "name": name,
                "source_type": source_type,
            }
            if source_type == "savings":
                opening_balance = request.form.get("opening_balance") or 0
                minimum_balance = request.form.get("minimum_balance") or 0
                row["opening_balance"] = float(opening_balance)
                row["minimum_balance"] = float(minimum_balance)
            elif source_type == "cash":
                opening_balance = request.form.get("cash_opening_balance") or 0
                row["opening_balance"] = float(opening_balance)
            elif source_type == "credit_card":
                credit_limit = request.form.get("credit_limit") or None
                outstanding = request.form.get("outstanding") or 0
                row["credit_limit"] = float(credit_limit) if credit_limit else None
                row["opening_balance"] = float(outstanding)
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
    return render_template("sources.html", savings=savings, cash=cash, credit_cards=credit_cards)


@app.template_filter("nice_date")
def nice_date(value):
    """'2026-09-23' -> '23 Sep', or '23 Sep 2025' if not this year — used to
    show a transaction's actual date in the Recent Transactions list."""
    if not value:
        return ""
    try:
        d = datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return value
    this_year = datetime.now(timezone.utc).date().year
    return d.strftime("%d %b" if d.year == this_year else "%d %b %Y")


def get_period_start(period):
    now = datetime.now(timezone.utc)
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
    wealth = compute_net_worth(all_txns, savings, credit_cards)

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
        outstanding_loans=outstanding_loans,
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
