import asyncio
import base64
import contextlib
from dotenv import load_dotenv
load_dotenv()
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


@contextlib.asynccontextmanager
async def lifespan(app_: FastAPI):
    task = asyncio.create_task(_cleanup_jobs())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="LedgerLane Converter API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = anthropic.Anthropic()


async def _cleanup_jobs():
    while True:
        await asyncio.sleep(300)
        now = time.time()
        expired = [k for k, v in jobs.items() if v["expires_at"] < now]
        for k in expired:
            del jobs[k]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/convert")
async def convert(file: UploadFile = File(...)):
    content = await file.read()

    # Count pages — fast, no Claude call
    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise HTTPException(status_code=400, detail={"error": "encrypted_pdf"})
        page_count = len(reader.pages)
    except HTTPException:
        raise
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


# ── download helpers ──────────────────────────────────────────────────────────

def _to_ofx_date(date_str: str) -> str:
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
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Transactions"
    headers = ["Date", "Description", "Raw Description", "Amount", "Balance", "Category"]
    ws.append(headers)
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
