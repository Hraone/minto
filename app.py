import os
import uuid
from functools import wraps
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, request, redirect, url_for, session, flash
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
# How long a logged-in session survives with no activity at all — separate
# from the Supabase access token's 1-hour life, which refresh_if_needed()
# renews automatically as long as this outer session is still alive.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)


@app.context_processor
def inject_asset_version():
    """Cache-busting query string for static assets like the favicon.
    Browsers cache favicons unusually aggressively — bump this constant any
    time you replace static/favicon.svg with a new design, and every page
    will pick up the change immediately instead of showing the old icon
    until someone happens to hard-refresh."""
    return {"asset_version": "1"}


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
        flash("Your session is no longer valid — please log in again.")
        return redirect(url_for("login"))
    raise e


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))

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
                flash("Your account is no longer valid — please log in again.")
                return redirect(url_for("login"))

        return view(*args, **kwargs)
    return wrapped


@app.route("/signup", methods=["GET", "POST"])
def signup():
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

        # New users see Minto's short introduction once before entering the app.
        if session.pop("show_info", False):
            return redirect(url_for("info"))

        return redirect(url_for("entry"))

    return render_template("login.html")


@app.route("/info")
@login_required
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
            flash("Pick an amount, a savings source to pay from, and a card to pay off.")
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
            flash(f"Couldn't record the payment — please try again. ({e})")
            return render_template(
                "pay_cc_bill.html",
                savings_sources=savings_sources,
                cc_sources=cc_sources,
            )

        flash("Payment recorded — savings and card balances both updated.")
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
            flash("Pick an amount, a bank account to withdraw from, and which cash source it's going into.")
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
            flash(f"Couldn't record the withdrawal — please try again. ({e})")
            return render_template(
                "withdraw_cash.html",
                bank_sources=bank_sources,
                cash_sources=cash_sources,
            )

        flash("Withdrawal recorded — bank and cash balances both updated.")
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
                    flash(f"You already have a source named '{name}'.")
                else:
                    flash("Couldn't add that source — please try again.")
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
