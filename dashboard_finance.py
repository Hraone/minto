"""Pure financial calculations used by the dashboard route."""

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


_CENT = Decimal("0.01")


def _decimal_amount(value):
    """Convert a stored monetary value without introducing binary-float noise."""
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _money(value):
    """Round a monetary result to cents using conventional half-up rounding."""
    return float(_decimal_amount(value).quantize(_CENT, rounding=ROUND_HALF_UP))


_NON_FLOW_CATEGORIES = (
    "transfer",
    "lending",
    "trip_expense_payment",
    "trip_settlement",
)


def get_dashboard_period_start(period, now):
    """Return the inclusive start for a dashboard period at a fixed time."""
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return now - timedelta(days=7)
    if period == "year":
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "all":
        return None
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def calculate_dashboard_flow(transactions):
    """Summarize income and outgoing activity without double-counting movements."""
    total_in = Decimal("0")
    total_out = Decimal("0")
    expense_by_category = {}
    outgoing_by_category = {}
    spend_by_source = {}

    for transaction in transactions:
        amount = transaction.get("amount")
        direction = transaction.get("direction")
        category = transaction.get("category")

        if direction == "in" and amount and category not in _NON_FLOW_CATEGORIES:
            total_in += _decimal_amount(amount)
        if direction == "out" and amount and category not in _NON_FLOW_CATEGORIES:
            total_out += _decimal_amount(amount)

        if category == "expense" and direction == "out" and amount:
            expense_category = transaction.get("expense_category") or "other"
            expense_by_category[expense_category] = (
                _decimal_amount(expense_by_category.get(expense_category, 0)) + _decimal_amount(amount)
            )

        if direction == "out" and amount and category not in _NON_FLOW_CATEGORIES:
            normalized_category = (category or "other").strip().lower()
            if normalized_category == "expense":
                detail = (transaction.get("expense_category") or "Other expense").strip()
                label = detail.replace("_", " ").title()
            elif normalized_category == "investment":
                detail = (transaction.get("investment_category") or "Other investment").strip()
                label = "Investment · " + detail.replace("_", " ").title()
            else:
                label = "Other · " + normalized_category.replace("_", " ").title()
            outgoing_by_category[label] = _decimal_amount(outgoing_by_category.get(label, 0)) + _decimal_amount(amount)

        source = transaction.get("user_sources")
        if source and amount and direction == "out" and category not in _NON_FLOW_CATEGORIES:
            source_name = source["name"]
            spend_by_source[source_name] = _decimal_amount(spend_by_source.get(source_name, 0)) + _decimal_amount(amount)

    return {
        "total_in": _money(total_in),
        "total_out": _money(total_out),
        "net": _money(total_in - total_out),
        "expense_by_category": {key: _money(value) for key, value in expense_by_category.items()},
        "outgoing_by_category": {key: _money(value) for key, value in outgoing_by_category.items()},
        "spend_by_source": {key: _money(value) for key, value in spend_by_source.items()},
    }


def filter_commitments_to_current_month(commitments, today):
    """Keep only commitments whose due date falls in today's calendar month."""
    month_end = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return [
        commitment for commitment in commitments
        if commitment.get("due_date")
        and commitment["due_date"].year == today.year
        and commitment["due_date"].month == today.month
        and commitment["due_date"] <= month_end
    ]


def filter_commitments_through_date(commitments, cutoff_date):
    """Keep unpaid commitments due on or before a supplied planning horizon."""
    return [
        item for item in commitments
        if item.get("due_date")
        and item["due_date"] <= cutoff_date
        and not item.get("paid", False)
        and float(item.get("amount") or 0) > 0
    ]


def calculate_safe_to_spend(savings, commitments):
    """Calculate spendable cash after commitments and per-account minimum reserves.

    Balances below their configured minimum are not treated as discretionary
    cash. Their shortfalls are reported separately so the user can save to
    restore each account's required minimum.
    """
    # Keep signed balances: an overdrawn account reduces available cash.
    total_cash = sum((_decimal_amount(source.get("balance")) for source in savings), Decimal("0"))
    minimum_reserve = sum((max(_decimal_amount(source.get("minimum_balance")), Decimal("0")) for source in savings), Decimal("0"))
    account_shortfalls = []
    for source in savings:
        balance = _decimal_amount(source.get("balance"))
        minimum = max(_decimal_amount(source.get("minimum_balance")), Decimal("0"))
        shortfall = max(minimum - balance, Decimal("0"))
        if shortfall > 0:
            account_shortfalls.append({
                "id": source.get("id"),
                "name": source.get("name") or "Account",
                "balance": _money(balance),
                "minimum_balance": _money(minimum),
                "shortfall": _money(shortfall),
            })

    # Preserve the full sum of required minimums across accounts. A surplus in
    # one account should not hide a shortfall in another account's minimum.
    spendable_before_commitments = max(total_cash - minimum_reserve, Decimal("0"))
    committed = sum((max(_decimal_amount(item.get("amount")), Decimal("0")) for item in commitments), Decimal("0"))
    return {
        "total_cash": _money(total_cash),
        "minimum_reserve": _money(minimum_reserve),
        "spendable_before_commitments": _money(spendable_before_commitments),
        "committed": _money(committed),
        "safe_to_spend": _money(max(spendable_before_commitments - committed, Decimal("0"))),
        "commitment_shortfall": _money(max(committed - spendable_before_commitments, Decimal("0"))),
        "minimum_balance_shortfall": _money(sum((_decimal_amount(x["shortfall"]) for x in account_shortfalls), Decimal("0"))),
        "minimum_balance_accounts": account_shortfalls,
    }


def compute_safe_to_spend(savings, commitments):
    """Backward-compatible wrapper returning only the safe-to-spend amount."""
    return calculate_safe_to_spend(savings, commitments)["safe_to_spend"]

