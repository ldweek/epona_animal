# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Collections case next-best-action suggester.

The deterministic default produces an action suggestion based on the
case's days-overdue, promise-to-pay state, and follow-up history:

* No contact yet AND days_overdue >= 30 -> 'first_contact_email'
* Contacted but no promise AND days_overdue >= 45 -> 'phone_call'
* Promise to pay broken (lapsed) -> 'escalate_to_manager'
* Days overdue >= 90 AND no payment plan -> 'send_demand_letter'
* Days overdue >= 120 AND recorded demand letter >= 30 days old
  -> 'refer_to_collections_agency'
* Demand-letter age missing or below 30 days -> 'review_demand_letter'
* Days overdue >= 180 -> 'consider_write_off'
* Promise active AND not yet at promise date -> 'monitor'
* Otherwise -> 'manual_review'

The LLM hook can produce a custom action recommendation from the
recorded case snapshot and follow-up history. Stub providers raise
ProviderError; the deterministic action is returned.
"""

import dataclasses
import datetime
from typing import Optional

from odoo import _

from .provider_registry import (
    get_provider, has_provider, ProviderError,
)
from .llm_parsing import extract_json_object


@dataclasses.dataclass
class CaseSnapshot:
    """Plain transport record describing a collections case."""
    days_overdue: int
    total_overdue: float
    contact_count: int = 0
    has_active_promise: bool = False
    has_broken_promise: bool = False
    last_action_type: str = ''  # 'email' | 'call' | 'letter' | ''
    last_action_days_ago: int = 0
    has_demand_letter: bool = False
    demand_letter_days_ago: Optional[int] = None
    has_payment_plan: bool = False
    customer_segment: str = ''  # 'enterprise' | 'mid' | 'smb'
    is_resolved: bool = False
    is_disputed: bool = False
    is_paused: bool = False
    writeoff_materiality: float = 0.0


@dataclasses.dataclass
class Suggestion:
    action: str           # short action key
    rationale: str        # human-readable
    priority: str         # 'low' | 'medium' | 'high'


def suggest(
    case: CaseSnapshot,
    provider_key: str = 'manual',
    provider_config: Optional[dict] = None,
) -> Suggestion:
    """Return the deterministic suggestion; optionally augment via LLM.

    :param case: a CaseSnapshot.
    :param provider_key: registered provider key. Manual or missing
        returns the deterministic suggestion.
    :return: Suggestion record.
    """
    suggestion = _deterministic_suggest(case)
    # Terminal/suspended states and proved promises/plans are accounting
    # workflow controls, not soft AI hints. A provider may enrich ordinary
    # open-case recommendations but must never restart dunning on these cases.
    if (
        case.is_resolved
        or case.is_disputed
        or case.is_paused
        or case.has_payment_plan
        or case.has_active_promise
        or case.has_broken_promise
    ):
        return suggestion
    if provider_key and provider_key != 'manual':
        try:
            if has_provider(provider_key):
                provider = get_provider(provider_key, provider_config)
                if not getattr(provider, 'is_manual', False):
                    enriched = _augment_via_llm(
                        provider, case, suggestion,
                    )
                    if enriched:
                        return enriched
        except ProviderError:
            pass
    return suggestion


def provider_suggest(
    case: CaseSnapshot,
    provider_key: str = '',
    provider_config: Optional[dict] = None,
) -> Suggestion:
    """Return genuine provider output or raise; never relabel a rule as AI."""
    deterministic = _deterministic_suggest(case)
    if (
        case.is_resolved
        or case.is_disputed
        or case.is_paused
        or case.has_payment_plan
        or case.has_active_promise
        or case.has_broken_promise
    ):
        raise ProviderError(
            "This case is governed by a recorded workflow control; use the "
            "deterministic recommendation instead of an AI override."
        )
    if not provider_key or provider_key == 'manual' \
            or not has_provider(provider_key):
        raise ProviderError("No live AI provider is configured.")
    provider = get_provider(provider_key, provider_config)
    if getattr(provider, 'is_manual', False):
        raise ProviderError("The configured AI provider is manual-only.")
    enriched = _augment_via_llm(provider, case, deterministic)
    if not enriched:
        raise ProviderError(
            "The AI provider returned no valid next-action suggestion."
        )
    return enriched


def _deterministic_suggest(case: CaseSnapshot) -> Suggestion:
    if case.is_resolved:
        return Suggestion(
            action='no_action_resolved',
            rationale=_(
                "Case is resolved; no further collection action is due."
            ),
            priority='low',
        )
    if case.is_disputed:
        return Suggestion(
            action='review_dispute',
            rationale=_(
                "Case is disputed; suspend dunning and resolve the dispute "
                "before any collection escalation."
            ),
            priority='medium',
        )
    if case.is_paused:
        return Suggestion(
            action='monitor_paused_case',
            rationale=_(
                "Follow-up is paused; monitor the case and resume only after "
                "the recorded hold is cleared."
            ),
            priority='low',
        )
    if case.has_payment_plan:
        return Suggestion(
            action='monitor_payment_plan',
            rationale=_(
                "Active payment plan recorded; monitor compliance instead of "
                "starting the demand-letter escalation."
            ),
            priority='low',
        )
    if case.has_broken_promise:
        return Suggestion(
            action='escalate_to_manager',
            rationale=_(
                "Promise to pay lapsed; escalate per the dunning "
                "ladder so the manager applies pressure beyond the "
                "collector's reach."
            ),
            priority='high',
        )
    if case.has_active_promise:
        return Suggestion(
            action='monitor',
            rationale=_(
                "Active promise to pay in place; do not contact "
                "until the promise date passes. Re-evaluate then."
            ),
            priority='low',
        )
    if case.days_overdue >= 180:
        materiality = max(0.0, float(case.writeoff_materiality or 0.0))
        is_material = not materiality or case.total_overdue >= materiality
        materiality_text = _(
            "The balance %(balance).2f is at or above the company review "
            "threshold %(threshold).2f.",
            balance=case.total_overdue,
            threshold=materiality,
        ) if materiality else _(
            "No company write-off materiality threshold is configured; "
            "manager review is required."
        )
        if materiality and not is_material:
            materiality_text = _(
                "The balance %(balance).2f is below the company review "
                "threshold %(threshold).2f, but evidence and approval are "
                "still required.",
                balance=case.total_overdue,
                threshold=materiality,
            )
        return Suggestion(
            action='consider_write_off',
            rationale=_(
                "Over 180 days overdue (%(days)d). %(materiality)s Review "
                "documented recovery "
                "efforts, collectability evidence, approval policy, and tax "
                "requirements before any partial or full write-off.",
                days=case.days_overdue,
                materiality=materiality_text,
            ),
            priority='high' if is_material else 'medium',
        )
    if case.days_overdue >= 90 and case.has_demand_letter:
        if (
            case.days_overdue >= 120
            and case.demand_letter_days_ago is not None
            and case.demand_letter_days_ago >= 30
        ):
            return Suggestion(
                action='refer_to_collections_agency',
                rationale=_(
                    "The recorded demand letter is %(days)d days old. Review "
                    "the "
                    "logged response and approved escalation policy before "
                    "referring the case externally.",
                    days=case.demand_letter_days_ago,
                ),
                priority='high',
            )
        age = (
            _(
                "%(days)d days old",
                days=case.demand_letter_days_ago,
            )
            if case.demand_letter_days_ago is not None
            else _("missing a reliable sent date")
        )
        return Suggestion(
            action='review_demand_letter',
            rationale=_(
                "A demand letter is recorded but is %(age)s; verify delivery, "
                "response, and the required waiting period before external "
                "referral.",
                age=age,
            ),
            priority='medium',
        )
    if case.days_overdue >= 90 and not case.has_payment_plan:
        return Suggestion(
            action='send_demand_letter',
            rationale=_(
                "Over 90 days overdue and no payment plan; send "
                "formal demand letter to start the legal-action "
                "clock."
            ),
            priority='high',
        )
    if case.contact_count > 0 and case.days_overdue >= 45:
        return Suggestion(
            action='phone_call',
            rationale=_(
                "Prior contact is recorded but no promise or payment plan "
                "is active at %(days)d days overdue; review the history and "
                "make a documented phone follow-up.",
                days=case.days_overdue,
            ),
            priority='medium',
        )
    if case.contact_count == 0 and case.days_overdue >= 30:
        return Suggestion(
            action='first_contact_email',
            rationale=_(
                "No contact attempts yet on a %(days)d-day overdue case; "
                "send the standard first-contact email today.",
                days=case.days_overdue,
            ),
            priority='medium',
        )
    return Suggestion(
        action='manual_review',
        rationale=_(
            "Case state does not match a default rule; collector "
            "should review manually."
        ),
        priority='low',
    )


def _augment_via_llm(provider, case, deterministic):
    summary = (
        "days_overdue=%d total=%.2f contacts=%d "
        "active_promise=%s broken_promise=%s "
        "last_action=%s last_action_days_ago=%d "
        "demand_letter=%s demand_letter_days_ago=%s "
        "payment_plan=%s segment=%s"
        " resolved=%s disputed=%s paused=%s"
    ) % (
        case.days_overdue, case.total_overdue, case.contact_count,
        case.has_active_promise, case.has_broken_promise,
        case.last_action_type or 'none', case.last_action_days_ago,
        case.has_demand_letter, case.demand_letter_days_ago,
        case.has_payment_plan,
        case.customer_segment or 'unknown',
        case.is_resolved, case.is_disputed, case.is_paused,
    )
    messages = [
        {
            'role': 'system',
            'content': (
                "You are a collections specialist. Given a case "
                "snapshot and the deterministic next-action "
                "suggestion, return a JSON object with three keys: "
                "action (short snake_case identifier), rationale "
                "(plain English), priority (low | medium | high). "
                "Override the deterministic suggestion only when "
                "the case nuance warrants it; otherwise echo it."
            ),
        },
        {
            'role': 'user',
            'content': (
                "Deterministic suggestion: action=%s priority=%s\n"
                "Snapshot: %s"
            ) % (
                deterministic.action, deterministic.priority, summary,
            ),
        },
    ]
    # Real provider returns JSON we parse into a Suggestion; stub
    # providers raise ProviderError handled by the caller. A malformed
    # or partial response yields None so the deterministic suggestion
    # is used unchanged.
    raw = provider.chat(messages)
    data = extract_json_object(raw)
    if not data:
        return None
    action = data.get('action')
    priority = data.get('priority')
    if not action or priority not in ('low', 'medium', 'high'):
        return None
    return Suggestion(
        action=str(action),
        rationale=str(data.get('rationale') or deterministic.rationale),
        priority=priority,
    )
