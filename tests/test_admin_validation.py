import unittest

from admin_validation import (
    ADMIN_FEEDBACK_PRIORITIES, ADMIN_FEEDBACK_STATUSES,
    parse_admin_page, validate_admin_feedback_update,
)


class AdminValidationTests(unittest.TestCase):
    def test_supported_workflow_values(self):
        values, errors = validate_admin_feedback_update("In Progress", "Urgent")
        self.assertEqual({}, errors)
        self.assertEqual({"status": "In Progress", "priority": "Urgent"}, values)

    def test_all_supported_values_are_accepted(self):
        for status in ADMIN_FEEDBACK_STATUSES:
            self.assertEqual({}, validate_admin_feedback_update(status, "Normal")[1])
        for priority in ADMIN_FEEDBACK_PRIORITIES:
            self.assertEqual({}, validate_admin_feedback_update("Submitted", priority)[1])

    def test_invalid_values_are_rejected(self):
        values, errors = validate_admin_feedback_update("Administrator", "Critical")
        self.assertEqual({}, values)
        self.assertIn("status", errors)
        self.assertIn("priority", errors)

    def test_non_string_and_blank_values_are_rejected(self):
        _, errors = validate_admin_feedback_update("  ", None)
        self.assertIn("status", errors)
        self.assertIn("priority", errors)

    def test_page_parser_is_bounded(self):
        self.assertEqual(1, parse_admin_page(None))
        self.assertEqual(1, parse_admin_page("abc"))
        self.assertEqual(1, parse_admin_page("0"))
        self.assertEqual(100, parse_admin_page("999999", max_page=100))
        self.assertEqual(3, parse_admin_page(" 3 "))


if __name__ == "__main__":
    unittest.main()
