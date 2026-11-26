"""A document with no number is named by its type and state, never by its internal id."""
from __future__ import annotations

import re

import pytest
from fasthtml.common import to_xml

from ui.i18n import doc_ref_label, t
from ui.routes.contacts import _documents_tab

ID = "doc:4f6c9d2e-1b7a-4c3e-9a55-0123456789ab"


def _visible(html: str) -> str:
    return re.sub(r"<[^>]*>", " ", html)


def test_a_numbered_document_keeps_its_number():
    assert doc_ref_label({"ref_id": "INV-1"}) == "INV-1"
    assert doc_ref_label({"doc_number": "INV-2", "ref_id": "INV-1"}) == "INV-2"


def test_an_unnumbered_draft_reads_as_its_type_and_draft():
    assert doc_ref_label({"doc_type": "invoice", "status": "draft"}) == f"{t('enum.doc_type.invoice')} ({t('status.draft')})"


def test_an_unnumbered_issued_document_reads_as_unnumbered():
    assert doc_ref_label({"doc_type": "bill", "status": "final"}) == f"{t('enum.doc_type.bill')} ({t('documents.unnumbered')})"


def test_nothing_known_still_reads_as_a_document():
    assert doc_ref_label(None) == f"{t('contacts.doc_ref_fallback')} ({t('status.draft')})"


def test_the_contact_documents_tab_links_an_unnumbered_draft_by_a_readable_label():
    html = to_xml(_documents_tab([{"entity_id": ID, "doc_type": "invoice", "status": "draft", "total": 5, "currency": "USD"}]))
    link = re.search(r'<a [^>]*href="([^"]*)"[^>]*>(.*?)</a>', html)
    assert link.group(1) == f"/docs/{ID}"
    assert link.group(2) == f"{t('enum.doc_type.invoice')} ({t('status.draft')})"
    assert "4f6c9d2e" not in _visible(html) and "doc:" not in _visible(html)


def test_the_contact_documents_tab_still_shows_a_number():
    html = to_xml(_documents_tab([{"entity_id": ID, "doc_type": "invoice", "status": "final", "ref_id": "INV-9"}]))
    assert ">INV-9</a>" in html
