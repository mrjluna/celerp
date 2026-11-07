# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

import re
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

from celerp.models.company import Company
from celerp.services.business_time import business_today

_PREFIX_BY_DOC_TYPE = {
    "invoice": "INV",
    "proforma": "PF",
    "purchase_order": "PO",
    "quotation": "QUO",
    "shipping_doc": "SHIP",
    "credit_note": "CN",
    "debit_note": "SCN",
    "bill": "BILL",
    "memo": "MEMO",
    "list": "LST",
    "transfer": "TRF",
    "consignment_in": "CI",
    "subscription_invoice": "SUBI",
    "subscription_po": "SUBP",
    "production_order": "PRD",
    "audit": "AUD",
    "writeoff": "WO",
}


# Every list type has its own numbering counter. The shared "list" counter (LST) is no longer
# drawn from; it stays in the table so a company's old settings and numbers are still read.
_LIST_PREFIX_KEYS = {"quotation", "transfer", "audit", "writeoff", "shipping_doc"}
# The types the shared counter numbered. Audit, write-off and shipping always had their own.
_SHARED_COUNTER_KEYS = {"quotation", "transfer"}


def list_sequence_key(list_type: str | None, settings: dict | None = None) -> str:
    """Keep an existing shared counter for the types it numbered until the type has its own
    configuration."""
    key = list_type if list_type in _LIST_PREFIX_KEYS else "quotation"
    sequences = (settings or {}).get("sequences") or {}
    if key in _SHARED_COUNTER_KEYS and sequences.get("list") and not sequences.get(key):
        return "list"
    return key


# Default pattern for all new companies
DEFAULT_PATTERN = "{PREFIX}-{YY}{MM}-{####}"

# Ownership of the newly exposed settings; counters remain in the canonical registry above.
NUMBERING_MODULES = {
    "production_order": "celerp-manufacturing",
    "subscription_invoice": "celerp-subscriptions",
    "subscription_po": "celerp-subscriptions",
}
GUARDED_NUMBERING_TYPES = frozenset({"debit_note", *NUMBERING_MODULES})


def _expand_pattern(pattern: str, prefix: str, day: date, seq_num: int) -> str:
    """Expand a numbering pattern into a concrete document reference."""
    # Count # chars to determine padding width
    match = re.search(r"\{(#+)\}", pattern)
    pad = len(match.group(1)) if match else 2

    result = pattern
    result = result.replace("{PREFIX}", prefix)
    result = result.replace("{YYYY}", str(day.year))
    result = result.replace("{YY}", f"{day.year % 100:02d}")
    result = result.replace("{MM}", f"{day.month:02d}")
    result = result.replace("{DD}", f"{day.day:02d}")
    # Replace the {##...} token with zero-padded number
    result = re.sub(r"\{#+\}", f"{seq_num:0{pad}d}", result)
    return result


def _period(pattern: str, day: date) -> tuple:
    """The numbering period ``day`` falls in, as far as the pattern can tell periods apart.

    No year token: one period forever. A month needs the year and a day needs the month,
    so {DD} without {MM} counts through the year and {MM} without a year never resets."""
    if "{YYYY}" not in pattern and "{YY}" not in pattern:
        return ()
    if "{MM}" not in pattern:
        return (day.year,)
    if "{DD}" not in pattern:
        return (day.year, day.month)
    return (day.year, day.month, day.day)


def _stored_day(seq: dict) -> date | None:
    """The day the counter last numbered a document, or None when it never has."""
    try:
        year = int(seq.get("year") or 0)
        return date(year, int(seq.get("month") or 1), int(seq.get("day") or 1)) if year else None
    except (TypeError, ValueError):
        return None


def _numbering_day(settings: dict, seq: dict) -> date:
    """The company's business day, never earlier than the day the counter last used, so a
    timezone change cannot move numbering back into a finished period."""
    today = business_today(settings.get("timezone"))
    stored = _stored_day(seq)
    return max(today, stored) if stored else today


def _next_number(seq: dict, pattern: str, day: date) -> int:
    """The number the next document on ``day`` takes: 1 once ``day`` is in a later period
    than the last numbered document, otherwise the saved next number."""
    stored = _stored_day(seq)
    if stored and _period(pattern, day) > _period(pattern, stored):
        return 1
    return int(seq.get("next") or 1)


def validate_pattern(pattern: str) -> str | None:
    """Return error message if pattern is invalid, None if OK."""
    if not re.search(r"\{#+\}", pattern):
        return "Pattern must contain a sequence token like {##} or {####}"
    # Check for unknown tokens
    cleaned = re.sub(r"\{(PREFIX|YYYY|YY|MM|DD|#+)\}", "", pattern)
    unknown = re.findall(r"\{[^}]+\}", cleaned)
    if unknown:
        return f"Unknown tokens: {', '.join(unknown)}"
    if "{" in cleaned or "}" in cleaned:
        return "Pattern contains an incomplete or empty token"
    return None


def require_doc_type(doc_type: str) -> str:
    """Return ``doc_type`` when it is a document type this app numbers, else refuse it by name."""
    if doc_type not in _PREFIX_BY_DOC_TYPE:
        raise ValueError(
            f"{doc_type!r} is not a document type. Use one of: {', '.join(sorted(_PREFIX_BY_DOC_TYPE))}.")
    return doc_type


def next_doc_ref(company: Company, doc_type: str) -> str:
    """Generate the next document reference and increment the sequence counter.

    ``company`` must come from locked_company(), taken before any document row lock;
    a counter change on any other company object is refused at flush."""
    require_doc_type(doc_type)

    settings = dict(company.settings or {})
    sequences = dict(settings.get("sequences") or {})
    seq = dict(sequences.get(doc_type) or {})

    prefix = str(seq.get("prefix") or _PREFIX_BY_DOC_TYPE[doc_type])
    pattern = str(seq.get("pattern") or DEFAULT_PATTERN)

    day = _numbering_day(settings, seq)
    next_num = _next_number(seq, pattern, day)
    ref = _expand_pattern(pattern, prefix, day, next_num)

    if doc_type in GUARDED_NUMBERING_TYPES:
        # Validation metadata in the existing sequence, independent of its displayed format.
        # Keep the highest reserved number even if a direct repair lowers the counter.
        issued = seq.get("last_issued") or {}
        try:
            issued_day = date.fromisoformat(issued["date"])
        except (KeyError, TypeError, ValueError):
            issued_day = None
        previous = int(issued.get("number") or 0) if issued_day and _period(pattern, issued_day) == _period(pattern, day) else 0
        seq["last_issued"] = {"number": max(previous, next_num), "date": day.isoformat()}

    # Update sequence state
    seq["next"] = next_num + 1
    seq["year"] = day.year
    seq["month"] = day.month
    seq["day"] = day.day
    seq["prefix"] = prefix
    seq["pattern"] = pattern
    sequences[doc_type] = seq
    settings["sequences"] = sequences
    company.settings = settings

    return ref


def _draft_counter(doc_type: str) -> str:
    """The counter a new draft of ``doc_type`` is numbered from. A draft invoice is numbered
    from the proforma counter; finalize gives it its invoice number, so no invoice number goes
    unused."""
    return "proforma" if doc_type == "invoice" else doc_type


def next_draft_ref(company: Company, doc_type: str) -> str:
    """The reference a new draft of ``doc_type`` takes."""
    return next_doc_ref(company, _draft_counter(doc_type))


def peek_draft_ref(company: Company, doc_type: str) -> str:
    """The reference the next new draft of ``doc_type`` would take, without taking it."""
    settings = dict(company.settings or {})
    counter = _draft_counter(doc_type)
    return _sequence_view(settings, counter, dict((settings.get("sequences") or {}).get(counter) or {}))["preview"]


def get_all_sequences(company: Company) -> list[dict]:
    """Return the numbering config for all doc types."""
    settings = dict(company.settings or {})
    sequences = dict(settings.get("sequences") or {})
    return [
        _sequence_view(
            settings, doc_type,
            dict(sequences.get(
                list_sequence_key(doc_type, settings) if doc_type in _LIST_PREFIX_KEYS else doc_type
            ) or {}),
        )
        for doc_type in _PREFIX_BY_DOC_TYPE
    ]


def _sequence_view(settings: dict, doc_type: str, seq: dict) -> dict:
    """A counter as the Numbering tab shows it; the preview is the reference the next
    document of this type gets."""
    prefix = str(seq.get("prefix") or _PREFIX_BY_DOC_TYPE[doc_type])
    pattern = str(seq.get("pattern") or DEFAULT_PATTERN)
    day = _numbering_day(settings, seq)
    return {
        "doc_type": doc_type,
        "prefix": prefix,
        "pattern": pattern,
        "next": int(seq.get("next") or 1),
        "preview": _expand_pattern(pattern, prefix, day, _next_number(seq, pattern, day)),
    }


def update_sequence(company: Company, doc_type: str, prefix: str | None = None,
                    pattern: str | None = None, next_num: int | None = None) -> dict:
    """Update numbering config for a single doc type. Returns the updated sequence.

    ``company`` must come from locked_company(), as for next_doc_ref()."""
    require_doc_type(doc_type)

    settings = dict(company.settings or {})
    sequences = dict(settings.get("sequences") or {})
    inherited = (
        sequences.get("list")
        if doc_type in _LIST_PREFIX_KEYS and not sequences.get(doc_type)
        else None
    )
    seq = dict(sequences.get(doc_type) or inherited or {})
    if inherited is not None:
        # A split counter must not mint the same references as the shared one.
        seq["prefix"] = _PREFIX_BY_DOC_TYPE[doc_type]

    if prefix is not None:
        if not prefix or re.search(r"[\s/\\{}]", prefix):
            raise ValueError("Prefix must not be empty or contain spaces, slashes or braces")
        seq["prefix"] = prefix
    if pattern is not None:
        err = validate_pattern(pattern)
        if err:
            raise ValueError(err)
        seq["pattern"] = pattern
    if next_num is not None:
        # The number belongs to today's period, so the reset for a later period cannot discard it.
        day = _numbering_day(settings, seq)
        seq.update(next=max(1, next_num), year=day.year, month=day.month, day=day.day)

    if doc_type in _LIST_PREFIX_KEYS:
        def namespace(kind: str, row: dict) -> str:
            fmt = str(row.get("pattern") or DEFAULT_PATTERN)
            name = str(row.get("prefix") or _PREFIX_BY_DOC_TYPE[kind])
            return fmt.replace("{PREFIX}", name)

        target = namespace(doc_type, seq)
        for kind in sorted({"list", *_LIST_PREFIX_KEYS} - {doc_type}):
            other = sequences.get(kind)
            if other and target == namespace(kind, other):
                raise ValueError(f"Numbering format overlaps the active {kind} counter. "
                                 "Choose a distinct prefix or pattern.")

    sequences[doc_type] = seq
    settings["sequences"] = sequences
    company.settings = settings

    return _sequence_view(settings, doc_type, seq)


def _issued_number_pattern(settings: dict, doc_type: str) -> re.Pattern:
    """Recognize this type's existing series in its current numbering period."""
    seq = (settings.get("sequences") or {}).get(doc_type) or {}
    prefix = str(seq.get("prefix") or _PREFIX_BY_DOC_TYPE[doc_type])
    pattern = str(seq.get("pattern") or DEFAULT_PATTERN)
    day = _numbering_day(settings, seq)
    period = _period(pattern, day)
    dates = {"YYYY": str(day.year), "YY": f"{day.year % 100:02d}",
             "MM": f"{day.month:02d}", "DD": f"{day.day:02d}"}
    out, pos, numbered = [], 0, False
    for match in re.finditer(r"\{(PREFIX|YYYY|YY|MM|DD|#+)\}", pattern):
        out.append(re.escape(pattern[pos:match.start()]))
        token = match[1]
        if token == "PREFIX":
            out.append(re.escape(prefix))
        elif token.startswith("#"):
            out.append(r"(?P<number>\d+)" if not numbered else r"(?P=number)")
            numbered = True
        else:
            level = {"YYYY": 1, "YY": 1, "MM": 2, "DD": 3}[token]
            out.append(re.escape(dates[token]) if len(period) >= level else r"\d{2}")
        pos = match.end()
    out.append(re.escape(pattern[pos:]))
    return re.compile("^" + "".join(out) + "$")


async def update_sequence_checked(session, company: Company, doc_type: str, **changes) -> dict:
    """Validate a settings save under the caller's company lock, before changing anything.

    Existing Sales/List reset behavior is retained. The four newly exposed types refuse a
    rewind past a number already held by a record in the active period, including voids
    and drafts whose references are already reserved. A later period can still start at one.
    """
    from sqlalchemy import select
    from celerp.models.projections import Projection
    from celerp.modules.boundary import require_on

    require_doc_type(doc_type)
    if module := NUMBERING_MODULES.get(doc_type):
        require_on(company.settings, module)
    next_num = changes.get("next_num")
    if next_num is not None and (isinstance(next_num, bool) or next_num < 1):
        raise ValueError("Next number must be a positive whole number")
    proposed = SimpleNamespace(settings=deepcopy(company.settings or {}))
    result = update_sequence(proposed, doc_type, **changes)
    if doc_type in GUARDED_NUMBERING_TYPES:
        matcher = _issued_number_pattern(company.settings or {}, doc_type)
        numbers = (await session.execute(select(
            Projection.state["doc_number"].as_string(), Projection.state["ref_id"].as_string(),
        ).where(
            Projection.company_id == company.id, Projection.entity_type == "doc",
            Projection.state["doc_type"].as_string() == doc_type,
        ))).all()
        seq = ((company.settings or {}).get("sequences") or {}).get(doc_type) or {}
        pattern = str(seq.get("pattern") or DEFAULT_PATTERN)
        issued = seq.get("last_issued") or {}
        try:
            issued_day = date.fromisoformat(issued["date"])
        except (KeyError, TypeError, ValueError):
            issued_day = None
        highest = (int(issued.get("number") or 0) if issued_day and
                   _period(pattern, issued_day) == _period(pattern, _numbering_day(company.settings or {}, seq)) else 0)
        for row in numbers:
            for number in row:
                if match := matcher.fullmatch(str(number or "")):
                    highest = max(highest, int(match["number"]))
        if next_num is not None and next_num <= highest:
            raise ValueError(f"Next number must be at least {highest + 1}; "
                             f"number {highest} is already issued for this type")
        if highest:
            # Capture older records before a format edit makes their references unrecognizable.
            proposed.settings["sequences"][doc_type]["last_issued"] = {
                "number": highest,
                "date": _numbering_day(company.settings or {}, seq).isoformat(),
            }
    if proposed.settings != (company.settings or {}):
        company.settings = proposed.settings
    return result


async def next_available_doc_ref(session, company: Company, doc_type: str) -> str:
    """Draw the existing counter until no document or List holds the reference.

    The caller holds locked_company() through the record write and commit. There is no
    independent allocator or reservation table; next_doc_ref owns the same sequence state.
    This also covers old/imported references after a direct counter repair or reset.
    """
    from sqlalchemy import or_, select
    from celerp.models.projections import Projection

    while True:
        ref = next_doc_ref(company, doc_type)
        occupied = await session.scalar(select(Projection.entity_id).where(
            Projection.company_id == company.id,
            or_(Projection.entity_id.in_((f"doc:{ref}", f"list:{ref}")),
                Projection.state["ref_id"].as_string() == ref,
                Projection.state["doc_number"].as_string() == ref),
        ).limit(1))
        if occupied is None:
            return ref
