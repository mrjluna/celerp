# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""A subscription template's dates are real dates on every door: create, edit, import and the
schedule page. Text that is not a date is refused (422) and never stored, so "Next issue date: kk"
cannot reach a page or History."""
from __future__ import annotations

import json

import pytest

from agreement_kit import LINE, agreement, freeze_day, register, template

pytestmark = pytest.mark.asyncio

TYPES = ("subscription_invoice", "subscription_po")
DATE_FIELDS = ("start_date", "end_date", "next_bill_date")


def _body(doc_type: str, **extra) -> dict:
    return {"doc_type": doc_type, "contact_id": "contact:test-001", "frequency": "monthly",
            "start_date": "2026-12-01", "line_items": [dict(LINE)], **extra}


@pytest.mark.parametrize("doc_type", TYPES)
@pytest.mark.parametrize("field", DATE_FIELDS)
async def test_create_refuses_text_that_is_not_a_date(client, doc_type, field):
    hd = await register(client)
    for bad in ("kk", "2026-13-45", "01/12/2026"):
        r = await client.post("/docs", json=_body(doc_type, **{field: bad}), headers=hd)
        assert r.status_code == 422, (field, bad, r.text)
        assert bad in r.text


@pytest.mark.parametrize("doc_type", TYPES)
@pytest.mark.parametrize("field", DATE_FIELDS)
async def test_edit_refuses_text_that_is_not_a_date_and_keeps_the_stored_value(client, doc_type, field):
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-12-01", doc_type=doc_type, activate=False)
    before = (await template(client, hd, eid)).get(field)
    r = await client.patch(f"/docs/{eid}", json={"fields_changed": {field: {"old": before, "new": "kk"}}}, headers=hd)
    assert r.status_code == 422, r.text
    assert (await template(client, hd, eid)).get(field) == before


@pytest.mark.parametrize("doc_type", TYPES)
async def test_a_real_date_is_saved_and_shown(client, doc_type):
    hd = await register(client)
    eid = await agreement(client, hd, start="2026-12-01", doc_type=doc_type, activate=False, end_date="2027-12-01")
    state = await template(client, hd, eid)
    assert (state["start_date"], state["end_date"]) == ("2026-12-01", "2027-12-01")
    r = await client.patch(f"/docs/{eid}", json={"fields_changed": {"start_date": {"old": "2026-12-01", "new": "2027-01-15"}}}, headers=hd)
    assert r.status_code == 200, r.text
    assert (await template(client, hd, eid))["start_date"] == "2027-01-15"


@pytest.mark.parametrize("doc_type", TYPES)
async def test_the_snapshot_import_door_refuses_a_non_date_too(client, doc_type):
    hd = await register(client)
    r = await client.post("/docs/import", json={"entity_id": "doc:SUB-X", "event_type": "doc.created",
                                                "data": _body(doc_type, start_date="kk"), "source": "import",
                                                "idempotency_key": "kk-1"}, headers=hd)
    assert r.status_code in (404, 405, 422), r.text
    assert r.status_code != 200


async def test_the_schedule_page_refuses_a_non_date_and_stores_nothing(owner_ui, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    eid = await agreement(owner_ui.api, {}, start="2026-04-01")
    page = (await owner_ui.get(f"/subscriptions/{eid}")).text
    for field in ("anchor_date", "trial_end", "term_start", "term_end"):
        assert f'hx-patch="/subscriptions/{eid}/schedule/{field}"' in page
        control = page[page.rindex("<input", 0, page.index(f"/schedule/{field}")): page.index(f"/schedule/{field}")]
        assert 'type="date"' in control, (field, control)
    r = await owner_ui.patch(f"/subscriptions/{eid}/schedule/anchor_date", data={"value": "kk"})
    # The refusal is said in the toast the page shows; the redrawn control holds the stored date.
    toast = json.loads(r.headers["hx-trigger"])["celerpToast"]
    assert toast["type"] == "error" and "anchor_date" in toast["message"], toast
    assert 'value="kk"' not in r.text
    state = (await owner_ui.api.get(f"/docs/{eid}")).json()
    assert state["anchor_date"] == "2026-04-01"


async def test_the_template_header_shows_only_the_derived_next_issue_date(owner_ui, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    eid = await agreement(owner_ui.api, {}, start="2026-04-01")
    html = (await owner_ui.get(f"/docs/{eid}")).text
    assert "kk" not in html
    assert "2026-04-01" in html
