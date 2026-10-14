"""Validation helpers for Minto's private user-feedback workflow.

Kept independent from Flask and Supabase so the rules can be tested without
network credentials or a live database.
"""

FEEDBACK_CATEGORIES = (
    "General Feedback",
    "UI/UX Improvement",
    "Feature Request",
    "Bug Report",
    "Performance Issue",
    "Other",
)
MAX_FEEDBACK_MESSAGE_LENGTH = 5000


def validate_feedback_form(form):
    """Return normalized form values and field-level validation errors."""
    category = (form.get("category") or "").strip()
    rating_raw = (form.get("rating") or "").strip()
    message = (form.get("message") or "").strip()

    values = {
        "category": category,
        "rating": rating_raw,
        "message": message,
    }
    errors = {}

    if category not in FEEDBACK_CATEGORIES:
        errors["category"] = "Choose a valid feedback category."

    rating = None
    if rating_raw:
        try:
            rating = int(rating_raw)
        except (TypeError, ValueError):
            rating = None
        if rating is None or str(rating) != rating_raw or not 1 <= rating <= 5:
            errors["rating"] = "Choose a rating from 1 to 5, or leave it blank."
        else:
            values["rating"] = str(rating)

    if not message:
        errors["message"] = "Please tell us a little more about your feedback."
    elif len(message) > MAX_FEEDBACK_MESSAGE_LENGTH:
        errors["message"] = "Feedback must be 5,000 characters or fewer."

    values["rating_value"] = rating if "rating" not in errors else None
    return values, errors
