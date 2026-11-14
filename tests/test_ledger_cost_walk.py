# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A role without view_inventory_costs reads no cost amount from the ledger, at any depth of an
event's data: a line's cost inside a document's changed lines, a cost inside an item's changed
attributes, a cost list inside a list. Every reader returns event data through the one walk
(activity_redaction.event_data_for_role) over the one cost-key registry (is_cost_key)."""
from __future__ import annotations

import json
import random
import uuid

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import select

from celerp.services import activity_redaction
from celerp.services.activity_redaction import is_cost_key, redact_event_costs
from test_helpers import invite_user, register_admin, ui_signed_in

_DOC, _ITEM = "doc:cost-walk", "item:cost-walk"
_AMOUNTS = (98765.43, 87654.32, 76543.21, 65432.19, 54321.09, 43210.98, 32109.87, 21098.76)


def _line(sku: str, cost: float, unit_cost: float) -> dict:
    return {"sku": sku, "name": f"Line {sku}", "quantity": 2, "unit_price": 10.0,
            "cost_price": cost, "unit_cost": unit_cost, "attributes": {"landed_price": cost + 1}}


def _events(company_id) -> list:
    from celerp.models.ledger import LedgerEntry

    def entry(entity_id, entity_type, event_type, data):
        return LedgerEntry(company_id=company_id, entity_id=entity_id, entity_type=entity_type,
                           event_type=event_type, data=data, source="test",
                           idempotency_key=f"cost-walk-{uuid.uuid4()}")
    a = _AMOUNTS
    return [
        entry(_DOC, "doc", "doc.updated", {"fields_changed": {
            "line_items": {"old": [_line("A", a[0], a[1])], "new": [_line("A", a[2], a[3])]},
            "notes": {"old": "Before", "new": "After"},
        }}),
        entry(_ITEM, "item", "item.updated", {"fields_changed": {
            "attributes": {"old": {"pieces": 3, "cost_price": a[4], "landed_costs": [{"amount": a[5]}]},
                           "new": {"pieces": 2, "cost_price": a[6], "landed_costs": [{"amount": a[7]}]}},
            "name": {"old": "Old name", "new": "New name"},
        }}),
    ]


def _hides(text: str) -> bool:
    return not any(s in text for v in _AMOUNTS for s in (f"{v}", f"{v:.2f}", f"{v:,.2f}"))


async def _setup(client, session):
    owner = {"Authorization": f"Bearer {await register_admin(client)}"}
    token = await invite_user(client, session, owner, "op@cost-walk.example", "operator")
    from celerp.models.company import Company
    company_id = (await session.execute(select(Company.id).where(Company.name == "Perm Co"))).scalar_one()
    session.add_all(_events(company_id))
    await session.flush()
    return owner, {"Authorization": f"Bearer {token}"}, token


async def _readers(client, headers) -> dict[str, str]:
    """Every ledger reader's response text, for one role."""
    out = {}
    for name, (path, params) in {
        "ledger list": ("/ledger", {"limit": 500}),
        "ledger list (doc)": ("/ledger", {"entity_id": _DOC}),
        "ledger list (item)": ("/ledger", {"entity_id": _ITEM}),
        "facets": ("/ledger/facets", {"entity_id": _ITEM}),
        "dashboard activity": ("/dashboard/activity", {"limit": 100}),
        "dashboard activity search": ("/dashboard/activity/search", {"q": "cost-walk"}),
    }.items():
        r = await client.get(path, params=params, headers=headers)
        assert r.status_code == 200, (name, r.text)
        out[name] = r.text
    for e in (await client.get("/ledger", params={"limit": 500}, headers=headers)).json()["items"]:
        if e["entity_id"] in (_DOC, _ITEM):
            r = await client.get(f"/ledger/{e['id']}", headers=headers)
            assert r.status_code == 200, r.text
            out[f"single event {e['entity_id']}"] = r.text
    return out


@pytest.mark.asyncio
async def test_nested_line_and_attribute_costs_reach_no_reader_of_a_role_without_costs(client, session):
    owner, operator, token = await _setup(client, session)
    for name, text in (await _readers(client, operator)).items():
        assert _hides(text), name
    doc = (await client.get("/ledger", params={"entity_id": _DOC}, headers=operator)).json()["items"][0]["data"]
    line = doc["fields_changed"]["line_items"]["new"][0]
    assert line == {"sku": "A", "name": "Line A", "quantity": 2, "unit_price": 10.0, "attributes": {}}
    item = (await client.get("/ledger", params={"entity_id": _ITEM}, headers=operator)).json()["items"][0]["data"]
    assert item["fields_changed"]["attributes"] == {"old": {"pieces": 3}, "new": {"pieces": 2}}
    # The owner reads every amount: the walk narrows by role, never the stored event.
    shown = "".join((await _readers(client, owner)).values())
    assert all(f"{v}" in shown for v in _AMOUNTS)


@pytest.mark.asyncio
async def test_history_shows_no_nested_cost_to_a_role_without_costs(client, session):
    owner, operator, token = await _setup(client, session)
    async with ui_signed_in(token) as ui:
        for path in (f"/docs/{_DOC}/history?per_page=100", f"/docs/{_ITEM}/history?per_page=100", "/history"):
            r = await ui.get(path)
            assert r.status_code == 200, (path, r.text[:300])
            text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
            assert _hides(text) and _hides(r.text), path
        assert "After" in BeautifulSoup((await ui.get(f"/docs/{_DOC}/history?per_page=100")).text,
                                        "html.parser").get_text(" ", strip=True)


_PLAIN = ("name", "sku", "qty", "price", "retail_price", "pieces", "notes", "line_items", "attributes",
          "old", "new", "items", "value", "fields_changed")
_COST = ("cost_price", "cost_total", "unit_cost", "total_cogs", "cogs", "cost_base", "cost_landed",
         "landed_costs", "goods_cost_unknown", "parent_cost_total", "child_cost_total", "cost_anything",
         "landed_price", "cost")


def _payload(rnd: random.Random, depth: int):
    """A random event payload: dicts and lists nested up to ``depth`` levels, cost keys among
    plain ones at every level."""
    if depth == 0 or rnd.random() < 0.2:
        return rnd.choice([rnd.randint(0, 10**6) / 100, "text", None, True])
    if rnd.random() < 0.35:
        return [_payload(rnd, depth - 1) for _ in range(rnd.randint(0, 3))]
    return {rnd.choice(_COST if rnd.random() < 0.35 else _PLAIN): _payload(rnd, depth - 1)
            for _ in range(rnd.randint(0, 4))}


def _leaves(value, path=()):
    """Every (path, leaf) of a payload; a dict key and a list index each extend the path."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _leaves(v, (*path, ("k", k)))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _leaves(v, (*path, ("i", i)))
    else:
        yield path, value


def _expected_leaves(data) -> set:
    """The reference: every leaf whose path passes no cost key."""
    return {json.dumps([path, leaf]) for path, leaf in _leaves(data)
            if not any(kind == "k" and is_cost_key(k) for kind, k in path)}


def _check(data, out):
    keys = {k for path, _ in _leaves(out) for kind, k in path if kind == "k"}
    assert not any(is_cost_key(k) for k in keys), keys
    assert {json.dumps([p, v]) for p, v in _leaves(out)} == _expected_leaves(data)


_SEED, _RUNS, _HTTP_RUNS = 4270514, 500, 30


def test_the_walk_drops_every_cost_key_at_every_depth_and_keeps_the_rest():
    """Seeded generative check over random nesting (dicts and lists, depth up to 5)."""
    rnd = random.Random(_SEED)
    for _ in range(_RUNS):
        data = {"fields_changed": _payload(rnd, 5), **{rnd.choice(_COST + _PLAIN): _payload(rnd, 4)}}
        _check(data, redact_event_costs("item.updated", data))


@pytest.mark.asyncio
async def test_random_nested_costs_never_leave_the_ledger_for_a_role_without_costs(client, session):
    """The same check through the real reader: random payloads stored as events and read back
    over /ledger by an operator."""
    owner, operator, _ = await _setup(client, session)
    from celerp.models.company import Company
    from celerp.models.ledger import LedgerEntry
    company_id = (await session.execute(select(Company.id).where(Company.name == "Perm Co"))).scalar_one()
    rnd = random.Random(_SEED + 1)
    sent = {}
    for n in range(_HTTP_RUNS):
        data = {"fields_changed": _payload(rnd, 5), "extra": _payload(rnd, 4)}
        entity = f"item:walk-{n}"
        sent[entity] = data
        session.add(LedgerEntry(company_id=company_id, entity_id=entity, entity_type="item",
                                event_type="item.updated", data=data, source="test",
                                idempotency_key=f"walk-{uuid.uuid4()}"))
    await session.flush()
    for entity, data in sent.items():
        items = (await client.get("/ledger", params={"entity_id": entity}, headers=operator)).json()["items"]
        _check(json.loads(json.dumps(data)), items[0]["data"])


@pytest.mark.asyncio
async def test_every_ledger_reader_returns_data_through_the_walk(client, session, monkeypatch):
    """With every key counted as a cost, each ledger reader returns no event data at all: so each
    one reads through the walk and its registry. A ledger read route not listed here fails."""
    from celerp.main import app
    readers = {"/ledger", "/ledger/facets", "/ledger/{entry_id}", "/dashboard/activity",
               "/dashboard/activity/search"}
    routes = {path for path, ops in app.openapi()["paths"].items()
              if "get" in ops and path.startswith(("/ledger", "/dashboard/activity"))}
    assert routes == readers
    owner, operator, _ = await _setup(client, session)
    monkeypatch.setattr(activity_redaction, "is_cost_key", lambda key: True)
    for name, text in (await _readers(client, operator)).items():
        body = json.loads(text)
        datas = [e["data"] for e in body.get("items") or body.get("activities") or ([body] if "data" in body else [])]
        assert all(d == {} for d in datas) and body.get("fields", []) == [], (name, body)
    assert (await client.get("/ledger", headers=operator)).json()["items"]
