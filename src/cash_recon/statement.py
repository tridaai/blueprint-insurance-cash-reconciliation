"""Bank statement import: a generic CSV in, bank lines out.

The pipeline's tiers consume `BankLine` records; until now the only
way lines arrived was the synthetic dataset's own construction.
This importer takes the layout a bank actually exports — a CSV with
a date, a description, a reference, and either a debit/credit pair
or a single signed amount — and converts it into the same records,
indistinguishable downstream: same tiers, same gate, same reports.

The column mapping is a small config in code (`COLUMN_ALIASES`): a
header is recognised by any of its known names. Two statement
shapes are handled, debit/credit columns or one signed amount.

Import is all-or-nothing and self-checking:

- Every row is parsed and validated before anything is stored; a
  row that does not parse refuses the whole import, naming the row.
- When the file carries trailer controls — a CONTROL TOTAL row
  and/or a CONTROL COUNT row — the parsed credits and the parsed
  row count must equal them exactly, or the import is refused with
  the discrepancy stated. Nothing is half-imported.
- Re-importing the same file cannot double-apply cash. Each line's
  id is a digest of exactly its dedupe key — bank reference +
  amount + value date — so an already-seen line is recognised
  through the store port and reported as skipped, not stored twice.

Only credits become bank lines (this system reconciles premium cash
received); debit rows are counted in the import report and left
behind. The statement's reference column feeds the line's reference
only when it carries a policy number; otherwise the matcher's
reference search works on the description, as it does for any line.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import date, datetime, timezone
from decimal import Decimal

from .matcher import POLICY_RE
from .models import AuditEvent, BankLine
from .money import ZERO, cents
from .store import Store

# Canonical field -> the header names a bank might use for it.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "value date", "transaction date", "posting date"),
    "description": ("description", "details", "narrative", "particulars"),
    "reference": ("reference", "ref", "bank reference", "transaction reference"),
    "payer": ("payer", "payer name", "from", "originator"),
    "debit": ("debit", "debits", "withdrawal", "withdrawals", "paid out"),
    "credit": ("credit", "credits", "deposit", "deposits", "paid in"),
    "amount": ("amount", "signed amount", "value"),
}

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d %b %Y", "%b %d, %Y")
_TRAILER_RE = re.compile(r"^\s*(?:control\s+)?(total|count)\b", re.IGNORECASE)


class StatementImportError(RuntimeError):
    """The statement was refused. The message says why, with numbers."""


def _norm_header(cell: str) -> str:
    return re.sub(r"\s+", " ", cell.strip().lower())


def _parse_amount(cell: str, row_no: int) -> Decimal:
    text = cell.strip()
    if not text:
        return ZERO
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "").replace("£", "").replace("$", "").replace("€", "").strip()
    if text.startswith("-"):
        negative = True
        text = text[1:]
    try:
        value = Decimal(text)
    except Exception:
        raise StatementImportError(f"row {row_no}: amount {cell!r} does not parse") from None
    return cents(-value if negative else value)


def _parse_date(cell: str, row_no: int) -> date:
    text = cell.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise StatementImportError(f"row {row_no}: date {cell!r} does not parse")


def _payer_of(description: str, payer_cell: str) -> str:
    if payer_cell.strip():
        return payer_cell.strip()
    for sep in (" — ", " - ", " | "):
        if sep in description:
            return description.split(sep)[0].strip()
    return description.strip()


def _line_id(value_date: date, bank_ref: str, amount: Decimal) -> str:
    """The line's id is a digest of its dedupe key — bank reference,
    amount, value date — so the same line always gets the same id,
    in any store, and a re-import recognises it."""
    key = f"{value_date.isoformat()}|{bank_ref}|{amount:.2f}"
    return "IMP-" + hashlib.sha256(key.encode()).hexdigest()[:12].upper()


def import_batch_id(text: str) -> str:
    """The batch id of one statement import: a digest of the file's
    own text, so the same statement re-imported carries the same
    batch id, and every line it brought in traces to that batch."""
    return "STMT-" + hashlib.sha256(text.encode()).hexdigest()[:10].upper()


def import_statement(store: Store, text: str, source_name: str = "statement") -> dict:
    """Import one statement's text. Returns the import report;
    raises StatementImportError (nothing stored) on any refusal."""
    batch_id = import_batch_id(text)
    rows = list(csv.reader(io.StringIO(text)))
    header_at = None
    columns: dict[str, int] = {}
    for i, row in enumerate(rows):
        cells = [_norm_header(c) for c in row]
        found = {
            field: cells.index(alias)
            for field, aliases in COLUMN_ALIASES.items()
            for alias in aliases
            if alias in cells
        }
        if "date" in found and ("credit" in found or "amount" in found):
            header_at, columns = i, found
            break
    if header_at is None:
        raise StatementImportError(
            "no header row recognised: a statement needs a date column and "
            "a credit or signed-amount column (see COLUMN_ALIASES)"
        )

    def cell(row, field):
        idx = columns.get(field)
        return row[idx] if idx is not None and idx < len(row) else ""

    parsed: list[BankLine] = []
    debit_rows = 0
    credits_total = ZERO
    data_rows = 0
    control_total: Decimal | None = None
    control_count: int | None = None

    for row_no, row in enumerate(rows[header_at + 1:], start=header_at + 2):
        if not any(c.strip() for c in row):
            continue  # blank line
        trailer = _TRAILER_RE.match(row[0]) or _TRAILER_RE.match(cell(row, "description"))
        if trailer and not cell(row, "date").strip():
            value_cell = cell(row, "credit") or cell(row, "amount")
            if trailer.group(1).lower() == "total":
                control_total = _parse_amount(value_cell, row_no)
            else:
                try:
                    control_count = int(_parse_amount(value_cell, row_no))
                except Exception:
                    raise StatementImportError(
                        f"row {row_no}: control count {value_cell!r} does not parse"
                    ) from None
            continue

        data_rows += 1
        value_date = _parse_date(cell(row, "date"), row_no)
        description = cell(row, "description").strip()
        bank_ref = cell(row, "reference").strip()
        if "amount" in columns and "credit" not in columns:
            signed = _parse_amount(cell(row, "amount"), row_no)
            if signed == 0:
                raise StatementImportError(f"row {row_no}: amount is zero")
            if signed < 0:
                debit_rows += 1
                continue
            amount = signed
        else:
            credit = _parse_amount(cell(row, "credit"), row_no)
            debit = _parse_amount(cell(row, "debit"), row_no) if "debit" in columns else ZERO
            if credit > 0 and debit > 0:
                raise StatementImportError(f"row {row_no}: both a debit and a credit are set")
            if credit == 0 and debit == 0:
                raise StatementImportError(f"row {row_no}: neither a debit nor a credit is set")
            if credit == 0:
                debit_rows += 1
                continue
            amount = credit
        credits_total += amount
        parsed.append(BankLine(
            bank_line_id=_line_id(value_date, bank_ref, amount),
            value_date=value_date,
            amount=amount,
            payer_name=_payer_of(description, cell(row, "payer")),
            reference=bank_ref if POLICY_RE.search(bank_ref) else None,
            narrative=description,
            source="import",
            import_batch=batch_id,
        ))

    if control_total is not None and credits_total != control_total:
        raise StatementImportError(
            f"control total mismatch: the statement's trailer states {control_total} "
            f"in credits but its rows sum to {credits_total}; nothing was imported"
        )
    if control_count is not None and data_rows != control_count:
        raise StatementImportError(
            f"control count mismatch: the statement's trailer states {control_count} "
            f"data rows but {data_rows} were parsed; nothing was imported"
        )

    existing = {l.bank_line_id for l in store.bank_lines()}
    imported_ids: list[str] = []
    skipped = 0
    for line in parsed:
        if line.bank_line_id in existing:
            skipped += 1
            continue
        store.add_bank_line(line)
        existing.add(line.bank_line_id)
        imported_ids.append(line.bank_line_id)

    report = {
        "source": source_name,
        "batch_id": batch_id,
        "data_rows": data_rows,
        "credit_rows": len(parsed),
        "debit_rows_skipped": debit_rows,
        "credits_total": str(credits_total),
        "control_total": str(control_total) if control_total is not None else None,
        "control_count": control_count,
        "imported": len(imported_ids),
        "skipped_duplicates": skipped,
        "imported_ids": imported_ids,
    }
    store.audit(AuditEvent(
        at=datetime.now(timezone.utc), actor="statement_import",
        action="statement_imported", subject=source_name,
        detail=f"batch {batch_id}: {len(imported_ids)} line(s) imported, {skipped} already seen, "
               f"{debit_rows} debit row(s) skipped, credits {credits_total}",
    ))
    return report
