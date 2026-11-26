# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The item suggestions of a line open directly under the field being typed in (or above it when
there is no room below), fully visible and never clipped inside the table, on every document
type that takes lines, near the top of the window and with the field near its bottom."""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.browser

KINDS = [("doc", "quotation"), ("doc", "invoice"), ("doc", "purchase_order"), ("doc", "bill"),
         ("list", "transfer"), ("doc", "production_order")]

_MEASURE = """() => {
    const row = document.querySelector('tr:has([data-name=description])');
    const input = row.querySelector('input[data-name=description]');
    const list = input.parentElement.querySelector('.catalog-ac-list');
    const f = input.getBoundingClientRect(), l = list.getBoundingClientRect();
    const cx = l.left + l.width / 2, cy = l.top + Math.min(l.height - 2, 12);
    const top = document.elementFromPoint(cx, cy);
    return {fieldTop: f.top, fieldBottom: f.bottom, fieldLeft: f.left, top: l.top, bottom: l.bottom, left: l.left,
            right: l.right, width: l.width, height: l.height, vw: document.documentElement.clientWidth,
            vh: window.innerHeight, shown: getComputedStyle(list).display !== 'none',
            reachable: !!top && list.contains(top)};
}"""


def _make(api, kind: str, name: str) -> str:
    if kind[0] == "list":
        r = api.post("/lists", json={"list_type": kind[1], "status": "draft", "line_items": []})
    else:
        r = api.post("/docs", json={"doc_type": kind[1], "line_items": []})
    assert r.status_code in (200, 201), r.text
    return r.json().get("id") or r.json().get("entity_id")


@pytest.fixture(scope="module")
def stocked(api):
    tag = uuid.uuid4().hex[:6]
    for n in range(5):
        r = api.post("/items", json={"status": "available", "sku": f"ACP-{tag}-{n}", "name": f"Popup widget {tag} {n}",
                                     "quantity": 5, "sell_by": "piece"})
        assert r.status_code in (200, 201), r.text
    return f"Popup widget {tag}"


def _open(page, ui_server, kind, eid, query):
    page.goto(f"{ui_server}/{'lists' if kind[0] == 'list' else 'docs'}/{eid}", wait_until="load")
    field = page.locator("input[data-name=description]").first
    field.click()
    field.press_sequentially(query, delay=20)
    page.locator(".catalog-ac-list .catalog-ac-option").first.wait_for(state="visible", timeout=8000)
    page.wait_for_timeout(200)
    return field


def _assert_beside_field(m: dict):
    assert m["shown"] and m["height"] > 0, m
    under = abs(m["top"] - m["fieldBottom"]) <= 6
    over = abs(m["bottom"] - m["fieldTop"]) <= 6
    assert under or over, f"suggestions are not against the field: {m}"
    assert m["top"] >= -1 and m["bottom"] <= m["vh"] + 1, f"suggestions leave the window: {m}"
    assert m["left"] >= -1 and m["right"] <= m["vw"] + 1, f"suggestions leave the window: {m}"
    assert m["reachable"], f"suggestions are covered or clipped: {m}"


@pytest.mark.parametrize("kind", KINDS, ids=[k[1] for k in KINDS])
def test_suggestions_open_under_the_field(page, ui_server, api, stocked, kind):
    page.set_viewport_size({"width": 1280, "height": 800})
    eid = _make(api, kind, "x")
    _open(page, ui_server, kind, eid, stocked)
    _assert_beside_field(page.evaluate(_MEASURE))


@pytest.mark.parametrize("kind", [("doc", "quotation"), ("doc", "bill")], ids=["quotation", "bill"])
def test_suggestions_open_above_a_field_near_the_bottom_of_the_window(page, ui_server, api, stocked, kind):
    page.set_viewport_size({"width": 1280, "height": 800})
    eid = _make(api, kind, "x")
    page.goto(f"{ui_server}/docs/{eid}", wait_until="load")
    # Lift the field to the bottom of the window.
    page.evaluate("""() => {
        const f = document.querySelector('input[data-name=description]');
        const spacer = document.createElement('div');
        spacer.style.height = '3000px';
        document.getElementById('main-content').append(spacer);
        f.scrollIntoView({block: 'start'});
        const r = f.getBoundingClientRect();
        (function up(el) {
            while (el && !(el.scrollHeight > el.clientHeight && /(auto|scroll)/.test(getComputedStyle(el).overflowY))) el = el.parentElement;
            (el || document.scrollingElement).scrollBy(0, -(window.innerHeight - r.height - 20 - r.top));
        })(f.parentElement);
    }""")
    page.wait_for_timeout(200)
    field = page.locator("input[data-name=description]").first
    field.click()
    field.press_sequentially(stocked, delay=20)
    page.locator(".catalog-ac-list .catalog-ac-option").first.wait_for(state="visible", timeout=8000)
    page.wait_for_timeout(200)
    m = page.evaluate(_MEASURE)
    _assert_beside_field(m)
    assert m["bottom"] <= m["fieldTop"] + 6, f"expected the suggestions above the field: {m}"


def test_suggestions_open_under_the_field_at_phone_width(page, ui_server, api, stocked):
    page.set_viewport_size({"width": 390, "height": 800})
    eid = _make(api, ("doc", "quotation"), "x")
    _open(page, ui_server, ("doc", "quotation"), eid, stocked)
    _assert_beside_field(page.evaluate(_MEASURE))
