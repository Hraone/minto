"""Pure validation helpers for Minto admin workflows."""
ADMIN_FEEDBACK_STATUSES = ("Submitted", "Under Review", "In Progress", "Resolved", "Closed", "Declined")
ADMIN_FEEDBACK_PRIORITIES = ("Low", "Normal", "High", "Urgent")


def validate_admin_feedback_update(status, priority):
    status = status.strip() if isinstance(status, str) else ""
    priority = priority.strip() if isinstance(priority, str) else ""
    errors = {}
    if status not in ADMIN_FEEDBACK_STATUSES:
        errors["status"] = "Choose a supported status."
    if priority not in ADMIN_FEEDBACK_PRIORITIES:
        errors["priority"] = "Choose a supported priority."
    return ({"status": status, "priority": priority}, errors) if not errors else ({}, errors)


def parse_admin_page(value, max_page=100000):
    try:
        return max(1, min(int(value), max_page))
    except (TypeError, ValueError):
        return 1
