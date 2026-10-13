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
    CURRENCY_NO_OPEN_ITEMS = "CURRENCY_NO_OPEN_ITEMS"  # the payer has nothing open in this line's currency


class PackStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class ReturnReason(str, Enum):
    """Why the bank took cash back, as stated on the return advice."""

    NSF = "NSF"  # the payer's account held insufficient funds
    ACCOUNT_CLOSED = "ACCOUNT_CLOSED"
    STOP_PAYMENT = "STOP_PAYMENT"
    RECALL = "RECALL"  # the sending bank recalled the wire


# ---------------------------------------------------------------- inputs


class RatePeriod(BaseModel):
    """One contracted commission rate, in force from a date.

    A broker's rate changes mid-book: the contract is renegotiated
    and remittances before the change net at the old rate, after it
    at the new one. The history is what lets the matcher price a
    remittance by the period it belongs to instead of by whatever
    the rate happens to be today."""

    effective_from: date
    rate: Decimal


class Broker(BaseModel):
    broker_id: str
    name: str
    commission_rate: Decimal  # the rate currently in force, e.g. 0.125 for 12.5%
    rate_history: list[RatePeriod] = Field(default_factory=list)

    def rate_on(self, as_of: date) -> Decimal:
        """The contracted rate in force on `as_of`: the latest
        history period that has begun, the earliest period when the
        date precedes them all, and the headline rate when no
        history is recorded."""
        if not self.rate_history:
            return self.commission_rate
        begun = [p for p in self.rate_history if p.effective_from <= as_of]
        if begun:
            return max(begun, key=lambda p: p.effective_from).rate
        return min(self.rate_history, key=lambda p: p.effective_from).rate


class Installment(BaseModel):
    installment_id: str
    policy_number: str
    sequence: int  # 1-based within the policy
    due_date: date
    amount_due: Decimal
    amount_applied: Decimal = Decimal("0.00")
    status: InstallmentStatus = InstallmentStatus.OPEN
    # The currency amount_due is denominated in — the policy's
    # currency, carried on the installment so every open item names
    # its own denomination. Matching is in-currency: cash settles
    # only items denominated in the cash's own currency.
    currency: str = "USD"

    @property
    def open_balance(self) -> Decimal:
        return self.amount_due - self.amount_applied


class Receivable(BaseModel):
    """A billed policy: the thing cash is applied against."""

    policy_number: str
    insured_name: str
    broker_id: str | None  # None = direct-billed insured
    status: PolicyStatus = PolicyStatus.ACTIVE
    # The currency the policy is billed in (ISO code). A specialty
    # carrier bills in more than one currency; the receivable, its
    # installments, and the cash that settles them all carry the
    # denomination so no computation ever has to guess it.
    currency: str = "USD"
    installments: list[Installment] = Field(default_factory=list)
    # The current term's span, when the billing record carries it.
    # The return-premium computation stands on these two dates; a
    # policy whose record has neither cannot be cancelled in this
    # system, and the refusal says so.
    term_start: date | None = None
    term_end: date | None = None  # exclusive: the first day not covered

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
    # The currency the cash actually arrived in (ISO code). The
    # amount is denominated in this and nothing else: the matcher
    # works the line against open items in this currency only, and
    # any functional-currency figure is a recorded conversion, made
    # in code at the governed rate — never a match decision.
    currency: str = "USD"
    resend_of: str | None = None  # a returned line this payment re-sends, if any
    # Provenance: how the line arrived — "dataset" (the synthetic
    # world), "import" (a bank statement file), or "manual" (added
    # directly, the default for anything nobody stamped). An import
    # also carries its batch id, so the line traces to the exact
    # statement import that brought it in.
    source: str = "manual"
    import_batch: str | None = None


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
    # The denomination of `amount` — the bank line's currency, which
    # is also the settled item's currency (matching is in-currency).
    currency: str = "USD"
    # For a non-functional-currency allocation, the conversion made
    # in code when the allocation was created: the governed rate in
    # force on the bank line's value date (functional units per one
    # unit of the currency) and the resulting functional amount,
    # rounded half-up to cents. Both are recorded, never recomputed
    # later — a rate move, or a return reversing this allocation,
    # uses these figures, so history is never re-priced. None on
    # functional (USD) allocations, where amount IS the functional
    # amount.
    fx_rate: Decimal | None = None
    functional_amount: Decimal | None = None


class HandlingNote(BaseModel):
    """One dated handling note on an exception. Notes are appended,
    never edited or deleted — what the desk knew, and when, is part
    of the exception's record."""

    at: datetime
    by: str
    text: str


class ExceptionRecord(BaseModel):
    exception_id: str
    code: ExceptionCode
    bank_line_id: str | None
    policy_number: str | None
    broker_id: str | None
    amount: Decimal  # the amount at issue (delta, or the full line)
    currency: str = "USD"  # the currency `amount` is denominated in — the bank line's
    detail: str  # what happened, in words a cashier can act on
    first_seen: date
    status: str = "open"  # open | resolved | written_off
    proposed_by: str | None = None  # model/backend that proposed, if any
    # Handling metadata for the workbench: who has it, and the running
    # notes. Assignment and notes never change the amount, the code,
    # or the disposition — those move only through the governed
    # paths (the gate, a re-match, a recorded return).
    assignee: str | None = None
    handling_notes: list[HandlingNote] = Field(default_factory=list)


class SuspenseItem(BaseModel):
    """Unapplied cash held in suspense, with its aging clock."""

    suspense_id: str
    bank_line_id: str
    amount: Decimal
    received_date: date
    reason: str
    # The currency the parked cash is denominated in — the bank
    # line's. Suspense is held per currency: an EUR balance is never
    # added to a USD one anywhere in the reports.
    currency: str = "USD"
    status: str = "open"  # open | resolved (closed by the re-match pass)
    resolved_on: date | None = None

    def age_days(self, as_of: date) -> int:
        return (as_of - self.received_date).days


class EntryEdit(BaseModel):
    """One governed change to a proposed pack entry, made at the
    gate before approval: what the entry said, what it says now,
    and the approver who changed it. The original proposal is never
    lost — it is the first edit's "before", and it is what the
    proposal quality report grades the model against."""

    at: datetime
    by: str
    before_policy: str
    before_installment: str
    before_amount: Decimal
    after_policy: str
    after_installment: str
    after_amount: Decimal


class PayableOffset(BaseModel):
    """Cash from one bank line netted against a return-premium
    payable: the carrier owed the return premium back, the payer
    sent more cash for the same cancelled policy, and the two meet
    here. The offset is proposed by the matcher and lands on the
    payable only when its pack is approved, like every allocation."""

    bank_line_id: str
    payable_id: str
    policy_number: str
    amount: Decimal
    currency: str = "USD"  # the bank line's — offsets only ever meet a same-currency payable


class NonPremiumEntry(BaseModel):
    """One bank line classified as not premium cash at all — a bank
    fee rebate, an interest credit, a transfer between the carrier's
    own accounts. The line rides a disposition pack (an NPACK-*
    PostingPack) to the same human gate as everything else, and
    posts to the non-premium ledger view: it never touches a
    receivable and never enters suspense."""

    bank_line_id: str
    kind: str  # "BANK_FEE" | "INTEREST" | "OWN_TRANSFER" | "OTHER_NON_PREMIUM"
    amount: Decimal
    payer_name: str
    narrative: str = ""
    currency: str = "USD"  # the line's currency — a disposition disposes of the cash as it arrived


class PostingEntry(BaseModel):
    """One line of a posting pack: an allocation, or a delta write-off."""

    kind: str  # "allocation" | "write_off" | "payable_offset" | "non_premium"
    allocation: Allocation | None = None
    payable_offset: PayableOffset | None = None
    non_premium: NonPremiumEntry | None = None
    # Set on the allocation entries of a combination settlement:
    # two bank lines (the legs) whose cash together settles one
    # installment exactly. Entries sharing a combination id are
    # approved together or not at all, and each leg grosses up at
    # the commission rate in force on its own value date.
    combination_id: str | None = None
    write_off_policy: str | None = None
    write_off_amount: Decimal | None = None
    write_off_exception_id: str | None = None
    auto_applied: bool = False  # T1 exact at/below the policy threshold
    # Provenance for the quality report: set when the allocation
    # stands on a model proposal — "remittance_read" (the reader's
    # reading of an advice) or "lump_sum" (the proposer's subset) —
    # and the backend that proposed it. Code-matched tiers carry
    # neither: the model made no claim there, so there is nothing
    # to grade.
    proposal_kind: str | None = None
    proposed_by: str | None = None
    edits: list[EntryEdit] = Field(default_factory=list)


class ResolutionAllocation(BaseModel):
    """One installment's share of a resolution candidate: the cash
    the candidate would apply there if the gate chose it. The
    conversion figures are stamped when the candidate is scanned,
    at the governed rate in force on the bank line's value date —
    the same recording rule as the matcher's allocations — so a
    candidate approved days later applies the figures it was
    proposed at, never a re-priced version."""

    policy_number: str
    installment_id: str
    amount: Decimal
    fx_rate: Decimal | None = None
    functional_amount: Decimal | None = None


class ResolutionCandidate(BaseModel):
    """One defensible reading of an unidentified wire, found by the
    resolution scan (resolution.py). A candidate exists only when
    it carries exact arithmetic (an amount interpretation that
    foots to the cent) or verbatim evidence from the line's own
    text — usually both. A candidate with neither is not a
    candidate and never reaches the record.

    A flagged candidate names a policy the evidence points at
    whose cash cannot be applied (cancelled, or nothing open):
    it stands on the record as considered-and-flagged, and the
    gate can never choose it."""

    candidate_id: str  # "C1", "C2", ... within one scan
    policy_number: str
    broker_id: str | None  # the policy's broker of record, if any
    interpretation: str  # "exact" | "netting" | "installment_run" | "narrative"
    allocations: list[ResolutionAllocation] = Field(default_factory=list)
    arithmetic: str = ""  # the interpretation, shown: figures and rates named
    evidence: str = ""  # verbatim text from the bank line, when evidence carries it
    evidence_kind: str = ""  # "policy_number" | "insured_name" | "broker_name" | "payer_name"
    proposed_by: str = "resolution_scan"  # the scan, or the model backend that added it
    flags: list[str] = Field(default_factory=list)  # "CANCELLED_POLICY" | "NO_OPEN_BALANCE"
    model_rank: int | None = None  # the model's ordering, when a model ranked the scan
    model_explanation: str = ""  # the model's one line on this candidate, when it gave one

    @property
    def total(self) -> Decimal:
        return sum((a.amount for a in self.allocations), Decimal("0.00"))


class ResolutionInfo(BaseModel):
    """The resolution docket carried on a PACK-RESOLVE pack: the
    suspense item the pack decides, the candidate set the scan
    produced (the alternates), and — after approval — the one
    candidate the approver chose. The unchosen candidates lapse
    back to this record; they are never deleted, because "what
    else this cash could have been" is part of what the gate
    decided."""

    suspense_id: str
    bank_line_id: str
    amount: Decimal  # the suspense item's cash — every candidate sums to it
    currency: str = "USD"
    scan_backend: str = "resolution_scan"  # who scanned; "…+model:<backend>" when a model ranked
    candidates: list[ResolutionCandidate] = Field(default_factory=list)
    chosen_candidate: str | None = None


class PostingPack(BaseModel):
    pack_id: str
    value_date: date
    assembled_by: str
    entries: list[PostingEntry] = Field(default_factory=list)
    status: PackStatus = PackStatus.PROPOSED
    approvals: list[str] = Field(default_factory=list)  # approver names
    decided_at: datetime | None = None
    # Markers about the pack, never edits to it: a flag (e.g.
    # RETURN_AFFECTED) annotates the record; the entries — the body
    # a human approved — are never rewritten after the fact.
    flags: list[str] = Field(default_factory=list)
    # Set on a resolution pack (PACK-RESOLVE-*): the docket of
    # alternates for one unidentified suspense item. Such a pack
    # carries NO entries until the gate approves it — approval
    # names the chosen candidate and materialises exactly that
    # candidate's allocations as the pack's entries, so every
    # figure computed from entries (posting file, day totals,
    # the returns replay) sees the one settlement that happened,
    # never the sum of the roads not taken.
    resolution: ResolutionInfo | None = None

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

    @property
    def offset_total(self) -> Decimal:
        return sum(
            (e.payable_offset.amount for e in self.entries if e.payable_offset),
            Decimal("0.00"),
        )

    @property
    def non_premium_total(self) -> Decimal:
        return sum(
            (e.non_premium.amount for e in self.entries if e.non_premium),
            Decimal("0.00"),
        )


class ReturnUnwind(BaseModel):
    """One allocation's reversal inside a payment return: the cash
    taken back from that allocation and the gross (billed) amount
    it re-opened. Recorded per allocation, so a later return of
    the same line knows exactly what application still stands —
    the unwind is replayed from the gate's own arithmetic, never
    re-priced at a later rate."""

    pack_id: str
    policy_number: str
    installment_id: str
    cash: Decimal
    gross: Decimal


class PaymentReturn(BaseModel):
    """A bank return advice worked against an applied bank line.

    The bank took money back (a bounced debit, a recalled wire, a
    dishonoured cheque). The return is its own record — the
    original application is never edited away; the reversal is
    derived from it and chained to it here and on the audit trail.

    A return may be for part of the line — banks claw back what
    they claw back, and the rest of the application stands. The
    returned amount unwinds the line's cash in a fixed order:
    suspense the line still holds first (cash that never settled
    anything), then its allocations in the reverse of the order
    they were applied, each to the cent. A line can be returned
    more than once, until its returns sum to the line amount —
    and never beyond it; the excess is refused at recording time,
    in words.
    """

    return_id: str
    bank_line_id: str
    reason: ReturnReason
    return_date: date
    recorded_by: str
    amount: Decimal  # what this return took back (the full line when partial is False and it is the line's only return)
    currency: str = "USD"  # the original line's currency — the reversal is in that currency, at its recorded rates
    pack_ids: list[str] = Field(default_factory=list)  # approved packs that carried the line
    detail: str = ""
    # Part-return bookkeeping. Defaults keep records written
    # before part-returns existed valid and read exactly as they
    # were read then: a legacy record is a full return with no
    # unwind list, and suspense_released None means "the whole
    # remaining suspense position" — the close pack reconstructs
    # it that way, never from a stored guess.
    partial: bool = False  # this return left some of the line standing
    cumulative_returned: Decimal | None = None  # the line's returns including this one; None on legacy records
    suspense_released: Decimal | None = None  # this return's suspense share; None on legacy records
    unwind: list[ReturnUnwind] = Field(default_factory=list)


class DayClaim(BaseModel):
    """The store-level claim on one day's run: the atomic record
    that makes "a day runs once" true across processes, where the
    audit-trail guard alone was read-then-write. The first starter
    inserts the claim and wins it; a second starter — even while
    the first is still running — finds the claim standing and is
    refused, naming it. A completed run's claim is the day's
    standing record. An interrupted attempt's claim is
    reclaimable only by the sanctioned recovery run, which proves
    the prior attempt interrupted on the trail, and no longer
    holding the day's run lock, before it takes the claim over."""

    day: date
    attempt: int
    actor: str
    claimed_at: datetime
    state: str = "running"  # running | completed
    thread_id: str = ""


class Cancellation(BaseModel):
    """A policy cancelled mid-term, with the return-premium
    computation shown line by line on the record.

    Earned premium is pro-rata by days over the term; paid to date
    is what the installments say was applied, on the billed basis.
    The difference, when the insured has paid ahead of what was
    earned, is the return premium — money the carrier owes back,
    carried as a ReturnPremiumPayable. When the insured is behind,
    the record shows what they owe and no payable opens."""

    cancellation_id: str
    policy_number: str
    effective_date: date
    recorded_on: date
    recorded_by: str
    reason: str = ""
    premium_total: Decimal  # billed premium, all installments
    term_days: int
    elapsed_days: int  # term days earned at the effective date
    earned_premium: Decimal
    paid_to_date: Decimal
    return_premium: Decimal  # max(0, paid − earned)
    insured_owes: Decimal  # max(0, earned − paid); a receivable fact, no payable
    payee_kind: str  # "insured" | "broker" — who the return is owed to
    payee_name: str
    broker_id: str | None
    payable_id: str | None  # None when no return premium is due
    computation: list[str] = Field(default_factory=list)


class PayableEvent(BaseModel):
    """One movement on a payable's chain: opened, netted against an
    incoming bank line, or paid out under an approved payout pack.
    The chain is the payable's history, readable in order."""

    on: date
    kind: str  # "opened" | "netted" | "paid"
    amount: Decimal
    ref: str  # cancellation id / bank line id / payout pack id
    note: str = ""


class ReturnPremiumPayable(BaseModel):
    """Money the carrier owes back on a cancelled policy.

    Opened by a cancellation, reduced two ways only: netted against
    later cash for the same policy (through a posting pack at the
    gate) or paid out under an approved payout pack. It is a ledger
    of its own — it never enters the bank identity; the close pack
    rolls it separately and says so."""

    payable_id: str
    policy_number: str
    cancellation_id: str
    payee_kind: str  # "insured" | "broker"
    payee_name: str
    broker_id: str | None
    amount: Decimal  # the return premium as computed at cancellation
    amount_netted: Decimal = Decimal("0.00")
    amount_paid: Decimal = Decimal("0.00")
    status: str = "open"  # open | settled (netted to zero) | paid
    opened_on: date
    events: list[PayableEvent] = Field(default_factory=list)

    @property
    def remaining(self) -> Decimal:
        return self.amount - self.amount_netted - self.amount_paid


class PayoutEntry(BaseModel):
    """One line of a payout pack: a payable paid in full, at the
    amount standing when the pack was assembled."""

    payable_id: str
    policy_number: str
    payee_kind: str
    payee_name: str
    broker_id: str | None
    amount: Decimal


class PayoutPack(BaseModel):
    """Proposed return-premium payouts, decided at the same human
    gate as posting packs: assembled by a named person, approved by
    a different one, a second approver above the single-approver
    limit. Approval produces a payout file; this system pays
    nothing itself."""

    pack_id: str
    value_date: date
    assembled_by: str
    entries: list[PayoutEntry] = Field(default_factory=list)
    status: PackStatus = PackStatus.PROPOSED
    approvals: list[str] = Field(default_factory=list)
    decided_at: datetime | None = None

    @property
    def total(self) -> Decimal:
        return sum((e.amount for e in self.entries), Decimal("0.00"))


class BrokerStatementLine(BaseModel):
    """One line of a broker's own monthly statement: what the
    broker says it billed, what commission it says it took, and
    what net it says it remitted, under its own remittance
    reference. The broker's figures are claims, not postings —
    the reconciliation reads them against the books and classifies
    every disagreement; nothing here ever edits a book record."""

    policy_number: str
    gross: Decimal  # gross billed, as the broker states it
    commission: Decimal  # the commission the broker says it took
    net: Decimal  # net remitted, as the broker states it
    reference: str = ""  # the broker's own remittance reference
    currency: str = "USD"


class BrokerStatement(BaseModel):
    """A broker statement as imported: one broker, one period,
    held as a record of its own so the reconciliation can cite
    exactly which statement it read. A corrected statement for the
    same broker and period supersedes the standing one — the old
    record stays, marked with what replaced it, and only the
    standing statement is reconciled."""

    statement_id: str  # BSTMT-<digest of the file text>
    broker_id: str
    period: str  # "YYYY-MM"
    source_name: str = ""
    superseded_by: str | None = None
    control_total: Decimal | None = None  # the file's trailer, as stated
    control_count: int | None = None
    lines: list[BrokerStatementLine] = Field(default_factory=list)

    @property
    def net_total(self) -> Decimal:
        return sum((l.net for l in self.lines), Decimal("0.00"))


class AuditEvent(BaseModel):
    at: datetime
    actor: str  # a person, "matcher", "reader:<backend>", "gate", ...
    action: str
    subject: str  # bank line / pack / exception id
    detail: str = ""
