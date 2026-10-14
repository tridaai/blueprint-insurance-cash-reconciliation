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

# Formats that read one way only. Numeric day/month dates (03/09/2026)
# read either way, so their order is settled once per file (below).
_UNAMBIGUOUS_DATE_FORMATS = ("%Y-%m-%d", "%d %b %Y", "%b %d, %Y")
_NUMERIC_DATE_FORMATS = {
    "DMY": ("%d/%m/%Y", "%d-%m-%Y"),
    "MDY": ("%m/%d/%Y", "%m-%d-%Y"),
}
_NUMERIC_DATE = re.compile(r"^\s*(\d{1,2})[/-](\d{1,2})[/-](\d{4})\s*$")
DATE_ORDERS = tuple(_NUMERIC_DATE_FORMATS)
_TRAILER_RE = re.compile(r"^\s*(?:control\s+)?(total|count)\b", re.IGNORECASE)


class StatementImportError(RuntimeError):
    """The statement was refused. The message says why, with numbers."""


def _norm_header(cell: str) -> str:
    return re.sub(r"\s+", " ", cell.strip().lower())


def _plain_number(text: str) -> str | None:
    """A money figure as the bank printed it, rewritten as a plain
    decimal — or None when it cannot be read with certainty. Money
    carries at most two decimal places, which is what tells a decimal
    separator from a thousands separator: "4.800,00" and "4,800.00"
    are both 4800.00, "4800,50" is 4800.50, and "1.234" (three digits
    after the dot) is 1234. Stripping separators blindly read
    "4.800,00" as 4.80 — the bug this replaces."""
    if re.fullmatch(r"\d+(\.\d{1,2})?", text):
        return text
    if "," in text and "." in text:
        last = max(text.rfind(","), text.rfind("."))
        decimal_sep = text[last]
        group_sep = "." if decimal_sep == "," else ","
        whole, frac = text[:last], text[last + 1:]
        if (re.fullmatch(r"\d{1,2}", frac)
                and re.fullmatch(r"\d{1,3}(%s\d{3})*" % re.escape(group_sep), whole)):
            return whole.replace(group_sep, "") + "." + frac
        return None
    for sep in (",", "."):
        if sep in text:
            if re.fullmatch(r"\d{1,3}(%s\d{3})+" % re.escape(sep), text):
                return text.replace(sep, "")          # thousands groups only
            if sep == "," and re.fullmatch(r"\d+,\d{1,2}", text):
                return text.replace(",", ".")          # decimal comma
            return None
    return None


def _parse_amount(cell: str, row_no: int) -> Decimal:
    text = cell.strip()
    if not text:
        return ZERO
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace("£", "").replace("$", "").replace("€", "").strip()
    if text.startswith("-"):
        negative = True
        text = text[1:].strip()
    plain = _plain_number(text)
    if plain is None:
        raise StatementImportError(
            f"row {row_no}: amount {cell!r} does not parse as money (or its "
            "decimal and thousands separators cannot be told apart); nothing "
            "was imported")
    value = Decimal(plain)
    return cents(-value if negative else value)


def _date_order(cells: list[tuple[int, str]], stated: str | None) -> str | None:
    """Settle, once for the whole file, whether numeric dates are
    day-first or month-first. A date whose first number exceeds 12
    can only be day-first; one whose second exceeds 12, month-first.
    A file that proves both is refused, and so is a file whose every
    numeric date reads either way when the order is not stated —
    guessing silently read 09/10/2026 (10 September, US) as 9 October."""
    proofs: dict[str, tuple[int, str]] = {}
    numeric = []
    for row_no, text in cells:
        m = _NUMERIC_DATE.match(text)
        if not m:
            continue
        numeric.append((row_no, text))
        first, second = int(m.group(1)), int(m.group(2))
        if first > 12:
            proofs.setdefault("DMY", (row_no, text.strip()))
        if second > 12:
            proofs.setdefault("MDY", (row_no, text.strip()))
    if stated is not None and stated not in _NUMERIC_DATE_FORMATS:
        raise StatementImportError(
            f"date order {stated!r} is not one of {', '.join(DATE_ORDERS)}")
    if not numeric:
        return stated
    if len(proofs) == 2:
        (r1, d1), (r2, d2) = proofs["DMY"], proofs["MDY"]
        raise StatementImportError(
            f"the file mixes date orders: row {r1} ({d1}) can only be day-first "
            f"and row {r2} ({d2}) only month-first; nothing was imported")
    if proofs:
        (order, (row_no, text)), = proofs.items()
        if stated is not None and stated != order:
            raise StatementImportError(
                f"the stated date order {stated} contradicts row {row_no} ({text}), "
                f"which can only be {order}; nothing was imported")
        return order
    if stated is None:
        row_no, text = numeric[0]
        raise StatementImportError(
            f"every date in the file reads either way (e.g. row {row_no}: {text.strip()} "
            "is day-first or month-first): state the bank's date order "
            f"({' or '.join(DATE_ORDERS)}); nothing was imported")
    return stated


def _parse_date(cell: str, row_no: int, order: str | None = None) -> date:
    text = cell.strip()
    formats = _UNAMBIGUOUS_DATE_FORMATS + (_NUMERIC_DATE_FORMATS[order] if order else ())
    for fmt in formats:
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


def _line_id(value_date: date, bank_ref: str, amount: Decimal, occurrence: int = 1) -> str:
    """The line's id is a digest of its dedupe key — bank reference,
    amount, value date — so the same line always gets the same id,
    in any store, and a re-import recognises it. Two identical rows
    in ONE file are two payments (two $500 cash deposits the same
    day), so the second and later copies carry their occurrence in
    the key: the file keeps both, and a re-import still recognises
    each — keying on the fields alone silently dropped the second."""
    key = f"{value_date.isoformat()}|{bank_ref}|{amount:.2f}"
    if occurrence > 1:
        key += f"|#{occurrence}"
    return "IMP-" + hashlib.sha256(key.encode()).hexdigest()[:12].upper()


def import_batch_id(text: str) -> str:
    """The batch id of one statement import: a digest of the file's
    own text, so the same statement re-imported carries the same
    batch id, and every line it brought in traces to that batch."""
    return "STMT-" + hashlib.sha256(text.encode()).hexdigest()[:10].upper()


def import_statement(store: Store, text: str, source_name: str = "statement",
                     date_order: str | None = None) -> dict:
    """Import one statement's text. Returns the import report;
    raises StatementImportError (nothing stored) on any refusal.
    `date_order` ("DMY" or "MDY") states how the bank writes numeric
    dates; it is needed only when no date in the file settles it."""
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

    def is_trailer(row):
        return bool(_TRAILER_RE.match(row[0]) or _TRAILER_RE.match(cell(row, "description"))) \
            and not cell(row, "date").strip()

    order = _date_order(
        [(n, cell(r, "date")) for n, r in enumerate(rows[header_at + 1:], start=header_at + 2)
         if any(c.strip() for c in r) and not is_trailer(r)],
        date_order)
    seen_keys: dict[tuple, int] = {}

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
        value_date = _parse_date(cell(row, "date"), row_no, order)
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
        dedupe_key = (value_date, bank_ref, amount)
        seen_keys[dedupe_key] = seen_keys.get(dedupe_key, 0) + 1
        parsed.append(BankLine(
            bank_line_id=_line_id(value_date, bank_ref, amount, seen_keys[dedupe_key]),
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
        "date_order": order,
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
