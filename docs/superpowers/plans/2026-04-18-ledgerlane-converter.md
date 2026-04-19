# LedgerLane Converter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a real bank statement converter — PDF upload → Claude Haiku extracts transactions → downloadable CSV/Excel/QBO/OFX/JSON.

**Architecture:** FastAPI backend (Python) handles PDF page counting, Claude Haiku 4.5 extraction via native PDF document blocks, in-memory job storage with 1hr TTL, and file generation. The existing `index.html` frontend is wired to POST to the real API and render actual transactions.

**Tech Stack:** FastAPI, anthropic SDK, pypdf, openpyxl, pytest + httpx (tests); vanilla JS (frontend)

---

## Task 1: Backend scaffold

**Files:**
- Create: `ledgerlane/backend/requirements.txt`
- Create: `ledgerlane/backend/Procfile`
- Create: `ledgerlane/backend/railway.toml`
- Create: `ledgerlane/backend/main.py` (skeleton)
- Create: `ledgerlane/backend/tests/__init__.py`
- Create: `ledgerlane/backend/tests/test_main.py` (skeleton)

- [ ] **Step 1: Create requirements.txt**

```
fastapi>=0.111.0
uvicorn[standard]>=0.29.0
python-multipart>=0.0.9
anthropic>=0.25.0
pypdf>=4.2.0
openpyxl>=3.1.2
httpx>=0.27.0
pytest>=8.2.0
pytest-asyncio>=0.23.0
```

- [ ] **Step 2: Create Procfile**

```
web: uvicorn main:app --host 0.0.0.0 --port $PORT
```

- [ ] **Step 3: Create railway.toml**

```toml
[build]
builder = "nixpacks"

[deploy]
startCommand = "uvicorn main:app --host 0.0.0.0 --port $PORT"
healthcheckPath = "/health"
healthcheckTimeout = 10
```

- [ ] **Step 4: Create main.py skeleton**

```python
import asyncio
import base64
import csv
import io
import json
import time
import uuid
from datetime import date

import anthropic
import openpyxl
import pypdf
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

app = FastAPI(title="LedgerLane Converter API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = anthropic.Anthropic()

# In-memory job store: {token: {"result": dict, "expires_at": float}}
jobs: dict[str, dict] = {}

FREE_PAGE_LIMIT = 5

SYSTEM_PROMPT = """You are a financial data extraction engine. Extract every transaction from the bank statement provided.

Return ONLY a JSON object with this exact structure — no markdown, no explanation, no code fences:
{
  "bank_name": "string",
  "date_range": "string",
  "transactions": [
    {
      "date": "MM/DD",
      "description": "string (clean merchant name)",
      "raw_description": "string (original text from statement)",
      "amount": number,
      "balance": number or null,
      "category": "string"
    }
  ]
}

Rules:
- Include ALL rows including opening/closing balance rows
- amount is signed: credits are positive, debits are negative
- balance is the running balance after the transaction, null if not shown
- date format is MM/DD (no year needed)
- category must be one of: Income, Groceries, Dining, Transport, Housing, Utilities, Shopping, Software, Transfer, Cash, Refund, Interest, Other
- If the document is not a bank statement, return {"error": "not_a_bank_statement"}"""


@app.get("/health")
def health():
    return {"status": "ok"}


@app.on_event("startup")
async def startup():
    asyncio.create_task(_cleanup_jobs())


async def _cleanup_jobs():
    while True:
        await asyncio.sleep(300)
        now = time.time()
        expired = [k for k, v in jobs.items() if v["expires_at"] < now]
        for k in expired:
            del jobs[k]
```

- [ ] **Step 5: Create tests/__init__.py (empty)**

```python
```

- [ ] **Step 6: Write health endpoint test**

```python
# ledgerlane/backend/tests/test_main.py
import io
import json
from unittest.mock import MagicMock, patch

import pypdf
import pytest
from httpx import ASGITransport, AsyncClient

from main import app


def make_pdf(pages: int = 1) -> bytes:
    """Create a minimal valid PDF with N blank pages."""
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)
    return buf.read()


@pytest.mark.asyncio
async def test_health():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
```

- [ ] **Step 7: Install dependencies and run health test**

```bash
cd /Users/jansenng/Desktop/website-builder/ledgerlane/backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest tests/test_main.py::test_health -v
```

Expected: `PASSED`

- [ ] **Step 8: Commit**

```bash
cd /Users/jansenng/Desktop/website-builder/ledgerlane
git init  # if not already a git repo
git add backend/
git commit -m "feat: backend scaffold with health endpoint"
```

---

## Task 2: PDF page validation

**Files:**
- Modify: `ledgerlane/backend/main.py` — add `/convert` endpoint (page check only, no Claude yet)
- Modify: `ledgerlane/backend/tests/test_main.py` — add page validation tests

- [ ] **Step 1: Write failing tests for page validation**

Add to `tests/test_main.py`:

```python
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
    """5 pages should pass the page check (Claude call will be mocked later)."""
    pdf = make_pdf(pages=5)
    # We expect this to fail past the page check — 500 is fine here
    # because Claude isn't mocked yet. We just want no 400.
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.post(
            "/convert",
            files={"file": ("statement.pdf", pdf, "application/pdf")},
        )
    assert r.status_code != 400  # page check passed
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_main.py::test_convert_rejects_too_many_pages -v
```

Expected: `FAILED` — `/convert` route not defined yet

- [ ] **Step 3: Add /convert endpoint with page check to main.py**

Add after the `_cleanup_jobs` function:

```python
@app.post("/convert")
async def convert(file: UploadFile = File(...)):
    content = await file.read()

    # Count pages — fast, no Claude call
    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
        page_count = len(reader.pages)
    except Exception:
        raise HTTPException(
            status_code=400, detail={"error": "invalid_pdf"}
        )

    if page_count > FREE_PAGE_LIMIT:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "too_many_pages",
                "pages": page_count,
                "limit": FREE_PAGE_LIMIT,
            },
        )

    # Claude call will be added in Task 3
    raise HTTPException(status_code=501, detail={"error": "not_implemented"})
```

- [ ] **Step 4: Run page validation tests**

```bash
pytest tests/test_main.py -v
```

Expected: `test_health PASSED`, `test_convert_rejects_no_file PASSED`, `test_convert_rejects_too_many_pages PASSED`, `test_convert_accepts_five_pages PASSED`

- [ ] **Step 5: Commit**

```bash
git add backend/main.py backend/tests/test_main.py
git commit -m "feat: PDF page validation — reject >5 pages with 400"
```

---

## Task 3: Claude extraction

**Files:**
- Modify: `ledgerlane/backend/main.py` — replace 501 stub with real Claude call + job storage
- Modify: `ledgerlane/backend/tests/test_main.py` — add extraction tests with mocked Claude

- [ ] **Step 1: Write failing test for successful extraction**

Add to `tests/test_main.py`:

```python
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
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_main.py::test_convert_returns_transactions -v
```

Expected: `FAILED` — returns 501 not implemented

- [ ] **Step 3: Replace stub with real Claude call in main.py**

Replace the entire `/convert` endpoint with:

```python
@app.post("/convert")
async def convert(file: UploadFile = File(...)):
    content = await file.read()

    # Count pages — fast, no Claude call
    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
        page_count = len(reader.pages)
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "invalid_pdf"})

    if page_count > FREE_PAGE_LIMIT:
        raise HTTPException(
            status_code=400,
            detail={"error": "too_many_pages", "pages": page_count, "limit": FREE_PAGE_LIMIT},
        )

    # Send to Claude Haiku with native PDF support
    pdf_b64 = base64.standard_b64encode(content).decode("utf-8")

    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=8192,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": "Extract all transactions from this bank statement.",
                        },
                    ],
                }
            ],
        )
    except anthropic.APIError as e:
        raise HTTPException(status_code=502, detail={"error": "claude_unavailable", "message": str(e)})

    # Parse Claude's JSON response
    raw = response.content[0].text.strip()
    # Strip markdown code fences if Claude wraps the JSON
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail={"error": "invalid_claude_response"})

    if "error" in data:
        raise HTTPException(status_code=422, detail=data)

    transactions = data.get("transactions", [])

    # Compute stats
    total_in = round(sum(t["amount"] for t in transactions if t.get("amount", 0) > 0), 2)
    total_out = round(sum(t["amount"] for t in transactions if t.get("amount", 0) < 0), 2)

    # Store result
    token = str(uuid.uuid4())
    jobs[token] = {
        "result": data,
        "expires_at": time.time() + 3600,
    }

    return {
        "token": token,
        "page_count": page_count,
        "bank_name": data.get("bank_name", "Unknown Bank"),
        "date_range": data.get("date_range", ""),
        "transactions": transactions,
        "stats": {"total_in": total_in, "total_out": total_out, "flagged": 0},
    }
```

- [ ] **Step 4: Run all tests**

```bash
pytest tests/test_main.py -v
```

Expected: all `PASSED`

- [ ] **Step 5: Commit**

```bash
git add backend/main.py backend/tests/test_main.py
git commit -m "feat: Claude Haiku extraction with native PDF document block and prompt caching"
```

---

## Task 4: Download endpoint

**Files:**
- Modify: `ledgerlane/backend/main.py` — add `/download/{token}/{format}` + helper functions
- Modify: `ledgerlane/backend/tests/test_main.py` — add download tests

- [ ] **Step 1: Write failing download tests**

Add to `tests/test_main.py`:

```python
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


import time  # add to imports at top of test file


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
    # Valid xlsx starts with PK (ZIP magic bytes)
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
```

- [ ] **Step 2: Run to confirm failures**

```bash
pytest tests/test_main.py::test_download_csv -v
```

Expected: `FAILED` — route not defined

- [ ] **Step 3: Add helper functions and download endpoint to main.py**

Add after the `/convert` endpoint:

```python
def _to_ofx_date(date_str: str) -> str:
    """Convert MM/DD to YYYYMMDD, assuming current year."""
    year = date.today().year
    try:
        month, day = date_str.split("/")
        return f"{year}{month.zfill(2)}{day.zfill(2)}"
    except (ValueError, AttributeError):
        return f"{year}0101"


def _generate_csv(transactions: list[dict]) -> bytes:
    output = io.StringIO()
    fields = ["date", "description", "raw_description", "amount", "balance", "category"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(transactions)
    return output.getvalue().encode("utf-8")


def _generate_xlsx(transactions: list[dict], bank_name: str) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Transactions"
    headers = ["Date", "Description", "Raw Description", "Amount", "Balance", "Category"]
    ws.append(headers)
    # Bold header
    from openpyxl.styles import Font
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for t in transactions:
        ws.append([
            t.get("date", ""),
            t.get("description", ""),
            t.get("raw_description", ""),
            t.get("amount"),
            t.get("balance"),
            t.get("category", ""),
        ])
    # Auto-width (approximate)
    for col in ws.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=0)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 40)
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output.read()


def _generate_qbo(transactions: list[dict], bank_name: str) -> bytes:
    rows = []
    for i, t in enumerate(transactions):
        ttype = "CREDIT" if (t.get("amount") or 0) > 0 else "DEBIT"
        rows.append(
            f"<STMTTRN>"
            f"<TRNTYPE>{ttype}"
            f"<DTPOSTED>{_to_ofx_date(t.get('date', '01/01'))}"
            f"<TRNAMT>{t.get('amount', 0):.2f}"
            f"<FITID>{i + 1}"
            f"<NAME>{t.get('description', '')[:32]}"
            f"<MEMO>{t.get('raw_description', '')[:255]}"
            f"</STMTTRN>"
        )
    now = date.today().strftime("%Y%m%d%H%M%S")
    body = (
        f"OFXHEADER:100\r\n"
        f"DATA:OFXSGML\r\n"
        f"VERSION:102\r\n"
        f"SECURITY:NONE\r\n"
        f"ENCODING:USASCII\r\n"
        f"CHARSET:1252\r\n"
        f"COMPRESSION:NONE\r\n"
        f"OLDFILEUID:NONE\r\n"
        f"NEWFILEUID:NONE\r\n"
        f"\r\n"
        f"<OFX>"
        f"<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>"
        f"<DTSERVER>{now}<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>"
        f"<BANKMSGSRSV1><STMTTRNRS><TRNUID>1001"
        f"<STATUS><CODE>0<SEVERITY>INFO</STATUS>"
        f"<STMTRS><CURDEF>USD"
        f"<BANKACCTFROM><BANKID>000000000<ACCTID>XXXXXXXXX<ACCTTYPE>CHECKING</BANKACCTFROM>"
        f"<BANKTRANLIST>{''.join(rows)}</BANKTRANLIST>"
        f"</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
    )
    return body.encode("ascii", errors="replace")


def _generate_ofx(transactions: list[dict], bank_name: str) -> bytes:
    rows = []
    for i, t in enumerate(transactions):
        ttype = "CREDIT" if (t.get("amount") or 0) > 0 else "DEBIT"
        rows.append(
            f"    <STMTTRN>\n"
            f"      <TRNTYPE>{ttype}</TRNTYPE>\n"
            f"      <DTPOSTED>{_to_ofx_date(t.get('date', '01/01'))}</DTPOSTED>\n"
            f"      <TRNAMT>{t.get('amount', 0):.2f}</TRNAMT>\n"
            f"      <FITID>{i + 1}</FITID>\n"
            f"      <NAME>{t.get('description', '')[:32]}</NAME>\n"
            f"      <MEMO>{t.get('raw_description', '')[:255]}</MEMO>\n"
            f"    </STMTTRN>\n"
        )
    now = date.today().strftime("%Y%m%d%H%M%S")
    body = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>\n'
        f"<OFX>\n"
        f"  <SIGNONMSGSRSV1>\n"
        f"    <SONRS>\n"
        f"      <STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>\n"
        f"      <DTSERVER>{now}</DTSERVER>\n"
        f"      <LANGUAGE>ENG</LANGUAGE>\n"
        f"    </SONRS>\n"
        f"  </SIGNONMSGSRSV1>\n"
        f"  <BANKMSGSRSV1>\n"
        f"    <STMTTRNRS>\n"
        f"      <TRNUID>1001</TRNUID>\n"
        f"      <STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>\n"
        f"      <STMTRS>\n"
        f"        <CURDEF>USD</CURDEF>\n"
        f"        <BANKACCTFROM>\n"
        f"          <BANKID>000000000</BANKID>\n"
        f"          <ACCTID>XXXXXXXXX</ACCTID>\n"
        f"          <ACCTTYPE>CHECKING</ACCTTYPE>\n"
        f"        </BANKACCTFROM>\n"
        f"        <BANKTRANLIST>\n"
        f"{''.join(rows)}"
        f"        </BANKTRANLIST>\n"
        f"      </STMTRS>\n"
        f"    </STMTTRNRS>\n"
        f"  </BANKMSGSRSV1>\n"
        f"</OFX>\n"
    )
    return body.encode("utf-8")


@app.get("/download/{token}/{fmt}")
def download(token: str, fmt: str):
    job = jobs.get(token)
    if job is None or job["expires_at"] < time.time():
        raise HTTPException(status_code=404, detail={"error": "token_not_found"})

    result = job["result"]
    transactions = result.get("transactions", [])
    bank_name = result.get("bank_name", "statement")
    slug = bank_name.lower().replace(" ", "-")[:30]

    if fmt == "csv":
        return StreamingResponse(
            io.BytesIO(_generate_csv(transactions)),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="ledgerlane-{slug}.csv"'},
        )
    elif fmt == "xlsx":
        return StreamingResponse(
            io.BytesIO(_generate_xlsx(transactions, bank_name)),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="ledgerlane-{slug}.xlsx"'},
        )
    elif fmt == "json":
        return StreamingResponse(
            io.BytesIO(json.dumps(transactions, indent=2).encode("utf-8")),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="ledgerlane-{slug}.json"'},
        )
    elif fmt == "qbo":
        return StreamingResponse(
            io.BytesIO(_generate_qbo(transactions, bank_name)),
            media_type="application/x-ofx",
            headers={"Content-Disposition": f'attachment; filename="ledgerlane-{slug}.qbo"'},
        )
    elif fmt == "ofx":
        return StreamingResponse(
            io.BytesIO(_generate_ofx(transactions, bank_name)),
            media_type="application/x-ofx",
            headers={"Content-Disposition": f'attachment; filename="ledgerlane-{slug}.ofx"'},
        )
    else:
        raise HTTPException(status_code=400, detail={"error": "invalid_format"})
```

- [ ] **Step 4: Add `import time` to test file if not present, then run all tests**

```bash
pytest tests/test_main.py -v
```

Expected: all tests `PASSED`

- [ ] **Step 5: Commit**

```bash
git add backend/main.py backend/tests/test_main.py
git commit -m "feat: download endpoint — CSV, Excel, QBO, OFX, JSON"
```

---

## Task 5: Wire frontend upload

**Files:**
- Modify: `ledgerlane/index.html` — replace fake `startDemo()` with real file upload

- [ ] **Step 1: Add API_BASE constant and hidden file input to index.html**

In `index.html`, find the opening `<script>` tag (around line 1058) and add at the very top of the script block:

```javascript
/* ======================== API config ======================== */
const API_BASE = 'http://localhost:8000';
```

In the HTML, find the `.drop` div (around line 704) and add a hidden file input directly after the opening `<div class="app-surface">` tag (around line 701):

```html
<input type="file" id="file-input" accept=".pdf,application/pdf" style="display:none" />
```

- [ ] **Step 2: Replace startDemo() with real upload logic**

Find and replace the entire `startDemo()` function (around line 1287) with:

```javascript
/* ======================== Upload state ======================== */
let currentToken = null;

function startUpload(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.pdf') && file.type !== 'application/pdf') {
    showUploadError('Please upload a PDF file.');
    return;
  }
  _runUpload(file);
}

async function _runUpload(file) {
  showStage('detect');
  // Animate bars while waiting — fire them on a loose schedule
  const barTimings = [
    { id: 'bar-1', delay: 0,    label: 'detect-s1', text: 'Reading pages…' },
    { id: 'bar-2', delay: 2500, label: 'detect-s2', text: 'Matching bank format…' },
    { id: 'bar-3', delay: 5500, label: 'detect-s3', text: 'Extracting transactions…' },
  ];
  barTimings.forEach(({ id, delay, label, text }) => {
    setTimeout(() => {
      document.getElementById(label).textContent = text;
      runBar(id, 3000, () => {});
    }, delay);
  });

  const formData = new FormData();
  formData.append('file', file);

  let data;
  try {
    const res = await fetch(`${API_BASE}/convert`, { method: 'POST', body: formData });
    if (!res.ok) {
      const err = await res.json();
      handleConvertError(err.detail || err);
      return;
    }
    data = await res.json();
  } catch (e) {
    showUploadError('Network error — please check your connection and try again.');
    return;
  }

  currentToken = data.token;

  // Update stage-2 labels with real data
  document.getElementById('detect-s1').textContent = `${data.page_count} page${data.page_count !== 1 ? 's' : ''} read.`;
  document.getElementById('detect-s2').textContent = `Matched: ${data.bank_name}.`;
  document.getElementById('detect-s3').textContent = `${data.transactions.length} transactions extracted.`;

  setTimeout(() => renderResults(data), 400);
}

function handleConvertError(detail) {
  showStage('upload');
  if (detail && detail.error === 'too_many_pages') {
    showUploadError(
      `This statement is ${detail.pages} pages. Free conversions are limited to ${detail.limit} pages. ` +
      `<a href="#" style="color:var(--accent-600);font-weight:500;">Create a free account →</a> to convert longer statements.`
    );
  } else if (detail && detail.error === 'not_a_bank_statement') {
    showUploadError('This doesn\'t look like a bank statement. Please upload a PDF bank statement.');
  } else {
    showUploadError('Something went wrong — please try again.');
  }
}

function showUploadError(msg) {
  let banner = document.getElementById('upload-error');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'upload-error';
    banner.style.cssText = 'margin-top:12px;padding:12px 14px;background:color-mix(in oklab,var(--bad-500) 10%,var(--card));border:1px solid color-mix(in oklab,var(--bad-500) 30%,var(--line));border-radius:var(--radius-md);color:var(--bad-500);font-size:13.5px;line-height:1.5;';
    document.getElementById('stage-upload').appendChild(banner);
  }
  banner.innerHTML = msg;
  banner.style.display = 'block';
}

function clearUploadError() {
  const banner = document.getElementById('upload-error');
  if (banner) banner.style.display = 'none';
}
```

- [ ] **Step 3: Wire the drop zone and file input**

Find and replace the existing drop zone drag/click handlers (around line 1311):

```javascript
/* Drop zone interaction */
const drop = document.getElementById('drop');
const fileInput = document.getElementById('file-input');

// Click to browse
drop.addEventListener('click', () => { clearUploadError(); fileInput.click(); });
fileInput.addEventListener('change', () => {
  if (fileInput.files[0]) startUpload(fileInput.files[0]);
  fileInput.value = '';  // reset so same file can be re-selected
});

// Drag and drop
['dragenter', 'dragover'].forEach(e =>
  drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add('over'); })
);
['dragleave', 'drop'].forEach(e =>
  drop.addEventListener(e, (ev) => {
    ev.preventDefault();
    drop.classList.remove('over');
    if (e === 'drop') {
      clearUploadError();
      startUpload(ev.dataTransfer.files[0]);
    }
  })
);
```

- [ ] **Step 4: Update resetDemo() to clear state**

Find `resetDemo()` and replace with:

```javascript
function resetDemo() {
  currentToken = null;
  clearUploadError();
  showStage('upload');
}
```

- [ ] **Step 5: Start both servers and manually test upload flow**

```bash
# Terminal 1 — backend
cd /Users/jansenng/Desktop/website-builder/ledgerlane/backend
source .venv/bin/activate
uvicorn main:app --reload --port 8000

# Terminal 2 — frontend
cd /Users/jansenng/Desktop/website-builder/ledgerlane
node serve.mjs
```

Open `http://localhost:3000`. Drop a real PDF bank statement. Verify:
- Drop zone turns teal on hover
- Bars animate while waiting
- Stage 2 shows "X pages read", bank name, transaction count
- Non-PDF file shows error message
- PDF >5 pages shows page-limit error with upgrade link

- [ ] **Step 6: Commit**

```bash
cd /Users/jansenng/Desktop/website-builder/ledgerlane
git add index.html
git commit -m "feat: wire frontend upload to real /convert API"
```

---

## Task 6: Render real results and wire downloads

**Files:**
- Modify: `ledgerlane/index.html` — replace hardcoded `renderTable()` with real data renderer + wire download buttons

- [ ] **Step 1: Replace renderTable() with renderResults()**

Find the existing `renderTable()` function (around line 1233) and replace it entirely:

```javascript
function renderResults(data) {
  // Update preview header
  document.querySelector('.preview-head .hstack .pill').innerHTML =
    `<span class="dot" style="color:var(--accent-600)"></span> ${data.bank_name}`;
  document.querySelector('.preview-head .hstack .mono.dim').textContent = data.date_range;

  // Update stats
  document.getElementById('tx-count').textContent = data.transactions.length;
  document.getElementById('tx-flagged').textContent = data.stats.flagged;

  const fmtMoney = (n) => {
    const abs = Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return (n >= 0 ? '+$' : '−$') + abs;
  };

  // Update in/out totals in preview stats
  const statsEl = document.querySelector('.preview-stats');
  if (statsEl) {
    const statDivs = statsEl.querySelectorAll('div');
    if (statDivs[1]) statDivs[1].innerHTML = `<b class="credit">${fmtMoney(data.stats.total_in)}</b> in`;
    if (statDivs[2]) statDivs[2].innerHTML = `<b style="color:var(--bad-500)">${fmtMoney(data.stats.total_out)}</b> out`;
  }

  // Render transaction rows
  const tbody = document.getElementById('tbody');
  const rows = data.transactions.map((t) => {
    const amount = t.amount;
    const amountCell = amount === null
      ? `<td class="num dim">—</td>`
      : `<td class="num ${amount > 0 ? 'credit' : ''}">${fmtMoney(amount)}</td>`;
    const balance = t.balance !== null && t.balance !== undefined
      ? `$${Math.abs(t.balance).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
      : '—';
    return `<tr>
      <td class="date">${t.date || ''}</td>
      <td>
        <div><span class="cell-edit desc" contenteditable="true" spellcheck="false">${t.description || ''}</span></div>
        <div class="sub mono">${t.raw_description || ''}</div>
      </td>
      <td><span class="cell-edit" contenteditable="true" spellcheck="false">${t.category || ''}</span></td>
      ${amountCell}
      <td class="num dim">${balance}</td>
    </tr>`;
  });
  tbody.innerHTML = rows.join('');

  showStage('preview');
}
```

- [ ] **Step 2: Wire download buttons**

Find the export chips section and replace the existing export chip click handler (around line 1317) with:

```javascript
/* Export chip selection + download */
document.querySelectorAll('.chip').forEach(chip => {
  chip.addEventListener('click', () => {
    document.querySelectorAll('.chip').forEach(c => c.classList.remove('on'));
    chip.classList.add('on');
    const fmt = chip.dataset.fmt;
    document.getElementById('export-ext').textContent = {
      csv: 'CSV', xlsx: 'Excel', qbo: 'QuickBooks', ofx: 'OFX', json: 'JSON'
    }[fmt] || fmt.toUpperCase();
  });
});

document.getElementById('export-btn').addEventListener('click', () => {
  if (!currentToken) return;
  const activeFmt = document.querySelector('.chip.on')?.dataset.fmt || 'csv';
  window.location.href = `${API_BASE}/download/${currentToken}/${activeFmt}`;
});
```

- [ ] **Step 3: Remove the old renderTable() call at bottom of script**

Find and delete the line:

```javascript
renderTable();
```

(It's near the bottom of the script, before `applyTweaks()`)

- [ ] **Step 4: Manual end-to-end test**

With both servers running:
1. Drop a real PDF bank statement (3–5 pages)
2. Wait for processing (~5–15s)
3. Verify the table shows real transactions with correct amounts
4. Click "CSV" chip then "Download CSV →" — file should download
5. Click "Excel" chip then download — Excel file should open
6. Click "QuickBooks" chip then download — .qbo file

- [ ] **Step 5: Commit**

```bash
git add index.html
git commit -m "feat: render real transactions and wire download buttons to API"
```

---

## Task 7: Error handling polish

**Files:**
- Modify: `ledgerlane/index.html` — password-protected PDF error, generic 502 error

- [ ] **Step 1: Handle password-protected PDFs in backend**

In `ledgerlane/backend/main.py`, in the `/convert` endpoint, update the pypdf try block:

```python
    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise HTTPException(
                status_code=400, detail={"error": "encrypted_pdf"}
            )
        page_count = len(reader.pages)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "invalid_pdf"})
```

- [ ] **Step 2: Add encrypted PDF test**

```python
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
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/test_main.py -v
```

Expected: all `PASSED`

- [ ] **Step 4: Handle encrypted PDF error in frontend**

In `handleConvertError()` in `index.html`, add a new condition before the else clause:

```javascript
  } else if (detail && detail.error === 'encrypted_pdf') {
    showUploadError(
      'This PDF is password-protected. Remove the password and try again. ' +
      'Most banking apps let you download an unprotected version.'
    );
  } else if (detail && detail.error === 'invalid_pdf') {
    showUploadError('This file doesn\'t appear to be a valid PDF. Please try a different file.');
  }
```

- [ ] **Step 5: Commit**

```bash
git add backend/main.py backend/tests/test_main.py index.html
git commit -m "feat: handle encrypted and invalid PDFs with clear error messages"
```

---

## Task 8: Local end-to-end screenshot verification

**Files:**
- Read-only: `ledgerlane/temporary screenshots/` — compare before/after

- [ ] **Step 1: Ensure both servers are running**

```bash
# Terminal 1
cd ledgerlane/backend && source .venv/bin/activate && uvicorn main:app --reload --port 8000

# Terminal 2
cd ledgerlane && node serve.mjs
```

- [ ] **Step 2: Take screenshot of initial state**

```bash
cd /Users/jansenng/Desktop/website-builder/ledgerlane
node screenshot.mjs http://localhost:3000 close-upload
```

Read the screenshot with the Read tool. Verify:
- Upload zone is clean with no error banners
- Three trust chips visible below the drop zone

- [ ] **Step 3: Run full test suite one final time**

```bash
cd backend && pytest tests/test_main.py -v
```

Expected: all `PASSED`

- [ ] **Step 4: Final commit**

```bash
cd /Users/jansenng/Desktop/website-builder/ledgerlane
git add -A
git commit -m "feat: LedgerLane v1 — real PDF conversion with Claude Haiku 4.5"
```

---

## Self-Review Checklist

- [x] Health endpoint → Task 1
- [x] PDF page counting + 5-page limit → Task 2
- [x] Claude Haiku extraction with native PDF + prompt caching → Task 3
- [x] In-memory job store with 1hr TTL + cleanup → Task 3
- [x] Download: CSV, Excel, QBO, OFX, JSON → Task 4
- [x] Frontend real upload (drag/drop + browse) → Task 5
- [x] Frontend page-limit error with upgrade CTA → Task 5
- [x] Frontend real transaction rendering → Task 6
- [x] Download button wired to API → Task 6
- [x] Encrypted PDF error → Task 7
- [x] Invalid PDF / non-statement error → Task 7
- [x] CORS configured → Task 1
- [x] Prompt caching on system prompt → Task 3
- [x] All types consistent: `jobs` dict shape used identically in Task 3 (write) and Task 4 (read)
