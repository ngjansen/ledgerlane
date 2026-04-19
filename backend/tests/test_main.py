import io
import json
import time
from unittest.mock import MagicMock, patch

import pypdf
import pytest
from httpx import ASGITransport, AsyncClient

from main import app


# ── helpers ──────────────────────────────────────────────────────────────────

def make_pdf(pages: int = 1) -> bytes:
    """Create a minimal valid PDF with N blank pages."""
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)
    return buf.read()


MOCK_CLAUDE_RESPONSE = {
    "bank_name": "Chase Total Checking",
    "date_range": "Mar 1 – Mar 31, 2026",
    "transactions": [
        {
            "date": "03/01",
            "description": "Direct Deposit — Acme Corp",
            "raw_description": "ACH CREDIT #8842",
            "amount": 3241.50,
            "balance": 15724.77,
            "category": "Income",
        },
        {
            "date": "03/02",
            "description": "Whole Foods Market",
            "raw_description": "WHOLEFDS NYC",
            "amount": -142.88,
            "balance": 15581.89,
            "category": "Groceries",
        },
    ],
}


def mock_claude_client(response_text: str):
    """Return a mock anthropic.Anthropic client that returns response_text."""
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=response_text)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_message
    return mock_client


@pytest.fixture
def seeded_token(monkeypatch):
    """Seed a known token into the jobs dict and return the token."""
    import main
    token = "test-token-12345"
    main.jobs[token] = {
        "result": MOCK_CLAUDE_RESPONSE,
        "expires_at": time.time() + 3600,
    }
    yield token
    main.jobs.pop(token, None)


# ── tests: health ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ── tests: page validation ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_convert_rejects_no_file():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.post("/convert")
    assert r.status_code == 422  # FastAPI validation error


@pytest.mark.asyncio
async def test_convert_rejects_too_many_pages():
    pdf = make_pdf(pages=6)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.post(
            "/convert",
            files={"file": ("statement.pdf", pdf, "application/pdf")},
        )
    assert r.status_code == 400
    body = r.json()
    assert body["detail"]["error"] == "too_many_pages"
    assert body["detail"]["pages"] == 6
    assert body["detail"]["limit"] == 5


@pytest.mark.asyncio
async def test_convert_accepts_five_pages():
    """5 pages should pass the page check and proceed to Claude (mocked)."""
    pdf = make_pdf(pages=5)
    mock_client = mock_claude_client(json.dumps({
        "bank_name": "Test Bank", "date_range": "Jan 2026",
        "transactions": [{"date": "01/01", "description": "Test", "raw_description": "TEST",
                          "amount": 100.0, "balance": 100.0, "category": "Other"}]
    }))
    with patch("main.client", mock_client):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.post(
                "/convert",
                files={"file": ("statement.pdf", pdf, "application/pdf")},
            )
    assert r.status_code == 200


# ── tests: Claude extraction ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_convert_returns_transactions():
    pdf = make_pdf(pages=3)
    mock_client = mock_claude_client(json.dumps(MOCK_CLAUDE_RESPONSE))

    with patch("main.client", mock_client):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.post(
                "/convert",
                files={"file": ("statement.pdf", pdf, "application/pdf")},
            )

    assert r.status_code == 200
    body = r.json()
    assert "token" in body
    assert body["bank_name"] == "Chase Total Checking"
    assert body["page_count"] == 3
    assert len(body["transactions"]) == 2
    assert body["stats"]["total_in"] == 3241.50
    assert body["stats"]["total_out"] == -142.88


@pytest.mark.asyncio
async def test_convert_rejects_non_statement():
    pdf = make_pdf(pages=1)
    mock_client = mock_claude_client(json.dumps({"error": "not_a_bank_statement"}))

    with patch("main.client", mock_client):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            r = await ac.post(
                "/convert",
                files={"file": ("not_a_statement.pdf", pdf, "application/pdf")},
            )

    assert r.status_code == 422


# ── tests: downloads ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_download_csv(seeded_token):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get(f"/download/{seeded_token}/csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.strip().split("\n")
    assert lines[0].startswith("date,")  # header row
    assert len(lines) == 3  # header + 2 transactions


@pytest.mark.asyncio
async def test_download_xlsx(seeded_token):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get(f"/download/{seeded_token}/xlsx")
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    assert r.content[:2] == b"PK"


@pytest.mark.asyncio
async def test_download_json(seeded_token):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get(f"/download/{seeded_token}/json")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert data[0]["description"] == "Direct Deposit — Acme Corp"


@pytest.mark.asyncio
async def test_download_qbo(seeded_token):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get(f"/download/{seeded_token}/qbo")
    assert r.status_code == 200
    assert b"OFXHEADER" in r.content
    assert b"STMTTRN" in r.content


@pytest.mark.asyncio
async def test_download_ofx(seeded_token):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get(f"/download/{seeded_token}/ofx")
    assert r.status_code == 200
    assert b"<?xml" in r.content
    assert b"OFX" in r.content


@pytest.mark.asyncio
async def test_download_expired_token():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/download/nonexistent-token/csv")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_download_invalid_format(seeded_token):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get(f"/download/{seeded_token}/pdf")
    assert r.status_code == 400


# ── tests: error handling ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_convert_rejects_non_pdf():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.post(
            "/convert",
            files={"file": ("not_a_pdf.pdf", b"this is not a pdf", "application/pdf")},
        )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_pdf"
