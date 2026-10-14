"""Tests for the pure validation rules used by the feedback form."""

import unittest

from feedback_validation import FEEDBACK_CATEGORIES, validate_feedback_form


class FeedbackValidationTests(unittest.TestCase):
    def test_valid_feedback_without_rating_is_accepted(self):
        values, errors = validate_feedback_form({
            "category": "General Feedback",
            "rating": "",
            "message": "  Please add a compact monthly summary.  ",
        })

        self.assertEqual({}, errors)
        self.assertEqual("Please add a compact monthly summary.", values["message"])
        self.assertIsNone(values["rating_value"])

    def test_all_supported_categories_are_accepted(self):
        for category in FEEDBACK_CATEGORIES:
            with self.subTest(category=category):
                values, errors = validate_feedback_form({
                    "category": category,
                    "rating": "",
                    "message": "Useful details",
                })
                self.assertEqual({}, errors)
                self.assertEqual(category, values["category"])

    def test_rating_boundaries_are_valid(self):
        for rating in ("1", "2", "3", "4", "5"):
            with self.subTest(rating=rating):
                values, errors = validate_feedback_form({
                    "category": "Bug Report",
                    "rating": rating,
                    "message": "The save action fails.",
                })
                self.assertEqual({}, errors)
                self.assertEqual(int(rating), values["rating_value"])

    def test_invalid_category_and_rating_are_rejected(self):
        _, errors = validate_feedback_form({
            "category": "Admin Override",
            "rating": "6",
            "message": "Some feedback",
        })
        self.assertIn("category", errors)
        self.assertIn("rating", errors)

    def test_rating_is_not_required_for_bug_reports(self):
        _, errors = validate_feedback_form({
            "category": "Bug Report",
            "rating": "",
            "message": "The submit button does not respond.",
        })
        self.assertNotIn("rating", errors)

    def test_empty_and_whitespace_only_messages_are_rejected(self):
        for message in ("", "   \n\t "):
            with self.subTest(message=repr(message)):
                _, errors = validate_feedback_form({
                    "category": "General Feedback",
                    "rating": "",
                    "message": message,
                })
                self.assertIn("message", errors)

    def test_message_length_limit_is_enforced(self):
        _, valid_errors = validate_feedback_form({
            "category": "Other",
            "rating": "",
            "message": "x" * 5000,
        })
        _, invalid_errors = validate_feedback_form({
            "category": "Other",
            "rating": "",
            "message": "x" * 5001,
        })
        self.assertNotIn("message", valid_errors)
        self.assertIn("message", invalid_errors)


if __name__ == "__main__":
    unittest.main()
