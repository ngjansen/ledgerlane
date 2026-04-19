# LedgerLane Converter — Design Spec
_2026-04-18_

## Problem

The existing `ledgerlane/index.html` is a pure frontend prototype — the upload zone, detection animation, and transaction table are all hardcoded demos. No real PDF is ever processed. This spec covers adding a real FastAPI backend that accepts PDF uploads, counts pages, calls Claude Haiku 4.5 to extract transactions, and returns downloadable files.

## Scope

Core conversion only: upload → extract → download. No user accounts, no database, no payment. Free tier is enforced per-conversion by page count (≤5 pages).

---

## Architecture

```
Browser (index.html)
  → POST /convert (multipart PDF)
      → pypdf: count pages
          → >5 pages: 400 {error: "too_many_pages", pages: N, limit: 5}
          → ≤5 pages: Claude Haiku 4.5 (native PDF document block)
              → parse JSON transactions
              → store in-memory dict {UUID → result, expires 1hr}
              → 200 {token, transactions, meta}
  → GET /download/{token}/{format}
      → generate file in-memory
      → stream as attachment
```

---

## Backend

**File:** `ledgerlane/backend/main.py`
**Framework:** FastAPI
**Dependencies:** `anthropic`, `pypdf`, `openpyxl`, `python-multipart`, `uvicorn`
**Deploy:** Railway (`railway.toml` + `Procfile`)

### `POST /convert`

- Accepts `multipart/form-data` with a `file` field (PDF)
- Uses `pypdf.PdfReader` to count pages — no Claude call made, takes <50ms
- If `page_count > 5`: return 400 `{"error": "too_many_pages", "pages": N, "limit": 5}`
- Sends PDF as a native `document` block to `claude-haiku-4-5`
- System prompt is prompt-cached (stable across all requests)
- Claude returns JSON array of transactions
- Stores result under UUID token in an in-memory dict with 1hr expiry
- Returns:
  ```json
  {
    "token": "uuid",
    "page_count": 3,
    "bank_name": "Chase Total Checking",
    "date_range": "Mar 1 – Mar 31, 2026",
    "transactions": [
      {
        "date": "03/01",
        "description": "Direct Deposit — Acme Corp",
        "raw_description": "ACH CREDIT #8842",
        "amount": 3241.50,
        "balance": 15724.77,
        "category": "Income"
      }
    ],
    "stats": {
      "total_in": 8420.16,
      "total_out": -6911.48,
      "flagged": 0
    }
  }
  ```

### `GET /download/{token}/{format}`

- `format` is one of: `csv`, `xlsx`, `qbo`, `ofx`, `json`
- 404 if token not found or expired
- Generates file in-memory, returns with `Content-Disposition: attachment`
- Format details:
  - **CSV**: Python stdlib `csv` module
  - **Excel**: `openpyxl`, styled header row, autofit columns
  - **QBO**: OFX 1.02 SGML format (QuickBooks-compatible XML template)
  - **OFX**: OFX 2.x XML format
  - **JSON**: raw transactions array, pretty-printed

### `GET /health`

Returns `{"status": "ok"}` — used by Railway for health checks.

### CORS

Allow all origins in development. In production, restrict to the deployed frontend domain.

### In-memory store

```python
# Simple dict — no Redis needed for v1
jobs: dict[str, dict] = {}  # {token: {result, expires_at}}
```

Background cleanup task runs every 5 minutes, removes expired entries.

---

## Claude Integration

**Model:** `claude-haiku-4-5`
**Method:** Native PDF document block (handles both text-based and scanned PDFs)
**Prompt caching:** System prompt cached with `cache_control: {"type": "ephemeral"}`

**System prompt:**
```
You are a financial data extraction engine. Extract every transaction from the bank statement provided.

Return ONLY a JSON object with this exact structure — no markdown, no explanation:
{
  "bank_name": "string",
  "date_range": "string",
  "transactions": [
    {
      "date": "MM/DD",
      "description": "string (clean merchant name)",
      "raw_description": "string (original text from statement)",
      "amount": number (positive=credit, negative=debit),
      "balance": number or null,
      "category": "string (Income|Groceries|Dining|Transport|Housing|Utilities|Shopping|Software|Transfer|Cash|Refund|Interest|Other)"
    }
  ]
}

Rules:
- Include ALL rows including opening/closing balance rows
- amount is signed: credits positive, debits negative
- balance is the running balance after the transaction, null if not shown
- date format is MM/DD (no year)
- If the document is not a bank statement, return {"error": "not_a_bank_statement"}
```

---

## Frontend Changes (`index.html`)

### Stage 1 — Real upload

Drop zone `onclick` and `ondrop` handlers replaced with real logic:
- Create `FormData`, append file
- `fetch('/convert', {method: 'POST', body: formData})`
- Transition to stage 2 immediately on submit

### Stage 1.5 — Page limit rejection

If API returns `too_many_pages`:
- Stay on stage 1 (reset animation)
- Show inline error banner: *"This statement is {N} pages. Free conversions are limited to 5 pages. Create a free account to convert longer statements."*
- CTA button: "Create free account →" (placeholder `href="#"` for now)

### Stage 2 — Real progress

Bars animate while `fetch` is in-flight. Timing: bar 1 at 0s, bar 2 at 3s, bar 3 at 6s. If Claude responds faster, jump to stage 3 immediately. Detection text updated with real values from response.

### Stage 3 — Real data

- Table populated from `response.transactions`
- Header stats from `response.stats` (total in/out, flagged count)
- Bank name and date range from response
- Download buttons call `GET /download/{token}/{format}` — browser handles the file download natively

### Error handling

Network error or 500 → reset to stage 1 with toast: *"Something went wrong — please try again."*

---

## File Layout

```
ledgerlane/
├── index.html              ← modified frontend
├── backend/
│   ├── main.py             ← FastAPI app (single file)
│   ├── requirements.txt
│   ├── Procfile            ← web: uvicorn main:app --host 0.0.0.0 --port $PORT
│   └── railway.toml        ← [build] builder = "nixpacks"
├── serve.mjs               ← local static server (frontend dev)
├── screenshot.mjs
└── package.json
```

---

## Deployment

**Backend:** Railway
- Root directory: `ledgerlane/backend`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Env vars: `ANTHROPIC_API_KEY`

**Frontend:** Railway (same project, different service) or any static host
- Serve `ledgerlane/index.html`
- Backend URL injected as `const API_BASE = 'https://your-backend.railway.app'`

---

## Verification

1. Start backend locally: `cd backend && uvicorn main:app --reload`
2. Update `API_BASE` in `index.html` to `http://localhost:8000`
3. Start frontend: `node serve.mjs` in `ledgerlane/`
4. Test flows:
   - Drop a real 3-page PDF → should show real transactions
   - Drop a 6-page PDF → should show page limit error
   - Drop a non-PDF / non-statement → should show "not a bank statement" error
   - Click each download format → file should download with correct extension
5. Screenshot the full flow for visual verification
