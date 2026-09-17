import os
from functools import wraps
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, request, redirect, url_for, session, flash
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]


def get_client() -> Client:
    """A plain (unauthenticated) client — used for signup/login itself."""
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


def get_user_client() -> Client:
    """A client carrying the logged-in user's session, so Supabase's
    row-level-security policies scope every query to that user automatically."""
    client = get_client()
    if "access_token" in session and "refresh_token" in session:
        client.auth.set_session(session["access_token"], session["refresh_token"])
    return client


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
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
            client.auth.set_session(result.session.access_token, result.session.refresh_token)
        try:
            client.table("profiles").insert({"id": user_id}).execute()
        except Exception:
            pass  # profile row may already exist, or email confirmation is pending

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

        session["user_id"] = result.user.id
        session["email"] = result.user.email
        session["access_token"] = result.session.access_token
        session["refresh_token"] = result.session.refresh_token
        return redirect(url_for("entry"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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
        source_id = request.form.get("source_id") or None
        notes = request.form.get("notes") or None

        entry_row = client.table("entries").insert({
            "user_id": user_id,
            "entry_text": notes or f"{direction} {amount} {category}",
            "mode": "manual",
        }).execute()
        entry_id = entry_row.data[0]["id"]

        client.table("transactions").insert({
            "id": entry_id,
            "user_id": user_id,
            "direction": direction,
            "category": category,
            "expense_category": expense_category,
            "source_id": int(source_id) if source_id else None,
            "amount": float(amount) if amount else None,
            "currency": "INR",
            "description": notes,
            "raw_text": notes,
        }).execute()

        flash("Saved.")
        return redirect(url_for("entry"))

    sources = client.table("user_sources").select("*").eq("active", True).execute().data
    return render_template("entry.html", sources=sources)


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
                row["opening_balance"] = float(opening_balance)
            elif source_type == "credit_card":
                credit_limit = request.form.get("credit_limit") or None
                outstanding = request.form.get("outstanding") or 0
                row["credit_limit"] = float(credit_limit) if credit_limit else None
                row["opening_balance"] = float(outstanding)
            client.table("user_sources").insert(row).execute()
        return redirect(url_for("sources"))

    all_sources = (
        client.table("user_sources")
        .select("*")
        .eq("active", True)
        .order("name")
        .execute()
        .data
    )

    txns = (
        client.table("transactions")
        .select("source_id, amount, direction")
        .eq("user_id", user_id)
        .execute()
        .data
    )

    flows = defaultdict(lambda: {"in": 0.0, "out": 0.0})
    for t in txns:
        sid = t.get("source_id")
        if sid is None or not t.get("amount"):
            continue
        flows[sid][t["direction"]] += float(t["amount"])

    savings, credit_cards = [], []
    for s in all_sources:
        f = flows[s["id"]]
        opening = float(s.get("opening_balance") or 0)

        if s["source_type"] == "savings":
            s["balance"] = opening + f["in"] - f["out"]
            savings.append(s)
        else:
            limit = float(s["credit_limit"]) if s.get("credit_limit") else 0
            outstanding = max(opening + f["out"] - f["in"], 0)
            s["outstanding"] = outstanding
            s["limit"] = limit
            s["limit_left"] = max(limit - outstanding, 0) if limit else None
            s["limit_pct"] = round((outstanding / limit) * 100, 1) if limit else None
            credit_cards.append(s)

    return render_template("sources.html", savings=savings, credit_cards=credit_cards)


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
        query = query.gte("created_at", start.isoformat())
    txns = query.order("created_at", desc=True).execute().data

    total_in = sum(float(t["amount"]) for t in txns if t["direction"] == "in" and t["amount"])
    total_out = sum(float(t["amount"]) for t in txns if t["direction"] == "out" and t["amount"])
    net = total_in - total_out

    expense_by_category = defaultdict(float)
    for t in txns:
        if t["category"] == "expense" and t["amount"]:
            key = t.get("expense_category") or "other"
            expense_by_category[key] += float(t["amount"])

    spend_by_source = defaultdict(float)
    for t in txns:
        source = t.get("user_sources")
        if source and t["amount"] and t["direction"] == "out":
            spend_by_source[source["name"]] += float(t["amount"])

    recent = txns[:10]

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
    )


@app.route("/health")
def health():
    return "OK"


if __name__ == "__main__":
    app.run(debug=True)
