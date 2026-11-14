# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Role-gated cost redaction for activity/history payloads.

Cost (``cost_price`` / ``cost_total`` and any cost price list) is visible only to manager+
everywhere else in the app (item detail, list, dashboard cost basis). The ledger/activity feeds must honour
the same rule, enforced **server-side** so raw cost never ships to an under-threshold
role. This is the single source of truth for that redaction, used by both the ledger
router and the dashboard activity endpoints.

Every ledger reader (the event list, a single event, the dashboard activity and History)
shows an event only to a role holding the permission it is read with: the one its module names
on its ``projection_handler`` contribution, enabled or not, or core's own for the events no
module owns.
"""

from __future__ import annotations

from sqlalchemy import Select, case, false, true

from celerp.events.schemas import ItemPricingSet
from celerp.services.cost_visibility import COST_DERIVED_ITEM_KEYS, COST_ITEM_KEYS
from celerp.services.permissions import role_has_permission
from celerp.services.pricing import is_cost_price_type

# Every amount an item.pricing.set carries (all its fields but price_type), so an amount
# field added to the event later is redacted with the rest.
_PRICING_AMOUNT_KEYS = frozenset(ItemPricingSet.model_fields) - {"price_type"}

# The one cost-key registry for event data: item cost and its companions (the keys the item
# API strips), COGS on fulfillment, manufacturing unit cost and transform cost totals. A key
# starting with ``cost_`` or naming a cost price list is a cost key too. Sell prices
# (retail_price etc.) are deliberately excluded - they are visible to all roles, matching
# item-level visibility.
COST_DATA_KEYS = COST_ITEM_KEYS | COST_DERIVED_ITEM_KEYS | frozenset(
    {"total_cogs", "cogs", "unit_cost", "parent_cost_total", "child_cost_total"})


def is_cost_key(key) -> bool:
    """Whether a key of event data, at any depth, holds a cost."""
    key = str(key)
    return key in COST_DATA_KEYS or key.startswith("cost_") or is_cost_price_type(key)

# The events no module owns, each read with the permission of the records it changes. The
# longest prefix an event type starts with decides.
_CORE_EVENT_PERMISSIONS = {
    "sys.older_stock.": "view_inventory",
    "sys.": "manage_company_settings",
    "payment_batch.": "view_payments",
    "line_action.": "view_documents",
    "sub.": "view_subscriptions",
    "bom.": "manage_manufacturing",
}
# An event that neither an installed module nor core names (a removed module's) is read by
# those who manage the company.
_UNOWNED_EVENT_PERMISSION = "manage_company_settings"


def _event_permissions() -> list[tuple[str, str]]:
    """(prefix, permission) for every known event prefix, longest first."""
    from celerp.modules import slots
    from celerp.modules.loader import event_read_permissions

    found = {**_CORE_EVENT_PERMISSIONS, **event_read_permissions(),
             **{c["prefix"]: c["permission"] for c in slots.get("projection_handler")}}
    return sorted(found.items(), key=lambda kv: -len(kv[0]))


def can_read_event(event_type: str, settings: dict | None, role: str | None) -> bool:
    """Whether this role may read an event of ``event_type``."""
    permission = next((perm for prefix, perm in _event_permissions() if event_type.startswith(prefix)),
                      _UNOWNED_EVENT_PERMISSION)
    return role_has_permission(settings, role or "", permission)


def readable_events(stmt: Select, settings: dict | None, role: str | None) -> Select:
    """``stmt`` over ledger entries narrowed to the events this role can read."""
    from celerp.models.ledger import LedgerEntry

    held = lambda perm: role_has_permission(settings, role or "", perm)  # noqa: E731
    rules = [(LedgerEntry.event_type.startswith(prefix, autoescape=True), true() if held(perm) else false())
             for prefix, perm in _event_permissions()]
    return stmt.where(case(*rules, else_=true() if held(_UNOWNED_EVENT_PERMISSION) else false()))


def can_see_costs(settings: dict | None, role: str | None) -> bool:
    """True when the role holds view_inventory_costs for this company."""
    return role_has_permission(settings, role or "", "view_inventory_costs")


def _without_costs(value):
    """``value`` with every cost key dropped from every dict at every depth, through lists."""
    if isinstance(value, dict):
        return {k: _without_costs(v) for k, v in value.items() if not is_cost_key(k)}
    if isinstance(value, list):
        return [_without_costs(v) for v in value]
    return value


def redact_event_costs(event_type: str, data: dict) -> dict:
    """A copy of ``data`` with every cost amount stripped; rows are kept (counts and pagination
    stay intact), only cost numbers go. An ``item.pricing.set`` for a cost price type loses every
    amount and is marked ``cost_redacted``; any other event loses every cost key at every depth
    of its data (line items, attributes, old and new values alike).

    Read-time only: projection replay reads the raw ledger directly, so cost stays intact for
    state rebuilds; this keeps cost out of what a role without view_inventory_costs reads."""
    if not isinstance(data, dict):
        return data
    if event_type == "item.pricing.set" and is_cost_price_type(str(data.get("price_type") or "")):
        out = {k: v for k, v in data.items() if k not in _PRICING_AMOUNT_KEYS}
        out["cost_redacted"] = True
        return out
    return _without_costs(data)


def event_data_for_role(event_type: str, data, settings: dict | None, role: str | None):
    """An event's data as this role reads it: whole for a role that sees costs, else redacted.
    Every ledger reader returns event data through this."""
    return data if can_see_costs(settings, role) else redact_event_costs(event_type, data)


def fields_for_role(fields, settings: dict | None, role: str | None) -> list[str]:
    """Changed-field names as this role may filter or choose by: no cost field for a role that
    cannot see costs."""
    return list(fields) if can_see_costs(settings, role) else [f for f in fields if not is_cost_key(f)]


def redact_entries_for_role(entries: list[dict], settings: dict | None, role: str | None) -> list[dict]:
    """:func:`event_data_for_role` over serialized ledger entries carrying ``event_type`` and
    ``data``."""
    for e in entries:
        e["data"] = event_data_for_role(str(e.get("event_type") or ""), e.get("data") or {}, settings, role)
    return entries
