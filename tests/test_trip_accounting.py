import unittest

from trip_accounting import calculate_trip_balances, suggested_settlements, trip_payables_for_user


class TripAccountingTests(unittest.TestCase):
    def setUp(self):
        self.members = [
            {"id": 1, "role": "owner", "display_name": "Owner"},
            {"id": 2, "role": "member", "user_id": "hidden", "display_name": "Rohan"},
            {"id": 3, "role": "member", "guest_name": "Priya", "display_name": "Priya"},
        ]

    def test_solo_trip_share_has_no_outstanding_balance(self):
        paid, owed, net = calculate_trip_balances(
            self.members,
            [{"id": 10, "amount": 4000, "payer_member_id": 1}],
            [{"trip_expense_id": 10, "trip_member_id": 1, "amount": 4000, "status": "accepted"}],
            [],
        )
        self.assertEqual(paid[1], 400000)
        self.assertEqual(owed[1], 400000)
        self.assertEqual(net, {1: 0, 2: 0, 3: 0})

    def test_pending_friend_share_is_outstanding_before_acceptance(self):
        expenses = [{"id": 10, "amount": 4000, "payer_member_id": 1}]
        shares = [
            {"trip_expense_id": 10, "trip_member_id": 1, "amount": 2000, "status": "accepted"},
            {"trip_expense_id": 10, "trip_member_id": 2, "amount": 2000, "status": "pending"},
        ]
        _, owed, net = calculate_trip_balances(self.members, expenses, shares, [])
        self.assertEqual(net[1], 200000)
        self.assertEqual(owed[2], 200000)
        self.assertEqual(net[2], -200000)
        self.assertEqual(trip_payables_for_user([2], expenses, shares, []), 2000)

    def test_accepted_share_becomes_payable_without_changing_balance(self):
        expenses = [{"id": 10, "amount": 4000, "payer_member_id": 1}]
        shares = [
            {"trip_expense_id": 10, "trip_member_id": 1, "amount": 2000, "status": "accepted"},
            {"trip_expense_id": 10, "trip_member_id": 2, "amount": 2000, "status": "accepted"},
        ]
        _, _, net = calculate_trip_balances(self.members, expenses, shares, [])
        self.assertEqual(net[1], 200000)
        self.assertEqual(net[2], -200000)
        self.assertEqual(trip_payables_for_user([2], expenses, shares, []), 2000)

    def test_rejected_share_is_excluded_and_stays_unresolved_for_payer(self):
        expenses = [{"id": 10, "amount": 4000, "payer_member_id": 1}]
        shares = [
            {"trip_expense_id": 10, "trip_member_id": 1, "amount": 2000, "status": "accepted"},
            {"trip_expense_id": 10, "trip_member_id": 2, "amount": 2000, "status": "rejected"},
        ]
        _, _, net = calculate_trip_balances(self.members, expenses, shares, [])
        self.assertEqual(net[1], 200000)
        self.assertEqual(net[2], 0)
        self.assertEqual(trip_payables_for_user([2], expenses, shares, []), 0)

    def test_guest_shares_keep_name_only_accounting(self):
        expenses = [{"id": 10, "amount": 900, "payer_member_id": 1}]
        shares = [
            {"trip_expense_id": 10, "trip_member_id": 1, "amount": 300, "status": "accepted"},
            {"trip_expense_id": 10, "trip_member_id": 3, "amount": 600, "status": "accepted"},
        ]
        _, _, net = calculate_trip_balances(self.members, expenses, shares, [])
        self.assertEqual(net[1], 60000)
        self.assertEqual(net[3], -60000)

    def test_pending_settlement_does_not_reduce_outstanding(self):
        expenses = [{"id": 10, "amount": 4000, "payer_member_id": 1}]
        shares = [
            {"trip_expense_id": 10, "trip_member_id": 1, "amount": 2000, "status": "accepted"},
            {"trip_expense_id": 10, "trip_member_id": 2, "amount": 2000, "status": "accepted"},
        ]
        settlement = {"from_member_id": 2, "to_member_id": 1, "amount": 1000, "status": "pending"}
        _, _, net = calculate_trip_balances(self.members, expenses, shares, [settlement])
        self.assertEqual(net[2], -200000)

    def test_paid_settlement_clears_exact_balance(self):
        expenses = [{"id": 10, "amount": 4000, "payer_member_id": 1}]
        shares = [
            {"trip_expense_id": 10, "trip_member_id": 1, "amount": 2000, "status": "accepted"},
            {"trip_expense_id": 10, "trip_member_id": 2, "amount": 2000, "status": "accepted"},
        ]
        settlement = {"from_member_id": 2, "to_member_id": 1, "amount": 2000, "status": "paid"}
        _, _, net = calculate_trip_balances(self.members, expenses, shares, [settlement])
        self.assertEqual(net, {1: 0, 2: 0, 3: 0})
        self.assertEqual(trip_payables_for_user([2], expenses, shares, [settlement]), 0)

    def test_multiple_members_simplify_to_fewest_payments(self):
        result = suggested_settlements({1: 10000, 2: -6000, 3: -4000})
        self.assertEqual(len(result), 2)
        self.assertEqual(sum(item["amount"] for item in result), 100)

    def test_removed_friend_keeps_historical_share_by_stable_member_id(self):
        removed_member = {"id": 2, "role": "member", "active": False, "display_name": "Rohan"}
        expenses = [{"id": 10, "amount": 2000, "payer_member_id": 1}]
        shares = [
            {"trip_expense_id": 10, "trip_member_id": 1, "amount": 1000, "status": "accepted"},
            {"trip_expense_id": 10, "trip_member_id": 2, "amount": 1000, "status": "accepted"},
        ]
        _, _, net = calculate_trip_balances([self.members[0], removed_member], expenses, shares, [])
        self.assertEqual(net[2], -100000)


if __name__ == "__main__":
    unittest.main()

