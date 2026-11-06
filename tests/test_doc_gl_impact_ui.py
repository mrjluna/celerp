# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The GL Impact section on a document's detail page.

The page carries a lazy-load placeholder only for a role that holds
view_financial_reports; the section itself renders each posted entry with its
accounts, party and a link back to the journal, and says why when there is
nothing to show: a draft that will post once issued, or a document that never
reaches the books.
"""
from __future__ import annotations

import pytest
from fasthtml.common import to_xml

from ui.i18n import t
from ui.routes.documents import _doc_detail, _doc_gl_impact_section

_PLACEHOLDER = "/gl-impact"


def _page(doc_type="invoice", role="owner", settings=None) -> str:
    doc = {"entity_id": f"doc:{doc_type}-1", "doc_type": doc_type, "status": "draft",
           "ref_id": "D-1", "line_items": []}
    return to_xml(_doc_detail(doc, role=role, settings=settings))


def test_detail_page_loads_gl_impact_for_a_role_with_accounting_access():
    assert f"/docs/doc:invoice-1{_PLACEHOLDER}" in _page(role="owner")


def test_detail_page_hides_gl_impact_from_a_role_without_it():
    assert _PLACEHOLDER not in _page(role="viewer")


def test_detail_page_shows_gl_impact_to_a_role_granted_it():
    grants = {"role_grants": {"view_financial_reports": ["viewer", "operator", "manager", "admin", "owner"]}}
    assert _PLACEHOLDER in _page(role="viewer", settings=grants)


def test_lists_never_get_a_gl_impact_section():
    assert _PLACEHOLDER not in _page(doc_type="list", role="owner")


_POSTED = {
    "doc_id": "doc:inv-1", "doc_type": "invoice", "status": "final", "currency": "USD",
    "entries": [
        {"je_id": "je:auto:doc:inv-1", "ts": "2026-02-01", "memo": "Invoice INV-1", "status": "posted",
         "source_doc": {"doc_id": "doc:inv-1", "doc_ref": "INV-1"},
         "lines": [
             {"account": "1120", "name": "Accounts Receivable", "debit": 100.0, "credit": 0.0,
              "party_id": "contact:1", "party_name": "Acme Ltd"},
             {"account": "4100", "name": "Sales", "debit": 0.0, "credit": 100.0, "party_id": "contact:1",
              "party_name": "Acme Ltd", "item": "Widget", "quantity": 2},
         ]},
        {"je_id": "je:auto:doc:inv-1:fulfill", "ts": "2026-02-02", "memo": "Cost of sales", "status": "void",
         "source_doc": {"doc_id": "doc:inv-1", "doc_ref": "INV-1"},
         "lines": [{"account": "5100", "name": "Cost of Goods Sold", "debit": 42.0, "credit": 0.0,
                    "party_id": "contact:1", "party_name": ""}]},
    ],
}


def test_posted_document_lists_accounts_party_items_and_journal_links():
    html = to_xml(_doc_gl_impact_section(_POSTED, "doc:inv-1", "USD"))
    for expected in ("1120 Accounts Receivable", "4100 Sales", "5100 Cost of Goods Sold", "Acme Ltd",
                     "Widget \u00d7 2", "je:auto:doc:inv-1", "je:auto:doc:inv-1:fulfill", t("documents.gl_impact_void")):
        assert expected in html, expected
    assert "/accounting?q=INV-1" in html
    # A line with no resolvable party name shows an empty cell, never the raw contact id.
    assert "contact:1" not in html


def test_each_entry_is_headed_by_its_document_and_date_not_its_journal_key():
    html = to_xml(_doc_gl_impact_section(_POSTED, "doc:inv-1", "USD"))
    assert ">Journal entry for INV-1, 1 Feb 2026<" in html and ">Journal entry for INV-1, 2 Feb 2026<" in html
    # The key stays reachable as the link's tooltip, never as the heading.
    assert 'title="je:auto:doc:inv-1"' in html and ">je:auto:doc:inv-1<" not in html


def test_each_entry_totals_its_debits_and_credits_and_says_whether_it_balances():
    html = to_xml(_doc_gl_impact_section(_POSTED, "doc:inv-1", "USD"))
    first, second = html.split('class="gl-impact-block"')[1:3]
    assert "$100.00" in first.split("<tfoot>")[1] and ">Balanced<" in first
    assert f">{t("acct.out_of_balance")}<" in second  # a one-sided entry is flagged, never hidden


def test_a_line_names_its_item_with_quantity_and_unit_not_the_raw_memo():
    data = {**_POSTED, "entries": [{**_POSTED["entries"][0], "lines": [
        {**_POSTED["entries"][0]["lines"][1], "quantity": 1.5, "unit": "kg"},
        {**_POSTED["entries"][0]["lines"][0]}]}]}
    html = to_xml(_doc_gl_impact_section(data, "doc:inv-1", "USD"))
    assert "Widget \u00d7 1.5" in html and "Invoice INV-1</td>" not in html


def test_draft_invoice_says_it_is_not_posted_yet():
    data = {"doc_id": "doc:inv-2", "doc_type": "invoice", "status": "draft", "entries": []}
    assert t("documents.gl_impact_not_posted") in to_xml(_doc_gl_impact_section(data, "doc:inv-2", "USD"))


@pytest.mark.parametrize("doc_type,status", [("quotation", "draft"), ("quotation", "final"), ("invoice", "final")])
def test_document_that_never_posts_says_it_does_not_affect_the_ledger(doc_type, status):
    data = {"doc_id": "doc:x", "doc_type": doc_type, "status": status, "entries": []}
    html = to_xml(_doc_gl_impact_section(data, "doc:x", "USD"))
    assert t("documents.gl_impact_none") in html
    assert t("documents.gl_impact_not_posted") not in html


# --- the lazy-loaded endpoint -------------------------------------------------

from unittest.mock import AsyncMock, patch  # noqa: E402

from test_ui import _COMPANY, _authed, ui_client  # noqa: E402,F401  (fixtures)
from ui.api_client import APIError  # noqa: E402


@pytest.mark.asyncio
async def test_endpoint_renders_the_section_for_an_accountant(ui_client):
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
         patch("ui.api_client.get_doc_gl_impact", new=AsyncMock(return_value=_POSTED)) as read:
        r = await ui_client.get("/docs/doc:inv-1/gl-impact")
    assert r.status_code == 200
    read.assert_awaited_once()
    assert "1120 Accounts Receivable" in r.text


@pytest.mark.asyncio
async def test_endpoint_refuses_a_role_without_view_financial_reports(ui_client):
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
         patch("ui.routes.documents._get_role", return_value="viewer"), \
         patch("ui.api_client.get_doc_gl_impact", new=AsyncMock(return_value=_POSTED)) as read:
        r = await ui_client.get("/docs/doc:inv-1/gl-impact")
    assert r.status_code == 403 and "Accounts Receivable" not in r.text
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_endpoint_shows_nothing_when_the_books_cannot_answer(ui_client):
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
         patch("ui.api_client.get_doc_gl_impact", new=AsyncMock(side_effect=APIError(404, "gone"))):
        r = await ui_client.get("/docs/doc:inv-1/gl-impact")
    assert r.status_code == 200 and "GL Impact" not in r.text


@pytest.mark.asyncio
async def test_endpoint_displays_a_ledger_failure_instead_of_an_empty_section(ui_client):
    ui_client.cookies.update(_authed())
    with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
         patch("ui.api_client.get_doc_gl_impact", new=AsyncMock(side_effect=APIError(503, "Unavailable"))):
        response = await ui_client.get("/docs/doc:inv-1/gl-impact")
    assert response.status_code == 200
    assert t("documents.gl_impact") in response.text
    assert "Unavailable" in response.text
