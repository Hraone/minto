import unittest

from login_performance import load_login_profile_state


class FakeQuery:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error
        self.selected = None
        self.filters = []
        self.limit_value = None

    def select(self, columns):
        self.selected = columns
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def execute(self):
        if self.error:
            raise self.error
        return type("Result", (), {"data": self.rows})()


class FakeClient:
    def __init__(self, rows=None, error=None):
        self.query = FakeQuery(rows=rows, error=error)
        self.table_name = None

    def table(self, name):
        self.table_name = name
        return self.query


class LoginProfileLoaderTests(unittest.TestCase):
    def test_reads_all_preferences_in_one_query(self):
        row = {
            "app_mode": "trip",
            "theme": "dark",
            "genz_mode": True,
            "display_name": "Minto User",
            "biometric_enabled": True,
        }
        client = FakeClient(rows=[row])

        result = load_login_profile_state(client, "user-123")

        self.assertEqual(result, row)
        self.assertEqual(client.table_name, "profiles")
        self.assertEqual(
            client.query.selected,
            "app_mode,theme,genz_mode,display_name,biometric_enabled",
        )
        self.assertEqual(client.query.filters, [("id", "user-123")])
        self.assertEqual(client.query.limit_value, 1)

    def test_returns_empty_mapping_when_profile_row_is_missing(self):
        self.assertEqual(load_login_profile_state(FakeClient(), "user-123"), {})

    def test_returns_none_on_schema_or_query_error_for_legacy_fallback(self):
        self.assertIsNone(
            load_login_profile_state(FakeClient(error=RuntimeError("missing column")), "user-123")
        )


if __name__ == "__main__":
    unittest.main()
