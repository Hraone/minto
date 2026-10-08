"""Pure Trip Mode accounting helpers shared by routes and unit tests.

Money is converted to whole paise before sums are compared. A trip expense is
allocated to its payer and shares; pending shares count as outstanding as soon
as assigned, while rejected shares are excluded from the debt calculation.
"""

from collections import defaultdict


def to_paise(value):
    try:
        return int(round(float(value or 0) * 100))
    except (TypeError, ValueError, OverflowError):
        return 0


def member_aliases(members):
    """Map stable member ids and legacy display names to stable ids."""
    aliases = {}
    for member in members:
        member_id = member.get("id") or member.get("member_id")
        if member_id is None:
            continue
        aliases[str(member_id)] = member_id
        for value in (member.get("display_name"), member.get("guest_name"), member.get("name")):
            if value:
                aliases[str(value).strip().casefold()] = member_id
        if member.get("role") == "owner":
            aliases["you"] = member_id
    return aliases


def calculate_trip_balances(members, expenses, shares, settlements, legacy_splits=()):
    """Return paid, owed and net paise keyed by trip_member id.

    `paid` counts the full trip bill against the member recorded as payer.
    `owed` counts pending, accepted and settled shares. Rejected shares are
    excluded. Paid settlements transfer net between members; pending
    settlements do not.
    Legacy name-only expenses are resolved through aliases populated from the
    additive migration's trip_members snapshots.
    """
    aliases = member_aliases(members)
    ids = [m.get("id") or m.get("member_id") for m in members]
    ids = [mid for mid in ids if mid is not None]
    paid = {mid: 0 for mid in ids}
    owed = {mid: 0 for mid in ids}

    def resolve(value):
        if value is None:
            return None
        return aliases.get(str(value)) or aliases.get(str(value).strip().casefold())

    for expense in expenses:
        payer_id = expense.get("payer_member_id") or resolve(expense.get("paid_by"))
        if payer_id in paid:
            paid[payer_id] += to_paise(expense.get("amount"))

    expense_ids_with_shares = set()
    for share in shares:
        expense_id = share.get("trip_expense_id")
        expense_ids_with_shares.add(expense_id)
        if share.get("status") not in ("pending", "accepted", "settled"):
            continue
        member_id = share.get("trip_member_id") or share.get("member_id") or resolve(share.get("participant_name"))
        if member_id in owed:
            owed[member_id] += to_paise(share.get("amount", share.get("share_amount")))

    # Old rows not yet represented in the new share table still count.
    for split in legacy_splits:
        if split.get("trip_expense_id") in expense_ids_with_shares:
            continue
        member_id = split.get("trip_member_id") or resolve(split.get("participant_name"))
        if member_id in owed:
            owed[member_id] += to_paise(split.get("share_amount"))

    net = {mid: paid[mid] - owed[mid] for mid in ids}
    for settlement in settlements:
        if settlement.get("status") != "paid":
            continue
        amount = to_paise(settlement.get("amount"))
        from_id = settlement.get("from_member_id") or resolve(settlement.get("from_person"))
        to_id = settlement.get("to_member_id") or resolve(settlement.get("to_person"))
        if amount > 0 and from_id in net and to_id in net and from_id != to_id:
            net[from_id] += amount
            net[to_id] -= amount

    return paid, owed, net


def suggested_settlements(net_paise):
    """Minimize the number of payments needed to clear a net balance map."""
    creditors = sorted(
        ([member_id, amount] for member_id, amount in net_paise.items() if amount > 0),
        key=lambda pair: -pair[1],
    )
    debtors = sorted(
        ([member_id, -amount] for member_id, amount in net_paise.items() if amount < 0),
        key=lambda pair: -pair[1],
    )
    payments = []
    i = j = 0
    while i < len(creditors) and j < len(debtors):
        amount = min(creditors[i][1], debtors[j][1])
        payments.append({
            "from_member_id": debtors[j][0],
            "to_member_id": creditors[i][0],
            "amount": amount / 100,
        })
        creditors[i][1] -= amount
        debtors[j][1] -= amount
        if creditors[i][1] == 0:
            i += 1
        if debtors[j][1] == 0:
            j += 1
    return payments


def trip_payables_for_user(member_ids, expenses, shares, settlements):
    """Unpaid pending or accepted responsibility belonging to one user's trip rows."""
    member_ids = set(member_ids)
    payer_by_expense = {e.get("id"): e.get("payer_member_id") for e in expenses}
    owed = defaultdict(int)
    for share in shares:
        member_id = share.get("trip_member_id") or share.get("member_id")
        if member_id not in member_ids or share.get("status") not in ("pending", "accepted", "settled"):
            continue
        if payer_by_expense.get(share.get("trip_expense_id")) == member_id:
            continue
        owed[member_id] += to_paise(share.get("amount", share.get("share_amount")))
    for settlement in settlements:
        if settlement.get("status") == "paid" and settlement.get("from_member_id") in member_ids:
            owed[settlement["from_member_id"]] -= to_paise(settlement.get("amount"))
    return sum(max(amount, 0) for amount in owed.values()) / 100

