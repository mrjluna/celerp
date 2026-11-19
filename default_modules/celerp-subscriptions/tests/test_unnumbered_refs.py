# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""An agreement or document with no number is named for what it is ("Subscription template (Draft)"),
never by its internal id, wherever the subscriptions module words a reference."""
from __future__ import annotations

import re
import uuid

import pytest
from celerp.models.projections import Projection
from celerp_subscriptions import billing
from agreement_kit import agreement, company_id, freeze_day, register, schedule, summary, tick
from ui.i18n import t

pytestmark = pytest.mark.asyncio

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-", re.I)


async def _drop_number(session, eid: str) -> None:
    from sqlalchemy import select

    for row in (await session.execute(select(Projection).where(Projection.entity_id == eid))).scalars():
        row.state = {k: v for k, v in row.state.items() if k not in ("ref_id", "doc_number")}
    await session.commit()


async def test_a_failed_generation_of_an_unnumbered_agreement_names_it_readably(client, session, monkeypatch):
    from celerp_docs import routes as docs_routes

    freeze_day(monkeypatch, "2026-07-01")
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await agreement(client, hd, start="2026-07-01")
    assert (await schedule(client, hd, eid, generation_mode="auto_invoice", catchup_periods=0)).status_code == 200
    await _drop_number(session, eid)

    async def broken(*args, **kwargs):
        raise ValueError("synthetic finalize failure")

    monkeypatch.setattr(docs_routes, "finalize_document", broken)
    await tick(session, cid, "2026-07-01")
    rows = [r for r in (await summary(client, hd))["rows"] if r["kind"] == "generation_failed"]
    assert len(rows) == 1
    text = rows[0]["message"] + " " + str(rows[0]["ref_id"])
    assert eid not in text and "doc:" not in text and not UUID.search(text), text
    assert f"({t('status.draft', 'en')})" in rows[0]["ref_id"] or f"({t('documents.unnumbered', 'en')})" in rows[0]["ref_id"]


async def test_a_document_reference_without_a_number_is_readable(client, session):
    hd = await register(client)
    cid = await company_id(client, hd)
    eid = await agreement(client, hd, start="2026-07-01", activate=False)
    await _drop_number(session, eid)
    label = await billing.doc_ref(session, uuid.UUID(cid), eid)
    assert eid not in label and "doc:" not in label
    assert label.endswith(f"({t('status.draft', 'en')})")
