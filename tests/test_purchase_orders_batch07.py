# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Batch 07 purchase order tests through the document HTTP endpoints."""
from datetime import datetime, timezone

import pytest
from fasthtml.common import to_xml

from test_helpers import invite_user, register_admin


async def _doc(client, auth, kind):
    response = await client.post("/docs", headers=auth, json={
        "doc_type": kind, "line_items": [{"name": "Cable", "quantity": 2, "unit_price": 10, "tax_rate": 0}],
        "contact_email": "vendor@example.test",
    })
    assert response.status_code in {200, 201}, response.text
    return response.json()["id"]


@pytest.mark.asyncio
async def test_po_email_links_to_purchase_order_print(client, monkeypatch):
    from celerp_docs import routes, routes_share

    async def local_view(token):
        return f"/share/{token}"

    mail = {}
    monkeypatch.setattr(routes_share, "public_view_url", local_view)
    monkeypatch.setattr(routes, "_email_with_receipt", lambda *args, **kwargs: mail.update(kwargs))
    auth = {"Authorization": f"Bearer {await register_admin(client)}"}
    po = await _doc(client, auth, "purchase_order")
    state = (await client.get(f"/docs/{po}", headers=auth)).json()
    shared = await client.post(f"/docs/{po}/share", headers=auth, json={})
    assert shared.status_code == 200, shared.text
    assert (await client.get(f"/docs/{po}", headers=auth)).json()["status"] == "draft"

    response = await client.post(f"/docs/{po}/send", headers=auth, json={"sent_to": state["contact_email"]})
    assert response.status_code == 200, response.text
    assert mail["to"] == "vendor@example.test"
    assert mail["subject"].startswith(f"Purchase Order #{state['ref_id']}")
    assert "Purchase Order" in mail["body_text"]
    shared_url = next(word for word in mail["body_text"].split() if word.startswith("/share/"))
    printed = await client.get(shared_url)
    assert printed.status_code == 200, printed.text
    assert "Purchase Order" in printed.text
    assert state["ref_id"] in printed.text


def test_po_send_modal_previews_vendor_email_and_purchase_order_copy():
    from ui.routes.documents import _doc_detail

    rendered = to_xml(_doc_detail({
        "entity_id": "doc:PO-1000", "doc_type": "purchase_order", "status": "draft", "ref_id": "PO-1000",
        "contact_email": "vendor@example.test", "company_name": "Purchasing Co",
    }, role="owner", share_enabled=True))
    assert 'name="sent_to" value="vendor@example.test"' in rendered
    assert "Purchase Order #PO-1000 from Purchasing Co" in rendered
    assert "Here is Purchase Order #PO-1000." in rendered


@pytest.mark.asyncio
async def test_viewer_cannot_send_or_mark_po_sent(client, session):
    from ui.routes.documents import _doc_detail

    auth = {"Authorization": f"Bearer {await register_admin(client)}"}
    response = await client.patch("/companies/me/role-permissions", headers=auth, json={
        "perm_key": "edit_documents", "role_key": "viewer", "granted": False,
    })
    assert response.status_code == 200, response.text
    po = await _doc(client, auth, "purchase_order")
    viewer = {"Authorization": f"Bearer {await invite_user(client, session, auth, 'no-send@example.test', 'viewer')}"}
    state = (await client.get(f"/docs/{po}", headers=auth)).json()
    rendered = to_xml(_doc_detail({**state, "entity_id": po}, role="viewer", share_enabled=True))
    assert "send-modal-" not in rendered
    assert "/action/mark_sent" not in rendered
    for body in ({"sent_via": "manual"}, {"sent_to": "vendor@example.test"}):
        response = await client.post(f"/docs/{po}/send", headers=viewer, json=body)
        assert response.status_code == 403, response.text


@pytest.mark.parametrize("field", ["source_po_ref", "source_proforma_ref"])
def test_conversion_label_renders_reference_as_text(field):
    from ui.routes.documents import _doc_detail

    state = {"entity_id": "doc:converted", "doc_type": "bill", "status": "final", "ref_id": "BILL-1200"}
    plain = to_xml(_doc_detail(state, role="owner"))
    with_source = to_xml(_doc_detail({**state, field: "<Old & Co>"}, role="owner"))
    assert "Converted from" not in plain
    assert "Converted from &lt;Old &amp; Co&gt;" in with_source
    assert "Converted from <Old" not in with_source


@pytest.mark.parametrize("kind", ["bill", "consignment_in", "production_order"])
@pytest.mark.asyncio
async def test_internal_documents_refuse_email_and_manual_send(client, session, kind):
    from celerp.models.projections import Projection
    from celerp.services.auth import get_token_claims

    token = await register_admin(client)
    session.add(Projection(
        company_id=get_token_claims(token)["company_id"], entity_id="doc:internal", entity_type="doc",
        version=1, updated_at=datetime.now(timezone.utc),
        state={"doc_type": kind, "status": "draft", "line_items": [{"quantity": 1}]},
    ))
    await session.flush()
    for body in ({"sent_via": "manual"}, {"sent_to": "vendor@example.test"}):
        response = await client.post("/docs/doc:internal/send", headers={"Authorization": f"Bearer {token}"}, json=body)
        assert response.status_code == 409, response.text


@pytest.mark.parametrize(("status", "received", "role"), [
    ("sent", 2, "owner"), ("draft", 0, "owner"), ("received", 2, "owner"),
    ("returned", 2, "owner"), ("void", 0, "owner"), ("sent", 0, "viewer"),
])
def test_po_receive_hidden_when_no_line_or_permission_can_receive(status, received, role):
    from ui.routes.documents import _doc_detail

    html = to_xml(_doc_detail({
        "entity_id": "doc:PO-1000", "doc_type": "purchase_order", "status": status, "ref_id": "PO-1000",
        "line_items": [{"quantity": 2, "quantity_received": received, "name": "Cable", "line_id": "line-1"}],
    }, role=role))
    assert 'hx-post="/docs/doc:PO-1000/receive"' not in html


@pytest.mark.asyncio
async def test_po_status_cards_count_the_same_records_as_each_filter(client, session):
    from celerp.models.projections import Projection
    from celerp.services.auth import get_token_claims

    token = await register_admin(client)
    company = get_token_claims(token)["company_id"]
    auth = {"Authorization": f"Bearer {token}"}
    statuses = ("draft", "sent", "partially_received", "received", "partial_returned", "returned", "void")
    for i, status in enumerate(statuses):
        session.add(Projection(
            company_id=company, entity_id=f"doc:QA-{i}", entity_type="doc", version=1,
            updated_at=datetime.now(timezone.utc),
            state={"doc_type": "purchase_order", "status": status, "ref_id": f"PO-QA-{i}"},
        ))
    session.add(Projection(
        company_id=company, entity_id="doc:QA-bill", entity_type="doc", version=1,
        updated_at=datetime.now(timezone.utc),
        state={"doc_type": "bill", "status": "received", "ref_id": "BILL-QA", "source_po_ref": "PO-QA-bill"},
    ))
    await session.flush()

    for i, status in enumerate(statuses):
        params = {"doc_type": "purchase_order", "status": status}
        summary = await client.get("/docs/summary", params=params, headers=auth)
        assert summary.status_code == 200, summary.text
        assert summary.json()["cards"]["count_by_status"][status] == 1
        listing = await client.get("/docs", params=params, headers=auth)
        assert listing.status_code == 200, listing.text
        assert [row["id"] for row in listing.json()["items"]] == [f"doc:QA-{i}"]
