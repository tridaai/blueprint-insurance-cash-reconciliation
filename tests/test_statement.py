"""Bank statement import: a generic CSV becomes the same bank lines
the tiers consume — validated against its own trailer controls,
all-or-nothing, and safe to re-import."""

from datetime import date
from pathlib import Path

import pytest

from cash_recon.dataset import build_dataset
from cash_recon.objects import InMemoryObjectStore
from cash_recon.pipeline import run_day
from cash_recon.config import Policy as PolicyConfig
from cash_recon.llm import OfflineReader
from cash_recon.statement import StatementImportError, import_statement
from cash_recon.store import InMemoryStore, SqliteStore

EXAMPLE = Path(__file__).parent.parent / "data" / "example-statement.csv"

SIGNED = """Date,Description,Reference,Amount
2026-09-23,Marigold Catering Co — premium NSP-1001,TXN-1,4800.00
2026-09-23,Bank fee reversal,FEE-9,-25.00
2026-09-24,Willow Creek Vets — premium NSP-1010,TXN-2,"2,960.00"
"""


def _books_only_store():
    """Brokers and receivables on the books; no bank lines yet — the
    desk before the first statement arrives."""
    store = InMemoryStore()
    ds = build_dataset()
    for b in ds.brokers:
        store.upsert_broker(b)
    for r in ds.receivables:
        store.upsert_receivable(r)
    return store


def test_clean_import_of_the_example_statement():
    store = _books_only_store()
    report = import_statement(store, EXAMPLE.read_text(), "example-statement.csv")
    assert report["data_rows"] == 5 and report["credit_rows"] == 4
    assert report["debit_rows_skipped"] == 1
    assert report["credits_total"] == "13837.60"
    assert report["control_total"] == "13837.60" and report["control_count"] == 5
    assert report["imported"] == 4 and report["skipped_duplicates"] == 0

    lines = {l.bank_line_id: l for l in store.bank_lines()}
    assert len(lines) == 4
    marigold = next(l for l in lines.values() if str(l.amount) == "4800.00")
    assert marigold.payer_name == "Marigold Catering Co"
    assert marigold.value_date == date(2026, 9, 23)
    assert marigold.reference is None  # TXN-9001 is a bank ref, not a policy ref


def test_reimport_dedupes_on_reference_amount_date():
    store = _books_only_store()
    import_statement(store, EXAMPLE.read_text(), "example-statement.csv")
    again = import_statement(store, EXAMPLE.read_text(), "example-statement.csv")
    assert again["imported"] == 0 and again["skipped_duplicates"] == 4
    assert len(store.bank_lines()) == 4  # nothing doubled


def test_control_total_mismatch_refuses_everything():
    store = _books_only_store()
    bad = EXAMPLE.read_text().replace("13837.60", "13837.61")
    with pytest.raises(StatementImportError) as err:
        import_statement(store, bad, "bad.csv")
    assert "13837.61" in str(err.value) and "13837.60" in str(err.value)
    assert store.bank_lines() == []  # nothing half-imported


def test_control_count_mismatch_refuses_everything():
    store = _books_only_store()
    bad = EXAMPLE.read_text().replace("CONTROL COUNT,,,5", "CONTROL COUNT,,,6")
    with pytest.raises(StatementImportError) as err:
        import_statement(store, bad, "bad.csv")
    assert "6" in str(err.value) and "5" in str(err.value)
    assert store.bank_lines() == []


def test_signed_amount_shape_and_thousands_separators():
    store = _books_only_store()
    report = import_statement(store, SIGNED, "signed.csv")
    assert report["imported"] == 2 and report["debit_rows_skipped"] == 1
    amounts = sorted(l.amount for l in store.bank_lines())
    from decimal import Decimal

    assert amounts == [Decimal("2960.00"), Decimal("4800.00")]


def test_unparseable_row_refuses_with_the_row_named():
    store = _books_only_store()
    bad = SIGNED.replace("2026-09-24", "not-a-date")
    with pytest.raises(StatementImportError) as err:
        import_statement(store, bad, "bad.csv")
    assert "row 4" in str(err.value)
    assert store.bank_lines() == []


def test_policy_reference_in_the_reference_column_is_kept():
    store = _books_only_store()
    text = ("Date,Description,Reference,Debit,Credit\n"
            "2026-09-23,Premium received,NSP-1010,,2960.00\n")
    import_statement(store, text, "one.csv")
    (line,) = store.bank_lines()
    assert line.reference == "NSP-1010"


def test_imported_lines_are_indistinguishable_downstream():
    store = _books_only_store()
    import_statement(store, EXAMPLE.read_text(), "example-statement.csv")
    result = run_day(store, InMemoryObjectStore(), OfflineReader(), PolicyConfig(),
                     date(2026, 9, 23), actor="cash-team")
    by_id = {o["bank_line_id"]: o for o in result["outcomes"]}
    imported = [o for o in by_id.values()]
    assert len(imported) == 2  # the two credits dated 2026-09-23
    tiers = {o["tier"] for o in imported}
    assert tiers == {"T1_EXACT", "T2_NETTING"}  # Marigold exact, Bayline netted
    assert result["report"]["footing_ok"] is True


def test_import_behaves_the_same_on_the_sqlite_store(tmp_path):
    store = SqliteStore(tmp_path / "import.db")
    ds = build_dataset()
    for b in ds.brokers:
        store.upsert_broker(b)
    report = import_statement(store, EXAMPLE.read_text(), "example-statement.csv")
    assert report["imported"] == 4
    again = import_statement(store, EXAMPLE.read_text(), "example-statement.csv")
    assert again["skipped_duplicates"] == 4 and len(store.bank_lines()) == 4


def test_import_over_http():
    from fastapi.testclient import TestClient

    from cash_recon.api import Service, create_app
    from cash_recon.objects import InMemoryObjectStore
    from cash_recon.store import InMemoryStore

    service = Service(InMemoryStore(), InMemoryObjectStore(), OfflineReader(), PolicyConfig())
    http = TestClient(create_app(service))
    ok = http.post("/api/statements/import",
                   json={"text": EXAMPLE.read_text(), "source_name": "example-statement.csv"})
    assert ok.status_code == 200 and ok.json()["imported"] == 4
    bad = http.post("/api/statements/import",
                    json={"text": EXAMPLE.read_text().replace("13837.60", "1.00"),
                          "source_name": "bad.csv"})
    assert bad.status_code == 422 and "control total mismatch" in bad.json()["detail"]
