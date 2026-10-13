"""The match-quality trend watch: per-month figures that foot to
the day reports and close packs, deltas in code, and a watch that
names deteriorations only — or says "within tolerance" out loud.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from cash_recon.api import Service, create_app
from cash_recon.config import Policy as PolicyConfig, WatchPolicy
from cash_recon.dataset import build_october_dataset
from cash_recon.gate import approve_pack
from cash_recon.llm import OfflineReader
from cash_recon.models import BankLine
from cash_recon.objects import InMemoryObjectStore
from cash_recon.pipeline import ingest_october_dataset, run_day
from cash_recon.rematch import rematch_suspense
from cash_recon.report import day_report
from cash_recon.store import InMemoryStore
from cash_recon.trends import evaluate_watch, match_quality

from tests.conftest import ASSEMBLER, CONTROLLER, run_and_approve, run_through

D = Decimal


def two_month_world(world):
    ds, store, objects = world
    run_through(store, objects, date(2026, 9, 30))
    october = build_october_dataset()
    ingest_october_dataset(store, objects, october)
    for day in sorted({l.value_date for l in october.bank_lines}):
        run_and_approve(store, objects, day)
    return ds, store, objects, october


def test_month_figures_foot_to_day_reports_and_close_packs(world):
    ds, store, _objects, october = two_month_world(world)
    trend = match_quality(store, WatchPolicy())
    months = {m["month"]: m for m in trend["months"]}
    assert set(months) == {"2026-06", "2026-07", "2026-09", "2026-10"}

    policy = PolicyConfig()
    for key, last_day in (("2026-09", 30), ("2026-10", 31)):
        year, month = (int(p) for p in key.split("-"))
        days = sorted({l.value_date for l in store.bank_lines()
                       if (l.value_date.year, l.value_date.month) == (year, month)})
        bank = sum((D(day_report(store, d, policy)["bank_total"]) for d in days), D("0"))
        allocated = sum((D(day_report(store, d, policy)["allocated_total"]) for d in days), D("0"))
        assert D(months[key]["bank_value"]) == bank
        assert D(months[key]["allocated_value"]) == allocated
        tier_total = sum((D(s["value"]) for s in months[key]["tier_mix"].values()), D("0"))
        assert tier_total == allocated

    # The close packs' own committed figures for the same months.
    assert months["2026-09"]["bank_value"] == "219914.65"
    assert months["2026-09"]["suspense_received_value"] == "61086.99"
    assert months["2026-10"]["bank_value"] == "113495.09"
    assert months["2026-10"]["suspense_received_value"] == "34954.75"

    # Opening seed months: one line each, wholly in suspense.
    assert months["2026-06"]["bank_value"] == "940.00"
    assert months["2026-06"]["suspense_received_value"] == "940.00"
    assert months["2026-07"]["bank_value"] == "2340.00"


def test_auto_rate_derives_from_the_dataset_gold(world):
    ds, store, _objects, october = two_month_world(world)
    months = {m["month"]: m for m in match_quality(store, WatchPolicy())["months"]}

    sept_amounts = {l.bank_line_id: l.amount for l in ds.bank_lines}
    expected_auto = sum(
        (sept_amounts[bid] for bid, g in ds.gold.items() if g.auto), D("0")
    )
    assert D(months["2026-09"]["auto_applied_value"]) == expected_auto
    expected_rate = (expected_auto / D(months["2026-09"]["bank_value"])).quantize(D("0.0001"))
    assert months["2026-09"]["auto_applied_rate"] == str(expected_rate)

    oct_amounts = {l.bank_line_id: l.amount for l in october.bank_lines}
    expected_auto_oct = sum(
        (oct_amounts[bid] for bid, g in october.gold.items() if g.auto), D("0")
    )
    assert D(months["2026-10"]["auto_applied_value"]) == expected_auto_oct


def test_october_exception_mix_names_the_planted_mismatch(world):
    _ds, store, _objects, _october = two_month_world(world)
    months = {m["month"]: m for m in match_quality(store, WatchPolicy())["months"]}
    mismatch = months["2026-10"]["exception_mix"]["NETTING_MISMATCH"]
    assert mismatch["count"] == 1
    assert mismatch["value"] == "100.00"  # 4,350.00 due at 13% − 4,250.00 sent at 15%


def test_deltas_are_computed_between_months(world):
    _ds, store, _objects, _oct = two_month_world(world)
    months = {m["month"]: m for m in match_quality(store, WatchPolicy())["months"]}
    october = months["2026-10"]
    september = months["2026-09"]
    assert october["deltas"]["auto_applied_rate"] is not None
    expected = (D(october["auto_applied_rate"]) - D(september["auto_applied_rate"]))
    assert D(october["deltas"]["auto_applied_rate"]) == expected


def test_dwell_and_recovery_on_a_rematch_world():
    policy = PolicyConfig()
    store = InMemoryStore()
    objects = InMemoryObjectStore()
    store.add_bank_line(BankLine(bank_line_id="BL-M1", value_date=date(2026, 9, 3),
                                 amount=D("800.00"), payer_name="Late Policy Co",
                                 reference="NSP-9001", narrative=""))
    run_day(store, objects, OfflineReader(), policy, date(2026, 9, 3), actor=ASSEMBLER)
    from cash_recon.models import Installment, PolicyStatus, Receivable

    store.upsert_receivable(Receivable(
        policy_number="NSP-9001", insured_name="Late Policy Co", broker_id=None,
        status=PolicyStatus.ACTIVE,
        installments=[Installment(installment_id="NSP-9001-I1", policy_number="NSP-9001",
                                   sequence=1, due_date=date(2026, 9, 1), amount_due=D("800.00"))]))
    rematch_suspense(store, OfflineReader(), policy, ASSEMBLER, date(2026, 9, 5))
    pack = store.pack("PACK-REMATCH-2026-09-05")
    approve_pack(store, pack.pack_id, CONTROLLER, policy)

    months = {m["month"]: m for m in match_quality(store, WatchPolicy())["months"]}
    sept = months["2026-09"]
    # Received on the 3rd, resolved on the 5th: two days of dwell.
    assert sept["suspense_resolved_items"] == 1
    assert sept["avg_suspense_dwell_days"] == "2.00"
    assert sept["rematch"] == {"attempted": 1, "recovered": 1, "recovery_rate": "1.0000"}


def _row(month, auto, dwell, exc):
    return {
        "month": month,
        "auto_applied_rate": auto,
        "avg_suspense_dwell_days": dwell,
        "exception_mix": {
            code: {"count": 1, "value": "10.00", "share": share}
            for code, share in exc.items()
        },
    }


def test_watch_names_each_deterioration():
    policy = WatchPolicy()
    prev = _row("2026-09", "0.3000", "1.00", {"PARTIAL": "0.0200"})
    cur = _row("2026-10", "0.2000", "3.50", {"PARTIAL": "0.0450"})
    watch = evaluate_watch([prev, cur], policy)
    assert watch["status"] == "findings"
    kinds = {(f["kind"], f.get("code")) for f in watch["findings"]}
    assert ("AUTO_RATE_DROP", None) in kinds
    assert ("EXCEPTION_SHARE_DOUBLED", "PARTIAL") in kinds
    assert ("SUSPENSE_DWELL_UP", None) in kinds
    for finding in watch["findings"]:
        assert finding["figures"]  # every finding carries its figures


def test_watch_within_tolerance_says_so():
    policy = WatchPolicy()
    prev = _row("2026-09", "0.3000", "2.00", {"PARTIAL": "0.0200"})
    # Small movements, and a doubling that stays under the floor.
    cur = _row("2026-10", "0.2800", "3.00", {"PARTIAL": "0.0250", "OVERPAID": "0.0040"})
    prev["exception_mix"]["OVERPAID"] = {"count": 1, "value": "1.00", "share": "0.0010"}
    watch = evaluate_watch([prev, cur], policy)
    assert watch["status"] == "within tolerance"
    assert watch["findings"] == []
    assert "Within tolerance" in watch["note"]
    assert watch["compared"] == ["2026-09", "2026-10"]


def test_watch_on_the_real_books_is_structured(world):
    _ds, store, _objects, _oct = two_month_world(world)
    watch = match_quality(store, WatchPolicy())["watch"]
    assert watch["compared"] == ["2026-09", "2026-10"]
    assert watch["status"] in ("findings", "within tolerance")
    assert watch["note"]
    if watch["status"] == "within tolerance":
        assert watch["findings"] == []


def test_trends_api(world):
    _ds, store, objects, _oct = two_month_world(world)
    client = TestClient(create_app(Service(store, objects, OfflineReader(), PolicyConfig())))
    resp = client.get("/api/trends")
    assert resp.status_code == 200
    body = resp.json()
    assert [m["month"] for m in body["months"]] == ["2026-06", "2026-07", "2026-09", "2026-10"]
    assert body["watch"]["compared"] == ["2026-09", "2026-10"]
