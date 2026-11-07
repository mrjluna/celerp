"""Uploads through the shared document attachment controls."""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser
PDF = b'%PDF-1.0\n1 0 obj<</Type/Catalog>>endobj\n'

@pytest.mark.parametrize('resource,kind', [('docs', 'invoice'), ('lists', 'quotation')])
@pytest.mark.parametrize('posted', [False, True])
@pytest.mark.parametrize('filename', ['invoice.pdf', 'invoice.PDF', 'invoice copy.pdf'])
def test_pdf_upload_reaches_document_attachment_list(page, ui_server, api, posted, filename, resource, kind):
    response = api.post('/'+resource, json={('doc_type' if resource == 'docs' else 'list_type'): kind, 'line_items': [{'name': 'Service', 'quantity': 1, 'unit_price': 10}]})
    assert response.status_code == 200, response.text
    doc_id = response.json()['id']
    if posted:
        response = api.post(f'/{resource}/{doc_id}/finalize')
        assert response.status_code == 200, response.text
    page.goto(f'{ui_server}/{resource}/{doc_id}')
    section = page.locator('.doc-internal').filter(has=page.locator('[id^="files-section-"]'))
    section.locator('summary').click()
    page.locator('[id^="file-drop-input-"]').set_input_files({'name': filename, 'mimeType': 'application/pdf', 'buffer': PDF})
    expect(page.locator('a.file-link', has_text=filename)).to_be_visible()
    expect(page.locator('.files-notice')).to_be_empty()
    files = api.get(f'/{resource}/{doc_id}').json()['files']
    assert files[-1]['filename'] == filename
    assert api.get(f'/{resource}/{doc_id}/files/{files[-1]["id"]}').content == PDF
