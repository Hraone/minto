"""Pure financial calculations used by the dashboard route."""

from datetime import datetime, timedelta


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
    total_in = 0.0
    total_out = 0.0
    expense_by_category = {}
    outgoing_by_category = {}
    spend_by_source = {}

    for transaction in transactions:
        amount = transaction.get("amount")
        direction = transaction.get("direction")
        category = transaction.get("category")

        if direction == "in" and amount and category not in _NON_FLOW_CATEGORIES:
            total_in += float(amount)
        if direction == "out" and amount and category not in _NON_FLOW_CATEGORIES:
            total_out += float(amount)

        if category == "expense" and amount:
            expense_category = transaction.get("expense_category") or "other"
            expense_by_category[expense_category] = (
                expense_by_category.get(expense_category, 0.0) + float(amount)
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
            outgoing_by_category[label] = outgoing_by_category.get(label, 0.0) + float(amount)

        source = transaction.get("user_sources")
        if source and amount and direction == "out" and category not in _NON_FLOW_CATEGORIES:
            source_name = source["name"]
            spend_by_source[source_name] = spend_by_source.get(source_name, 0.0) + float(amount)

    return {
        "total_in": total_in,
        "total_out": total_out,
        "net": total_in - total_out,
        "expense_by_category": expense_by_category,
        "outgoing_by_category": outgoing_by_category,
        "spend_by_source": spend_by_source,
    }


def filter_commitments_to_current_month(commitments, today):
    """Keep only commitments whose due date falls in today's calendar month."""
    month_end = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return [
        commitment for commitment in commitments
        if (
            commitment.get("due_date")
            and commitment["due_date"].year == today.year
            and commitment["due_date"].month == today.month
            and commitment["due_date"] <= month_end
        )
    ]


def compute_safe_to_spend(savings, commitments):
    """Return liquid savings remaining after commitments, never below zero."""
    liquid = sum(float(source.get("balance") or 0) for source in savings)
    committed = sum(float(item.get("amount") or 0) for item in commitments)
    return max(round(liquid - committed, 2), 0.0)

