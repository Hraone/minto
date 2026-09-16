import os
from functools import wraps
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
            client.table("user_sources").insert({
                "user_id": user_id,
                "name": name,
                "source_type": source_type,
            }).execute()
        return redirect(url_for("sources"))

    all_sources = client.table("user_sources").select("*").eq("active", True).execute().data
    return render_template("sources.html", sources=all_sources)


@app.route("/health")
def health():
    return "OK"


if __name__ == "__main__":
    app.run(debug=True)
