# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Journal-entry anomaly detection.

The deterministic default flags moves on rule-based heuristics that
match the patterns auditors most commonly investigate:

* Round-number outlier: amount is an exact thousand multiple AND
  is more than 3x the same-journal calendar-month population median.
* Non-working-day post: the server-stamped posting instant falls on one of
  the posting company's configured weekend days in the posting user's
  timezone (not merely the accounting date).
* Just-under-threshold: amount within 5% below a configurable
  approval threshold (suggests structuring to evade approval).
* Reversal pair: exact opposing signed account vectors posted by the same
  user/journal/period, excluding Odoo's linked reversal workflow.

The LLM hook (when a non-manual provider is configured) calls
chat() with the deterministic flags as input and asks for a
prioritised summary; the LLM output augments rather than replaces
the deterministic findings, so a missing or failing provider never
causes a capability call to error out.

The detector returns a list of Finding records:

    Finding(
        move_id, severity, rule, description, amount, date
    )

Callers (a dashboard tile, a close-workflow validator, a chatter-
posting cron) consume the list however they want.
"""

import dataclasses
import datetime
import hashlib
import statistics
import unicodedata
from typing import Iterable, List, Optional

from .provider_registry import (
    get_provider, has_provider, ProviderError,
)
from .llm_parsing import extract_json_array


@dataclasses.dataclass
class JournalEntrySummary:
    """Plain transport record describing one journal entry.

    The detector consumes these summaries rather than account.move
    records so it stays Odoo-independent and unit-testable. Callers
    inside Odoo build summaries from account.move; tests can build
    them from literals.
    """
    id: int
    date: datetime.date
    amount: float                 # absolute amount, signed positive
    posted_by: str = ''           # user login or display name
    posted_at: Optional[datetime.datetime] = None
    journal_id: int = 0
    account_codes: tuple = ()     # accounts touched
    account_balances: tuple = ()  # (account id/code, signed balance)
    currency_decimals: int = 2
    reversed_entry_id: int = 0    # core account.move reversal link
    baseline_median: float = 0.0  # journal/calendar-month population median
    reference: str = ''


@dataclasses.dataclass
class Finding:
    move_id: int
    severity: str                 # 'low' | 'medium' | 'high'
    rule: str                     # rule key
    description: str              # human-readable
    amount: float
    date: datetime.date
    source: str = 'deterministic'
    provider_key: str = ''
    provider_evidence_hash: str = ''


_SEVERITY_BY_RULE = {
    'round_outlier_v2': 'medium',
    'weekend_post_v2': 'low',
    'just_under_threshold': 'high',
    'reversal_pair_v2': 'medium',
}

MAX_PROVIDER_DESCRIPTION_CHARS = 1000


def detect(
    entries: Iterable[JournalEntrySummary],
    approval_threshold: float = 0.0,
    median_multiplier: float = 3.0,
    provider_key: str = 'manual',
    provider_config: Optional[dict] = None,
    weekend_days=(5, 6),
) -> List[Finding]:
    """Run the deterministic anomaly rules; optionally augment via LLM.

    :param entries: iterable of JournalEntrySummary records.
    :param approval_threshold: configured approval threshold (0.0
        disables the just-under-threshold rule).
    :param median_multiplier: factor over the period median to
        trigger round-outlier (default 3.0).
    :param provider_key: registered provider key. 'manual' (or
        any provider whose `is_manual` attribute is True) skips
        the LLM call and returns the deterministic findings only.
    :param provider_config: dict / JSON string handed to the
        provider factory. Ignored when provider is manual.
    :return: list of Finding records.
    """
    entries = list(entries)
    findings = []
    findings.extend(_round_outlier(entries, median_multiplier))
    findings.extend(_weekend_post(entries, weekend_days))
    if approval_threshold > 0:
        findings.extend(
            _just_under_threshold(entries, approval_threshold),
        )
    findings.extend(_reversal_pair(entries))
    # Pure callers keep the historical fail-soft behaviour. The live Odoo
    # wiring calls augment_with_provider itself so it can put authorization,
    # request bounds and a durable cache in front of every paid invocation.
    try:
        augment_with_provider(
            entries, findings, provider_key=provider_key,
            provider_config=provider_config,
        )
    except ProviderError:
        pass
    return findings


def augment_with_provider(
    entries: Iterable[JournalEntrySummary],
    findings: List[Finding],
    provider_key: str = 'manual',
    provider_config: Optional[dict] = None,
) -> bool:
    """Augment ``findings`` and report whether a live provider was invoked.

    Unlike :func:`detect`, provider errors deliberately propagate. This gives
    the Odoo caller an unambiguous rule for caching: only a completed live call
    (including a valid empty answer) is cached; failed or unavailable providers
    remain retryable.
    """
    entries = list(entries)
    if not provider_key or provider_key == 'manual' \
            or not has_provider(provider_key):
        return False
    provider = get_provider(provider_key, provider_config)
    if getattr(provider, 'is_manual', False):
        return False
    _augment_via_llm(
        provider, entries, findings, provider_key=provider_key,
    )
    return True


def _round_outlier(entries, multiplier):
    if not entries:
        return []
    # The live caller supplies the stable population median for each entry's
    # journal/calendar month. Pure callers get the same scope computed from
    # their complete input, never one selection-wide median across journals.
    grouped_amounts = {}
    for entry in entries:
        if entry.amount <= 0:
            continue
        period = (
            entry.date.year, entry.date.month,
        ) if entry.date else (0, 0)
        grouped_amounts.setdefault(
            (entry.journal_id, period), [],
        ).append(entry.amount)
    out = []
    for e in entries:
        if e.amount <= 0:
            continue
        period = (e.date.year, e.date.month) if e.date else (0, 0)
        population = grouped_amounts.get((e.journal_id, period), ())
        if e.baseline_median > 0:
            median = e.baseline_median
        elif len(population) >= 3:
            median = statistics.median(population)
        else:
            continue
        if e.amount % 1000 != 0:
            continue
        if e.amount < median * multiplier:
            continue
        out.append(Finding(
            move_id=e.id, severity=_SEVERITY_BY_RULE['round_outlier_v2'],
            rule='round_outlier_v2',
            description=(
                "Round-number entry of %.2f is more than %dx the "
                "same-journal calendar-month median of %.2f."
                % (e.amount, int(multiplier), median)
            ),
            amount=e.amount, date=e.date,
        ))
    return out


def _weekend_post(entries, weekend_days=(5, 6)):
    weekend_days = {
        int(day) for day in (weekend_days or ())
        if isinstance(day, int) or str(day).isdigit()
        if 0 <= int(day) <= 6
    }
    out = []
    for e in entries:
        if e.posted_at and e.posted_at.weekday() in weekend_days:
            out.append(Finding(
                move_id=e.id,
                severity=_SEVERITY_BY_RULE['weekend_post_v2'],
                rule='weekend_post_v2',
                description=(
                    "Posted at %s (%s); review whether out-of-"
                    "hours posting is policy-compliant."
                    % (e.posted_at, e.posted_at.strftime('%A'))
                ),
                amount=e.amount, date=e.date,
            ))
    return out


def _just_under_threshold(entries, threshold):
    out = []
    band_low = threshold * 0.95
    for e in entries:
        if band_low <= e.amount < threshold:
            out.append(Finding(
                move_id=e.id,
                severity=_SEVERITY_BY_RULE['just_under_threshold'],
                rule='just_under_threshold',
                description=(
                    "Amount %.2f is within 5%% below the approval "
                    "threshold %.2f. Possible structuring to evade "
                    "approval; verify no related entries make up "
                    "a single split transaction."
                    % (e.amount, threshold)
                ),
                amount=e.amount, date=e.date,
            ))
    return out


def _reversal_pair(entries):
    """Flag only exact, manually keyed opposing vectors in one period."""
    def vector(entry):
        decimals = max(0, int(entry.currency_decimals or 0))
        return tuple(sorted(
            (str(account), round(float(balance), decimals))
            for account, balance in entry.account_balances
            if round(float(balance), decimals)
        ))

    def inverse(values):
        return tuple(sorted((account, -balance) for account, balance in values))

    candidates = [
        entry for entry in entries
        if entry.posted_by and entry.account_balances and entry.date
    ]
    out = []
    matched = set()
    for index, left in enumerate(candidates):
        if left.id in matched:
            continue
        left_vector = vector(left)
        for right in candidates[index + 1:]:
            if right.id in matched:
                continue
            if left.posted_by != right.posted_by:
                continue
            if left.journal_id != right.journal_id:
                continue
            if (left.date.year, left.date.month) != (
                right.date.year, right.date.month,
            ):
                continue
            if left.reversed_entry_id == right.id \
                    or right.reversed_entry_id == left.id:
                continue
            if vector(right) != inverse(left_vector):
                continue
            matched.update((left.id, right.id))
            # Match one-to-one.  One vector plus two identical opposites
            # evidences one pair, not a three-entry reversal cluster.
            break
    for entry in candidates:
        if entry.id not in matched:
            continue
        out.append(Finding(
            move_id=entry.id,
            severity=_SEVERITY_BY_RULE['reversal_pair_v2'],
            rule='reversal_pair_v2',
            description=(
                "Same user posted an exact opposing account-balance vector "
                "in the same journal and calendar month; review the pair "
                "because it is not linked through Odoo's reversal workflow."
            ),
            amount=entry.amount, date=entry.date,
        ))
    return out


def _augment_via_llm(provider, entries, findings, provider_key=''):
    """Call the provider with the deterministic findings so it can
    add prioritisation or detect patterns the rules miss.

    Stub providers raise ProviderError, which the caller swallows.
    Real provider implementations parse the response and append
    additional Finding records to the list in place.
    """
    if not entries:
        return
    # Run even when the deterministic rules found nothing: the whole
    # point of LLM augmentation is to catch patterns the rules miss.
    entry_summary = "\n".join(
        "- move_id=%s date=%s amount=%.2f posted_by=%s accounts=%s "
        "reference=%s" % (
            entry.id, entry.date, entry.amount,
            entry.posted_by or 'unknown',
            ','.join(entry.account_codes) or 'unknown',
            entry.reference or '',
        )
        for entry in entries[:200]
    )
    if findings:
        summary = "\n".join(
            "- move_id=%s severity=%s date=%s rule=%s description=%s" % (
                f.move_id, f.severity, f.date, f.rule, f.description,
            )
            for f in findings[:50]
        )
    else:
        summary = "No rule-based flags were raised."
    messages = [
        {
            'role': 'system',
            'content': (
                "You are an accounting auditor. Given a list of "
                "journal-entry summaries and rule-based flags, identify "
                "only additional anomalies missed by the rules. Return ONLY "
                "a JSON array. Each object must contain move_id (one of the "
                "supplied ids), severity (low, medium, or high), and a terse "
                "description. Return [] when no extra anomaly is supported."
            ),
        },
        {
            'role': 'user',
            'content': (
                "Journal entries:\n%s\n\nExisting rule findings:\n%s"
                % (entry_summary, summary)
            ),
        },
    ]
    # Real provider returns a JSON array of extra findings we parse and
    # append. Stubs raise ProviderError; the caller handles it. We only
    # accept findings that reference a move_id actually in this batch,
    # so a hallucinated entry cannot invent a finding against a move
    # that was never scanned. All LLM-sourced findings share the
    # 'llm_flag' rule so the live upsert stores at most one per move.
    raw = provider.chat(messages)
    items = extract_json_array(raw)
    if not items:
        return
    amount_by_id = {e.id: e.amount for e in entries}
    date_by_id = {e.id: e.date for e in entries}
    existing = {(f.move_id, f.rule) for f in findings}
    for item in items:
        if not isinstance(item, dict):
            continue
        move_id = item.get('move_id')
        if move_id not in amount_by_id:
            continue
        raw_description = str(item.get('description') or '')
        safe_description = ''.join(
            character
            if not unicodedata.category(character).startswith('C') else ' '
            for character in raw_description
        )
        description = ' '.join(safe_description.split())
        if not description:
            continue
        evidence_hash = hashlib.sha256(
            raw_description.encode('utf-8')
        ).hexdigest()
        description = description[:MAX_PROVIDER_DESCRIPTION_CHARS]
        severity = item.get('severity')
        if severity not in ('low', 'medium', 'high'):
            severity = 'medium'
        if (move_id, 'llm_flag') in existing:
            continue
        existing.add((move_id, 'llm_flag'))
        findings.append(Finding(
            move_id=move_id, severity=severity, rule='llm_flag',
            description=description,
            amount=amount_by_id[move_id], date=date_by_id[move_id],
            source='provider', provider_key=provider_key,
            provider_evidence_hash=evidence_hash,
        ))
