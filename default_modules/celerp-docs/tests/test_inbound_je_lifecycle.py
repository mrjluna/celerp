# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Inbound goods-recognition lifecycle: a bill recognises goods + AP exactly once (at finalize), and
receiving it only capitalises landed cost (Dr 1130-P / Cr clearing) - no double-posting. A purchase
order still recognises at receipt. COGS then relieves the same 1130-P the goods landed in."""
from __future__ import annotations

import uuid

import pytest

from stock_books import SAMPLE_STOCK_ENTRY


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@inlife.test"
    r = await client.post("/auth/register", json={"company_name": "InLife Co", "email": addr, "name": "A", "password": "validpass1"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _location(client, t) -> str:
    return (await client.post("/companies/me/locations", headers=_h(t), json={"name": "WH", "type": "warehouse"})).json()["id"]


async def _net(client, t) -> dict[str, float]:
    """Net debit-credit per account across all non-voided posted JEs (entries live on .created events),
    less the sample stock's opening entry."""
    led = [e for e in (await client.get("/ledger?entity_type=journal_entry", headers=_h(t))).json()["items"]
           if not e["entity_id"].startswith(SAMPLE_STOCK_ENTRY)]
    voided = {e["entity_id"] for e in led if str(e.get("event_type", "")).endswith(".voided")}
    by_acct: dict[str, float] = {}
    for e in led:
        if not str(e.get("event_type", "")).endswith(".created") or e["entity_id"] in voided:
            continue
        for x in (e.get("data") or {}).get("entries", []):
            by_acct[x["account"]] = round(by_acct.get(x["account"], 0.0)
                                          + float(x.get("debit", 0) or 0) - float(x.get("credit", 0) or 0), 2)
    return {k: v for k, v in by_acct.items() if abs(v) > 1e-9}


def _line(eid, sku, qty, price):
    return {"entity_id": eid, "sku": sku, "name": sku, "quantity": qty, "unit_price": price,
            "line_total": qty * price, "sell_by": "piece", "receive_as": "stock"}


@pytest.mark.asyncio
async def test_stock_bill_recognises_goods_and_ap_once(client):
    """Finalize + receive a plain stock bill: inventory and AP are recognised once, not doubled."""
    t = await _register(client)
    loc = await _location(client, t)
    goods = (await client.post("/items", headers=_h(t), json={"status": "available", "sku": "G1", "name": "G1", "quantity": 0, "sell_by": "piece"})).json()["id"]
    bill = (await client.post("/docs", headers=_h(t), json={"doc_type": "bill",
            "line_items": [_line(goods, "G1", 10, 10)], "total": 100})).json()["id"]
    assert (await client.post(f"/docs/{bill}/finalize", headers=_h(t))).status_code == 200
    assert (await client.post(f"/docs/{bill}/receive", headers=_h(t), json={"location_id": loc,
            "received_items": [{"item_id": goods, "sku": "G1", "name": "G1", "quantity_received": 10, "cost_price": 10, "receive_as": "stock"}]})).status_code == 200
    # Inventory 100 (once), AP 100 (once) - NOT 200/200.
    assert await _net(client, t) == {"1130-P": 100.0, "2110": -100.0}


@pytest.mark.asyncio
async def test_bill_with_freight_capitalises_landed_then_cogs_reconciles(client):
    """A bill with freight: finalize parks freight in clearing; receive draws it into 1130-P; selling
    relieves 1130-P at the full landed cost, netting inventory to zero."""
    t = await _register(client)
    loc = await _location(client, t)
    goods = (await client.post("/items", headers=_h(t), json={"status": "available", "sku": "G2", "name": "G2", "quantity": 0, "sell_by": "piece"})).json()["id"]
    frt = (await client.post("/items", headers=_h(t), json={"status": "available", "sku": "FR", "name": "FR", "quantity": 0, "sell_by": "piece", "inventory_type": "freight", "landed_cost_kind": "freight"})).json()["id"]
    bill = (await client.post("/docs", headers=_h(t), json={"doc_type": "bill", "line_items": [
            _line(goods, "G2", 10, 10), _line(frt, "FR", 1, 40)], "total": 140})).json()["id"]
    assert (await client.post(f"/docs/{bill}/finalize", headers=_h(t))).status_code == 200
    # After finalize: goods in 1130-P, freight parked in 1130-FRT clearing, AP = 140.
    assert await _net(client, t) == {"1130-P": 100.0, "1130-FRT": 40.0, "2110": -140.0}

    assert (await client.post(f"/docs/{bill}/receive", headers=_h(t), json={"location_id": loc,
            "received_items": [{"item_id": goods, "sku": "G2", "name": "G2", "quantity_received": 10, "cost_price": 10, "receive_as": "stock"}]})).status_code == 200
    # After receive: freight capitalised into 1130-P; clearing zeroed; AP unchanged. No double goods/AP.
    assert await _net(client, t) == {"1130-P": 140.0, "2110": -140.0}

    # The capitalisation JE carries a posting date (a dateless financial entry is never acceptable).
    led = (await client.get("/ledger?entity_type=journal_entry", headers=_h(t))).json()["items"]
    cap = next(e for e in led if "landed-cap" in (e.get("entity_id") or "")
               and str(e.get("event_type", "")).endswith(".created"))
    assert cap.get("ts"), "landed-cost capitalisation JE must have a posting date"

    # The received parcel carries the full landed cost.
    parcel = next(i for i in (await client.get("/items?q=G2", headers=_h(t))).json()["items"]
                  if float(i.get("quantity") or 0) == 10 and i["id"] != goods)
    assert parcel["cost_total"] == 140

    # Sell it: COGS relieves 1130-P for the full landed cost -> inventory nets to zero.
    inv = (await client.post("/docs", headers=_h(t), json={"doc_type": "invoice", "line_items": [
            {"entity_id": parcel["id"], "sku": "G2", "name": "G2", "quantity": 10, "unit_price": 30, "sell_by": "piece"}],
            "total": 300})).json()["id"]
    assert (await client.post(f"/docs/{inv}/finalize", headers=_h(t))).status_code == 200
    assert (await client.post(f"/docs/{inv}/fulfill-lines", headers=_h(t), json={"line_entity_ids": [parcel["id"]]})).status_code == 200
    net = await _net(client, t)
    assert net.get("1130-P", 0) == 0.0, net   # fully relieved
    assert net.get("5100") == 140.0           # COGS = full landed cost


@pytest.mark.asyncio
async def test_purchase_order_still_recognises_at_receipt(client):
    """A purchase order (not a bill) still recognises goods + AP at receipt."""
    t = await _register(client)
    loc = await _location(client, t)
    po = (await client.post("/docs", headers=_h(t), json={"doc_type": "purchase_order",
            "line_items": [{"sku": "PO1", "name": "PO1", "quantity": 5, "unit_price": 10, "line_total": 50, "sell_by": "piece"}],
            "total": 50})).json()["id"]
    assert (await client.post(f"/docs/{po}/receive", headers=_h(t), json={"location_id": loc,
            "received_items": [{"po_line_index": 0, "sku": "PO1", "name": "PO1", "quantity_received": 5}]})).status_code == 200
    assert await _net(client, t) == {"1130-P": 50.0, "2110": -50.0}


@pytest.mark.asyncio
async def test_po_receipt_retry_resend_void_and_bill_keep_one_stock_posting(client):
    token = await _register(client)
    auth = _h(token)
    loc = await _location(client, token)
    response = await client.post("/docs", headers=auth, json={"doc_type": "purchase_order", "line_items": [
        {"sku": "PO-QA", "name": "Cable", "quantity": 5, "unit_price": 10, "sell_by": "piece", "tax_rate": 0},
    ]})
    assert response.status_code in (200, 201), response.text
    url = f"/docs/{response.json()['id']}"
    send = {"sent_via": "manual", "idempotency_key": "qa-po-send"}
    a = await client.post(url + "/send", headers=auth, json=send)
    b = await client.post(url + "/send", headers=auth, json=send)
    assert a.status_code == b.status_code == 200
    assert a.json()["event_id"] == b.json()["event_id"]

    receipt = {"location_id": loc, "idempotency_key": "qa-po-receive", "received_items": [
        {"po_line_index": 0, "sku": "PO-QA", "name": "Cable", "quantity_received": 2},
    ]}
    a = await client.post(url + "/receive", headers=auth, json=receipt)
    b = await client.post(url + "/receive", headers=auth, json=receipt)
    assert a.status_code == b.status_code == 200, (a.text, b.text)
    order = (await client.get(url, headers=auth)).json()
    assert order["status"] == "partially_received"
    assert order["line_items"][0]["quantity_received"] == 2
    assert len(order["received_item_ids"]) == 1
    assert await _net(client, token) == {"1130-P": 20.0, "2110": -20.0}

    too_much = {**receipt, "idempotency_key": "qa-po-receive-too-much",
                "received_items": [{**receipt["received_items"][0], "quantity_received": 4}]}
    assert (await client.post(url + "/receive", headers=auth, json=too_much)).status_code == 422
    assert (await client.post(url + "/void", headers=auth, json={"reason": "QA"})).status_code == 409
    assert await _net(client, token) == {"1130-P": 20.0, "2110": -20.0}
    assert (await client.post(url + "/send", headers=auth, json={"sent_via": "manual"})).status_code == 200
    assert (await client.get(url, headers=auth)).json()["status"] == "partially_received"

    rest = {**receipt, "idempotency_key": "qa-po-receive-rest",
            "received_items": [{**receipt["received_items"][0], "quantity_received": 3}]}
    assert (await client.post(url + "/receive", headers=auth, json=rest)).status_code == 200
    assert await _net(client, token) == {"1130-P": 50.0, "2110": -50.0}
    finalized = await client.post(url + "/finalize", headers=auth)
    assert finalized.status_code == 200, finalized.text
    assert (await client.get(url, headers=auth)).json()["doc_type"] == "bill"
    assert await _net(client, token) == {"1130-P": 50.0, "2110": -50.0}
