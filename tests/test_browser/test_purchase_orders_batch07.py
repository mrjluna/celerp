# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Purchase order and AI upload regressions through the live UI."""
import uuid

import pytest
from playwright.sync_api import expect

from .conftest import answer_confirms
from .test_batch05_uploads import PDF
from .test_receive_and_return_lines import _select_all_and_act

pytestmark = pytest.mark.browser


def _create(api, kind="purchase_order"):
    tag = uuid.uuid4().hex[:8]
    response = api.post("/docs", json={"doc_type": kind, "line_items": [
        {"sku": f"{tag}-{i}", "name": f"Goods {i}", "quantity": quantity,
         "unit": "piece", "unit_price": 5, "tax_rate": 0}
        for i, quantity in enumerate((3, 2))
    ]})
    assert response.status_code in {200, 201}, response.text
    return response.json()["id"]


@pytest.mark.parametrize("width", [1280, 390])
def test_po_mark_sent_and_receive(page, ui_server, api, width):
    order = _create(api)
    page.set_viewport_size({"width": width, "height": 900})
    answer_confirms(page)
    url = f"{ui_server}/docs/{order}"
    page.goto(url)
    page.get_by_role("button", name="Mark as Sent", exact=True).click()
    expect(page.locator(".badge").get_by_text("Sent", exact=True)).to_be_visible()
    _select_all_and_act(page, "li-fulfill", "li-bulk-fulfill-btn")
    form = page.locator("#li-bulk-fulfill-btn")
    fields = form.locator("input.li-qty-input")
    assert fields.nth(0).input_value() == "3"
    expect(form).to_contain_text("Received: 0")
    fields.nth(0).fill("4")
    assert fields.nth(0).evaluate("e => e.validity.rangeOverflow")
    form.evaluate("""form => {
        form.dataset.submitAttempts = "0";
        form.addEventListener("submit", () => form.dataset.submitAttempts++, {capture: true});
    }""")
    fields.nth(0).press("Enter")
    assert form.get_attribute("data-submit-attempts") == "0"
    assert api.get(f"/docs/{order}").json()["status"] == "sent"
    fields.nth(0).fill("1")
    form.get_by_role("button", name="Receive Goods").click()
    expect(page.locator(".badge").get_by_text("Partially Received", exact=True)).to_be_visible()
    _select_all_and_act(page, "li-fulfill", "li-bulk-fulfill-btn")
    assert fields.first.input_value() == "2"
    form.get_by_role("button", name="Receive Goods").click()
    expect(page.locator(".badge").get_by_text("Received", exact=True)).to_be_visible()
    expect(page.locator("#li-bulk-select option[value=li-fulfill]")).to_have_count(0)
    if width == 1280:
        old = api.get(f"/docs/{order}").json()["ref_id"]
        page.get_by_role("button", name="Convert to Bill", exact=True).click()
        expect(page.get_by_text(f"Converted from {old}", exact=True)).to_be_visible()


def test_ai_upload_feedback_is_visible_at_the_drop_zone(page, ui_server, api):
    bill = _create(api, "bill")
    page.goto(f"{ui_server}/docs/{bill}")
    zone = page.locator(".ai-dropzone")
    expect(zone).to_be_visible()
    expect(zone).to_have_attribute("title", "Attach a receipt or invoice")
    for name, mime, content, message in [
        ("empty.pdf", "application/pdf", "", "File is empty"),
        ("unsupported.exe", "application/x-msdownload", "unsupported", "can't be attached"),
        ("failed.pdf", "application/pdf", PDF.decode(), "Storage unavailable"),
        ("receipt.pdf", "application/pdf", PDF.decode(), "File uploaded"),
    ]:
        if name == "failed.pdf":
            page.route(f"**/docs/{bill}/files", lambda route: route.fulfill(
                status=503, content_type="text/plain", body="Storage unavailable",
            ), times=1)
        zone.evaluate("""(node, [name, mime, content]) => {
            const transfer = new DataTransfer();
            transfer.items.add(new File([content], name, {type: mime}));
            node.dispatchEvent(new DragEvent('drop', {dataTransfer: transfer, bubbles: true, cancelable: true}));
        }""", [name, mime, content])
        expect(zone.get_by_role("status")).to_contain_text(message, ignore_case=True)
        expect(zone.locator(".file-drop-text")).to_have_text("Drop a receipt or invoice here to attach it")
    expect(zone.locator("input[capture=environment]")).to_have_attribute("accept", "image/*")
    expect(page.get_by_text("receipt.pdf", exact=True).first).to_be_attached()
    assert [file["filename"] for file in api.get(f"/docs/{bill}").json()["files"]] == ["receipt.pdf"]


def test_po_status_filter_and_conversion(page, ui_server, api):
    order = _create(api)
    old = api.get(f"/docs/{order}").json()["ref_id"]
    listing = f"{ui_server}/docs?type=purchase_order&preset=all&q={old}"
    page.goto(listing)
    select = page.locator("#doc-filters select[name=status]")
    expect(select.locator("option")).to_have_text([
        "All", "Draft", "Sent", "Partially Received", "Received", "Partially Returned", "Returned", "Void",
    ])
    draft = page.locator(".status-card", has_text="Draft")
    expect(draft.locator(".status-card-count")).to_have_text("1")
    select.select_option("draft")
    expect(page.get_by_role("link", name=old, exact=True)).to_be_visible()
    assert api.post(f"/docs/{order}/send", json={"sent_via": "manual"}).status_code == 200
    page.goto(listing)
    expect(page.locator(".status-card", has_text="Sent").locator(".status-card-count")).to_have_text("1")
    converted = api.post(f"/docs/{order}/finalize")
    assert converted.status_code == 200, converted.text
    page.reload()
    expect(page.locator(".status-card", has_text="Sent").locator(".status-card-count")).to_have_text("0")
    page.goto(f"{ui_server}/docs/{order}")
    expect(page.get_by_text(f"Converted from {old}", exact=True)).to_be_visible()
    page.goto(f"{ui_server}/docs?type=bill&preset=all&q={old}")
    current = api.get(f"/docs/{order}").json()["ref_id"]
    expect(page.get_by_role("link", name=current, exact=True)).to_be_visible()
    search = page.locator(".global-search-input")
    search.fill(old)
    search.press("End")
    expect(page.locator("#global-search-results")).to_contain_text(current)
    assert api.post(f"/docs/{order}/revert-to-draft", json={}).status_code == 200
    page.goto(f"{ui_server}/docs/{order}")
    expect(page.get_by_text(f"Converted from {old}", exact=True)).to_have_count(0)
