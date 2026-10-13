"""Broker statement reconciliation: the broker's version of the
month, checked against the books.

These tests pin the import discipline (control totals checked,
refused whole, dedupe, supersession), the five classes on a mini
world with one planted row of each, the footing identities
(statement classes sum, books classes sum, and posted + at-gate +
on-rejected equals the month's by-broker day splits), the
workbench mapping (a difference that maps to an open exception
names it; assignment flows through the existing workbench only),
and the shipped example statement against the real October books.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from cash_recon.broker_statement import (
    BrokerStatementError,
    broker_reconciliation_export,
    build_reconciliation,
    import_broker_statement,
    render_broker_reconciliation_text,
    standing_statement,
    statement_summaries,
)
from cash_recon.close_pack import close_pack_export
from cash_recon.config import Policy as PolicyConfig
from cash_recon.models import (
    Allocation,
    BankLine,
    Broker,
    ExceptionCode,
    ExceptionRecord,
    Installment,
    PackStatus,
    PolicyStatus,
    PostingEntry,
    PostingPack,
    RatePeriod,
    Receivable,
    SuspenseItem,
    Tier,
)
from cash_recon.store import InMemoryStore, SqliteStore
from cash_recon.workbench import assign_exception

CONTROLLER = "Priya Nair"


def _policy(number, broker_id, premium):
    return Receivable(
        policy_number=number, insured_name=f"Insured {number}", broker_id=broker_id,
        status=PolicyStatus.ACTIVE,
        installments=[Installment(
            installment_id=f"{number}-I1", policy_number=number, sequence=1,
            due_date=date(2026, 10, 1), amount_due=D(premium))],
    )


def _line(line_id, day, amount, payer="Mini Brokers"):
    return BankLine(
        bank_line_id=line_id, value_date=day, amount=D(amount),
        payer_name=payer, reference=None, narrative="premium",
    )


def _pack(store, pack_id, day, line_id, policy, amount, status):
    alloc = Allocation(
        bank_line_id=line_id, policy_number=policy,
        installment_id=f"{policy}-I1", amount=D(amount), tier=Tier.T2_NETTING,
    )
    pack = PostingPack(
        pack_id=pack_id, value_date=day, assembled_by="cash-team",
        entries=[PostingEntry(kind="allocation", allocation=alloc)],
        status=status,
        approvals=[CONTROLLER] if status is PackStatus.APPROVED else [],
        decided_at=(datetime(2026, 10, 31, tzinfo=timezone.utc)
                    if status is not PackStatus.PROPOSED else None),
    )
    store.save_pack(pack)
    store.save_allocations([alloc], pack_id)
    return pack


def _park(store, line_id, day, amount):
    store.save_suspense(SuspenseItem(
        suspense_id=f"SUS-{line_id}", bank_line_id=line_id, amount=D(amount),
        reason="UNIDENTIFIED", received_date=day,
    ))


def _mini_world(store=None):
    """BRK-MINI nets at 10% to 2026-10-14, 12% from 2026-10-15.

    Applied (approved): NSP-A 900.00 (Oct 5, 10%), NSP-B 1,800.00
    (Oct 6, 10%), NSP-C 2,640.00 (Oct 20, 12%), NSP-D 3,520.00
    (Oct 21, 12%). At the gate: NSP-F 616.00 proposed. On a
    rejected pack: NSP-G 704.00. NSP-C carries an open
    NETTING_MISMATCH exception for its row to map to.
    """
    store = store or InMemoryStore()
    store.upsert_broker(Broker(
        broker_id="BRK-MINI", name="Mini Brokers", commission_rate=D("0.12"),
        rate_history=[
            RatePeriod(rate=D("0.10"), effective_from=date(2026, 1, 1),
                       effective_to=date(2026, 10, 14)),
            RatePeriod(rate=D("0.12"), effective_from=date(2026, 10, 15),
                       effective_to=None),
        ],
    ))
    for number, premium in (("NSP-A", "1000.00"), ("NSP-B", "2000.00"),
                            ("NSP-C", "3000.00"), ("NSP-D", "4000.00"),
                            ("NSP-F", "700.00"), ("NSP-G", "800.00")):
        store.upsert_receivable(_policy(number, "BRK-MINI", premium))
    store.add_bank_line(_line("BL-A1", date(2026, 10, 5), "900.00"))
    _pack(store, "PACK-2026-10-05", date(2026, 10, 5), "BL-A1", "NSP-A",
          "900.00", PackStatus.APPROVED)
    store.add_bank_line(_line("BL-B1", date(2026, 10, 6), "1800.00"))
    _pack(store, "PACK-2026-10-06", date(2026, 10, 6), "BL-B1", "NSP-B",
          "1800.00", PackStatus.APPROVED)
    store.add_bank_line(_line("BL-C1", date(2026, 10, 20), "2640.00"))
    _pack(store, "PACK-2026-10-20", date(2026, 10, 20), "BL-C1", "NSP-C",
          "2640.00", PackStatus.APPROVED)
    store.add_bank_line(_line("BL-D1", date(2026, 10, 21), "3520.00"))
    _pack(store, "PACK-2026-10-21", date(2026, 10, 21), "BL-D1", "NSP-D",
          "3520.00", PackStatus.APPROVED)
    # At the gate: proposed, its suspense item held resolved, the
    # way the real proposal paths leave it.
    store.add_bank_line(_line("BL-F1", date(2026, 10, 22), "616.00"))
    _park(store, "BL-F1", date(2026, 10, 22), "616.00")
    _pack(store, "PACK-REMATCH-2026-10-22", date(2026, 10, 22), "BL-F1",
          "NSP-F", "616.00", PackStatus.PROPOSED)
    item = store.suspense_items()[0]
    item.status, item.resolved_on = "resolved", date(2026, 10, 22)
    store.save_suspense(item)
    # On a rejected pack: the rows stand in the index (ordinary
    # rejection re-opens nothing), the item stays resolved.
    store.add_bank_line(_line("BL-G1", date(2026, 10, 23), "704.00"))
    _park(store, "BL-G1", date(2026, 10, 23), "704.00")
    _pack(store, "PACK-REMATCH-2026-10-23", date(2026, 10, 23), "BL-G1",
          "NSP-G", "704.00", PackStatus.REJECTED)
    item = [i for i in store.suspense_items() if i.bank_line_id == "BL-G1"][0]
    item.status, item.resolved_on = "resolved", date(2026, 10, 23)
    store.save_suspense(item)
    store.add_exception(ExceptionRecord(
        exception_id="EXC-BL-C1-001", code=ExceptionCode.NETTING_MISMATCH,
        bank_line_id="BL-C1", policy_number="NSP-C", broker_id="BRK-MINI",
        amount=D("88.00"), detail="netting delta", first_seen=date(2026, 10, 20),
    ))
    return store


STATEMENT_V1 = """Broker statement — Mini Brokers, October 2026
Broker: BRK-MINI
Period: 2026-10
Policy,Gross billed,Commission,Net remitted,Remittance reference
NSP-A,1000.00,100.00,900.00,MB-1
NSP-B,2000.00,240.00,1760.00,MB-2
NSP-C,3100.00,372.00,2728.00,MB-3
NSP-E,500.00,60.00,440.00,MB-4
CONTROL TOTAL,,,5828.00,
CONTROL COUNT,,,,4,
"""

STATEMENT_V2 = """Broker statement — Mini Brokers, October 2026 (corrected)
Broker: BRK-MINI
Period: 2026-10
Policy,Gross billed,Commission,Net remitted,Remittance reference
NSP-A,1000.00,100.00,900.00,MB-1
NSP-B,2000.00,240.00,1760.00,MB-2
NSP-C,3100.00,372.00,2728.00,MB-3
CONTROL TOTAL,,,5388.00,
CONTROL COUNT,,,,3,
"""


def _rows_by_policy(body):
    return {r["policy_number"]: r for r in body["rows"]}


def test_mini_world_classifies_every_class_and_foots():
    store = _mini_world()
    report = import_broker_statement(store, STATEMENT_V1, source_name="mini.csv")
    assert report["deduped"] is False and report["lines"] == 4
    body = build_reconciliation(store, "BRK-MINI", "2026-10", PolicyConfig())
    assert body["counts"] == {
        "AGREED": 1, "AMOUNT_DIFFERS": 1, "COMMISSION_DIFFERS": 1,
        "MISSING_ON_BOOKS": 1, "MISSING_ON_STATEMENT": 1,
    }
    rows = _rows_by_policy(body)
    assert rows["NSP-A"]["class"] == "AGREED"
    assert rows["NSP-A"]["books"]["net"] == "900.00"
    # The broker priced October the 6th at the new 12%; the rate
    # in force was 10% — the expectation is priced from history.
    assert rows["NSP-B"]["class"] == "COMMISSION_DIFFERS"
    assert rows["NSP-B"]["books"]["expected_commission"] == "200.00"
    assert rows["NSP-B"]["books"]["contracted_rates"] == ["0.10"]
    assert rows["NSP-B"]["statement"]["commission"] == "240.00"
    assert rows["NSP-C"]["class"] == "AMOUNT_DIFFERS"
    assert rows["NSP-C"]["statement"]["gross"] == "3100.00"
    assert rows["NSP-C"]["books"]["gross"] == "3000.00"
    assert rows["NSP-C"]["books"]["net"] == "2640.00"
    assert rows["NSP-E"]["class"] == "MISSING_ON_BOOKS"
    assert rows["NSP-E"]["books"] is None and rows["NSP-E"]["exception"] is None
    assert rows["NSP-D"]["class"] == "MISSING_ON_STATEMENT"
    assert rows["NSP-D"]["statement"] is None
    assert rows["NSP-D"]["books"]["net"] == "3520.00"
    # The boundary: NSP-F's cash stands proposed (and NSP-G's on a
    # rejected pack) — neither is applied, so neither raises a row.
    assert "NSP-F" not in rows and "NSP-G" not in rows

    totals = body["totals"]["USD"]
    assert totals["statement_net_total"] == "5828.00"
    assert totals["statement_foots"] is True
    assert totals["books_net_total"] == "8860.00"
    assert totals["books_foots"] is True
    # The bridge to the day splits: posted + at the gate + on
    # rejected packs == the month's by-broker allocated.
    assert totals["posted"] == "8860.00"
    assert totals["at_gate"] == "616.00"
    assert totals["on_rejected_packs"] == "704.00"
    assert totals["day_split_allocated"] == "10180.00"
    assert totals["foots_to_day_splits"] is True
    assert body["foots"] is True


def test_difference_rows_map_to_open_exceptions_only():
    store = _mini_world()
    import_broker_statement(store, STATEMENT_V1)
    body = build_reconciliation(store, "BRK-MINI", "2026-10", PolicyConfig())
    rows = _rows_by_policy(body)
    assert rows["NSP-C"]["exception"]["exception_id"] == "EXC-BL-C1-001"
    assert rows["NSP-C"]["exception"]["code"] == "NETTING_MISMATCH"
    assert rows["NSP-C"]["exception"]["assignee"] is None
    # No open exception stands behind the other rows.
    assert rows["NSP-B"]["exception"] is None
    assert rows["NSP-D"]["exception"] is None
    # Assignment happens through the workbench, and the next
    # reconciliation shows it — the report holds no handling state
    # of its own.
    assign_exception(store, "EXC-BL-C1-001", CONTROLLER, "cash-team")
    body = build_reconciliation(store, "BRK-MINI", "2026-10", PolicyConfig())
    assert _rows_by_policy(body)["NSP-C"]["exception"]["assignee"] == CONTROLLER


@pytest.mark.parametrize("text,fragment", [
    (STATEMENT_V1.replace("5828.00", "5828.01"), "control total mismatch"),
    (STATEMENT_V1.replace("CONTROL COUNT,,,,4,", "CONTROL COUNT,,,,5,"),
     "control count mismatch"),
    (STATEMENT_V1.replace("BRK-MINI", "BRK-NOPE"), "not on the books"),
    (STATEMENT_V1.replace("NSP-A,1000.00", "NSP-A,ten"), "is not an amount"),
    (STATEMENT_V1.replace("Broker: BRK-MINI\n", ""), "names no broker"),
    (STATEMENT_V1.replace("Period: 2026-10\n", ""), "names no period"),
    (STATEMENT_V1.replace("NSP-A,1000.00,100.00,900.00", "NSP-A,1000.00,100.00,-900.00"),
     "negative"),
])
def test_import_refusals_store_nothing(text, fragment):
    store = _mini_world()
    with pytest.raises(BrokerStatementError, match=fragment):
        import_broker_statement(store, text)
    assert store.broker_statements() == []


def test_reimport_dedupes_and_a_corrected_statement_supersedes():
    store = _mini_world()
    first = import_broker_statement(store, STATEMENT_V1)
    again = import_broker_statement(store, STATEMENT_V1)
    assert again["deduped"] is True
    assert len(store.broker_statements()) == 1
    corrected = import_broker_statement(store, STATEMENT_V2)
    assert corrected["deduped"] is False
    assert corrected["superseded"] == [first["statement_id"]]
    standing = standing_statement(store, "BRK-MINI", "2026-10")
    assert standing.statement_id == corrected["statement_id"]
    summaries = statement_summaries(store)
    assert len(summaries) == 2
    assert sum(1 for s in summaries if s["standing"]) == 1
    # The reconciliation reads the standing (corrected) statement:
    # the phantom NSP-E row is gone.
    body = build_reconciliation(store, "BRK-MINI", "2026-10", PolicyConfig())
    assert body["statement_id"] == corrected["statement_id"]
    assert body["counts"]["MISSING_ON_BOOKS"] == 0
    assert body["totals"]["USD"]["statement_net_total"] == "5388.00"
    assert body["foots"] is True


def test_reconciliation_without_a_statement_is_refused_in_words():
    store = _mini_world()
    with pytest.raises(BrokerStatementError, match="no broker statement"):
        build_reconciliation(store, "BRK-MINI", "2026-10", PolicyConfig())


def test_export_is_byte_identical_and_the_text_names_the_classes():
    store = _mini_world()
    import_broker_statement(store, STATEMENT_V1)
    one = broker_reconciliation_export(store, "BRK-MINI", "2026-10", PolicyConfig())
    two = broker_reconciliation_export(store, "BRK-MINI", "2026-10", PolicyConfig())
    assert one == two and one["sha256"] == two["sha256"]
    text = render_broker_reconciliation_text(one)
    for needle in ("COMMISSION_DIFFERS (1)", "NSP-B", "5828.00", "8860.00",
                   "FOOTING", "EXC-BL-C1-001"):
        assert needle in text


def test_the_api_serves_import_reconciliation_and_text():
    from fastapi.testclient import TestClient

    from cash_recon.api import Service, create_app
    from cash_recon.llm import OfflineReader
    from cash_recon.objects import InMemoryObjectStore

    store = _mini_world()
    service = Service(store, InMemoryObjectStore(), OfflineReader(), PolicyConfig())
    client = TestClient(create_app(service))
    refused = client.post("/api/broker-statements/import", json={
        "text": STATEMENT_V1.replace("5828.00", "5828.01"), "source_name": "mini.csv"})
    assert refused.status_code == 422 and store.broker_statements() == []
    imported = client.post("/api/broker-statements/import", json={
        "text": STATEMENT_V1, "source_name": "mini.csv"})
    assert imported.status_code == 200
    assert imported.json()["statement_id"].startswith("BSTMT-")
    listing = client.get("/api/broker-statements").json()
    assert len(listing) == 1 and listing[0]["standing"] is True
    body = client.get("/api/broker-reconciliation/BRK-MINI/2026-10").json()
    assert body["reconciliation"]["counts"]["COMMISSION_DIFFERS"] == 1
    assert body["reconciliation"]["foots"] is True
    text = client.get("/api/broker-reconciliation/BRK-MINI/2026-10/text")
    assert text.status_code == 200 and "COMMISSION_DIFFERS (1)" in text.text
    missing = client.get("/api/broker-reconciliation/BRK-MINI/2026-11")
    assert missing.status_code == 404


def test_sqlite_store_runs_the_whole_flow(tmp_path):
    store = SqliteStore(tmp_path / "cashrecon.db")
    _mini_world(store)
    import_broker_statement(store, STATEMENT_V1)
    body = build_reconciliation(store, "BRK-MINI", "2026-10", PolicyConfig())
    assert body["counts"]["AGREED"] == 1
    assert body["foots"] is True


def _run_full_books():
    from cash_recon.dataset import build_dataset, build_october_dataset
    from cash_recon.gate import approve_pack
    from cash_recon.llm import OfflineReader
    from cash_recon.objects import InMemoryObjectStore
    from cash_recon.pipeline import (
        ingest_dataset,
        ingest_october_dataset,
        run_day,
    )

    store, objects = InMemoryStore(), InMemoryObjectStore()
    policy = PolicyConfig()
    reader = OfflineReader()
    ds = build_dataset()
    ingest_dataset(store, objects, ds)
    october = build_october_dataset()
    ingest_october_dataset(store, objects, october)
    days = sorted({l.value_date for l in ds.bank_lines}
                  | {l.value_date for l in october.bank_lines})
    for day in days:
        result = run_day(store, objects, reader, policy, day,
                         actor="cash-team")
        for key in ("pack_id", "non_premium_pack_id", "combination_pack_id"):
            if result.get(key):
                approve_pack(store, result[key], CONTROLLER, policy)
    return store


def test_the_shipped_example_against_the_real_october_books():
    store = _run_full_books()
    text = Path("examples/example-broker-statement.csv").read_text()
    report = import_broker_statement(
        store, text, source_name="example-broker-statement.csv")
    assert report["lines"] == 4 and report["net_total"] == "19061.70"
    body = build_reconciliation(store, "BRK-FERN", "2026-10", PolicyConfig())
    assert body["counts"] == {
        "AGREED": 1, "AMOUNT_DIFFERS": 1, "COMMISSION_DIFFERS": 1,
        "MISSING_ON_BOOKS": 1, "MISSING_ON_STATEMENT": 1,
    }
    rows = _rows_by_policy(body)
    assert rows["NSP-1046"]["class"] == "AGREED"
    assert rows["NSP-1007"]["class"] == "COMMISSION_DIFFERS"
    assert rows["NSP-1007"]["books"]["expected_commission"] == "485.10"
    assert rows["NSP-1007"]["books"]["contracted_rates"] == ["0.11"]
    assert rows["NSP-1047"]["class"] == "AMOUNT_DIFFERS"
    # The row maps to the exception standing open for the policy:
    # BL-103's OVERPAID (the 128.00 the broker's wrong-rate netting
    # overpaid, still in suspense). The NETTING_MISMATCH the
    # matcher raised on the line does not stand open after approval.
    assert rows["NSP-1047"]["exception"]["exception_id"] == "EXC-BL-103-OVERPAID"
    assert rows["NSP-1047"]["exception"]["code"] == "OVERPAID"
    assert rows["NSP-1099"]["class"] == "MISSING_ON_BOOKS"
    assert rows["NSP-1051"]["class"] == "MISSING_ON_STATEMENT"
    totals = body["totals"]["USD"]
    assert totals["statement_net_total"] == "19061.70"
    assert totals["books_net_total"] == "25236.90"
    assert totals["day_split_allocated"] == "25236.90"
    assert body["foots"] is True
    # BL-104's cash stands wholly in suspense (netted at a rate no
    # period carries): outside this report's boundary, no row.
    assert "NSP-1049" not in rows

    # The October close cites the reconciliation; September's has
    # no statement and gains no section.
    october = close_pack_export(store, 2026, 10)["pack"]
    (citation,) = october["broker_statement_reconciliation"]
    assert citation["broker_id"] == "BRK-FERN"
    assert citation["counts"] == body["counts"]
    assert citation["foots"] is True
    september = close_pack_export(store, 2026, 9)["pack"]
    assert "broker_statement_reconciliation" not in september
