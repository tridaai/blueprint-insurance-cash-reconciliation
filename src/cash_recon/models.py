"""Domain records for premium cash reconciliation.

The records mirror what a cash team actually works with: a broker book
with contracted commission rates, billed receivables (policies and
their installments), bank lines, and the remittance documents that
explain lump sums. Everything downstream — matches, allocations,
exceptions, posting packs — is built from these.

Amounts are Decimal. Dates are dates. Statuses are plain strings from
the small vocabularies defined here, validated at the edges.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- vocab


class PolicyStatus(str, Enum):
    ACTIVE = "active"  # in force, still billing
    PAID_IN_FULL = "paid_in_full"  # nothing left to collect
    CANCELLED = "cancelled"  # cancelled before this cash arrived


class InstallmentStatus(str, Enum):
    OPEN = "open"
    APPLIED = "applied"  # cash approved and posted against it
    PARTIALLY_APPLIED = "partially_applied"


class Tier(str, Enum):
    T1_EXACT = "T1_EXACT"
    T2_NETTING = "T2_NETTING"
    T3_REFERENCE = "T3_REFERENCE"
    T4_REMITTANCE = "T4_REMITTANCE"


class ExceptionCode(str, Enum):
    UNIDENTIFIED = "UNIDENTIFIED"  # no reference, no remittance
    PARTIAL = "PARTIAL"  # less than the amount due
    OVERPAID = "OVERPAID"  # more than the amount due
    NETTING_MISMATCH = "NETTING_MISMATCH"  # broker netted at a wrong rate
    REMITTANCE_ARITHMETIC = "REMITTANCE_ARITHMETIC"  # lines != the wire
    UNKNOWN_POLICY = "UNKNOWN_POLICY"  # cites a policy not on the books
    CANCELLED_POLICY = "CANCELLED_POLICY"  # cash for a cancelled policy
    DUPLICATE_PAYMENT = "DUPLICATE_PAYMENT"  # same cash, twice
    STALE_SUSPENSE = "STALE_SUSPENSE"  # unapplied past the configured age
    INTENT_DIFFERS = "INTENT_DIFFERS"  # note indicated another installment; applied oldest-first


class PackStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


# ---------------------------------------------------------------- inputs


class Broker(BaseModel):
    broker_id: str
    name: str
    commission_rate: Decimal  # contracted, e.g. 0.125 for 12.5%


class Installment(BaseModel):
    installment_id: str
    policy_number: str
    sequence: int  # 1-based within the policy
    due_date: date
    amount_due: Decimal
    amount_applied: Decimal = Decimal("0.00")
    status: InstallmentStatus = InstallmentStatus.OPEN

    @property
    def open_balance(self) -> Decimal:
        return self.amount_due - self.amount_applied


class Receivable(BaseModel):
    """A billed policy: the thing cash is applied against."""

    policy_number: str
    insured_name: str
    broker_id: str | None  # None = direct-billed insured
    status: PolicyStatus = PolicyStatus.ACTIVE
    installments: list[Installment] = Field(default_factory=list)

    @property
    def open_balance(self) -> Decimal:
        return sum((i.open_balance for i in self.installments), Decimal("0.00"))

    def open_installments(self) -> list[Installment]:
        """Open installments in allocation order: oldest first, by
        sequence. The order is a guarantee of the record, not an
        accident of how the installments happen to be stored."""
        return sorted(
            (i for i in self.installments if i.open_balance > 0),
            key=lambda i: i.sequence,
        )


class BankLine(BaseModel):
    """One credit on the bank statement: money that landed."""

    bank_line_id: str
    value_date: date
    amount: Decimal
    payer_name: str  # who the bank says sent it
    reference: str | None  # remittance reference / policy no., if stated
    narrative: str = ""  # free bank text, untrusted


class RemittanceLine(BaseModel):
    """One line of a remittance advice, as read from the document."""

    policy_number: str
    amount: Decimal
    citation: str = ""  # the document text this line was read from


class Remittance(BaseModel):
    """A remittance advice document (email/PDF text layer)."""

    remittance_id: str
    broker_id: str | None
    received_date: date
    stated_total: Decimal | None  # the total the document claims
    text: str  # full document text — untrusted input
    object_key: str | None = None  # where the document lives in S3
    lines: list[RemittanceLine] = Field(default_factory=list)  # after reading
    injection_flagged: bool = False


# ---------------------------------------------------------------- outputs


class Allocation(BaseModel):
    """Cash from one bank line applied to one installment."""

    bank_line_id: str
    policy_number: str
    installment_id: str
    amount: Decimal
    tier: Tier | None  # None for write-off entries (see PostingEntry)
    note: str = ""


class ExceptionRecord(BaseModel):
    exception_id: str
    code: ExceptionCode
    bank_line_id: str | None
    policy_number: str | None
    broker_id: str | None
    amount: Decimal  # the amount at issue (delta, or the full line)
    detail: str  # what happened, in words a cashier can act on
    first_seen: date
    status: str = "open"  # open | resolved | written_off
    proposed_by: str | None = None  # model/backend that proposed, if any


class SuspenseItem(BaseModel):
    """Unapplied cash held in suspense, with its aging clock."""

    suspense_id: str
    bank_line_id: str
    amount: Decimal
    received_date: date
    reason: str
    status: str = "open"  # open | resolved (closed by the re-match pass)
    resolved_on: date | None = None

    def age_days(self, as_of: date) -> int:
        return (as_of - self.received_date).days


class PostingEntry(BaseModel):
    """One line of a posting pack: an allocation, or a delta write-off."""

    kind: str  # "allocation" | "write_off"
    allocation: Allocation | None = None
    write_off_policy: str | None = None
    write_off_amount: Decimal | None = None
    write_off_exception_id: str | None = None
    auto_applied: bool = False  # T1 exact at/below the policy threshold


class PostingPack(BaseModel):
    pack_id: str
    value_date: date
    assembled_by: str
    entries: list[PostingEntry] = Field(default_factory=list)
    status: PackStatus = PackStatus.PROPOSED
    approvals: list[str] = Field(default_factory=list)  # approver names
    decided_at: datetime | None = None

    @property
    def allocation_total(self) -> Decimal:
        return sum(
            (e.allocation.amount for e in self.entries if e.allocation),
            Decimal("0.00"),
        )

    @property
    def write_off_total(self) -> Decimal:
        return sum(
            (e.write_off_amount or Decimal("0.00") for e in self.entries),
            Decimal("0.00"),
        )


class AuditEvent(BaseModel):
    at: datetime
    actor: str  # a person, "matcher", "reader:<backend>", "gate", ...
    action: str
    subject: str  # bank line / pack / exception id
    detail: str = ""
