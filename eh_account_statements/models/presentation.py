# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Shared presentation helpers for the IAS 1 primary statements.

Four concerns, each shared by the statement of comprehensive income and the
statement of changes in equity:

1. Structural OCI recycling (IAS 1.82A): two account tags, EH OCI Recyclable
   and EH OCI Non-Recyclable, classify the ledger OCI reserve accounts. SOCI
   lines that reference a tagged source account derive their reclassification
   section from the tag instead of a hand flag. A registry of the suite's own
   OCI account settings drives default tag assignment (CTA and cash-flow /
   net-investment hedge reserves recyclable; FVOCI-debt reserve recyclable;
   FVOCI-equity reserve and defined-benefit remeasurement reserve
   non-recyclable). Accounts without an explicit suite setting remain for a
   preparer to tag; translated account names are never configuration.

2. Current / non-current completeness (IAS 1.60): confirming a statement
   scans the posted ledger for accounts carrying balances whose account type
   falls outside the recognised current / non-current / equity / P&L sets.
   Such balances would silently escape a classified statement of financial
   position, so confirmation blocks until they are reclassified or a manager
   overrides with a recorded reason.

3. NCI linkage: when eh_account_consolidation is installed, a reviewed or
   closed exact-period, same-currency consolidation run can source NCI. SoCI
   consumes its reviewed period profit/OCI flow; SoCE consumes its closing NCI
   equity. Source identity, review stamps, period, currency and engine version
   are snapshotted and revalidated. The lookup stays a soft registry probe.

4. IAS 34 thin interim support: statements carry a period type (annual by
   default, preserving prior behaviour). An interim statement is labelled as
   such, can point at its two IAS 34.20 comparatives (the comparable interim
   period of the immediately preceding financial year, and the immediately
   preceding annual period), and can be flagged condensed per IAS 34.8, which
   collapses the presentation to the mandatory minimum line items (headings
   and subtotals). All of this is presentation only: no measurement changes.
"""

import logging

from odoo import _, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import float_is_zero
from odoo.tools.misc import formatLang

from odoo.addons.eh_account_base.tools.orm_compat import read_group_compat

_logger = logging.getLogger(__name__)

# XML ids of the two structural OCI recycling tags shipped by this module.
OCI_TAG_RECYCLABLE = 'eh_account_statements.tag_eh_oci_recyclable'
OCI_TAG_NON_RECYCLABLE = 'eh_account_statements.tag_eh_oci_non_recyclable'

# ---------------------------------------------------------------------------
# IAS 1.60 recognised classification sets. An account whose type is in none
# of these sets cannot land in a classified statement of financial position
# (current / non-current), in equity or in profit or loss, so a posted
# balance on it is an IAS 1 completeness breach.
# ---------------------------------------------------------------------------
CURRENT_ACCOUNT_TYPES = frozenset({
    'asset_receivable', 'asset_cash', 'asset_current', 'asset_prepayments',
    'liability_payable', 'liability_credit_card', 'liability_current',
})
NON_CURRENT_ACCOUNT_TYPES = frozenset({
    'asset_non_current', 'asset_fixed', 'liability_non_current',
})
EQUITY_ACCOUNT_TYPES = frozenset({'equity', 'equity_unaffected'})
PL_ACCOUNT_TYPES = frozenset({
    'income', 'income_other',
    'expense', 'expense_other', 'expense_depreciation',
    'expense_direct_cost',
})
# Memorandum/off-balance accounts are intentionally absent from primary
# financial statements. Their existence is not an IAS 1.60 classification
# failure and must never block statement confirmation.
NON_STATEMENT_ACCOUNT_TYPES = frozenset({'off_balance'})
RECOGNISED_ACCOUNT_TYPES = (
    CURRENT_ACCOUNT_TYPES | NON_CURRENT_ACCOUNT_TYPES
    | EQUITY_ACCOUNT_TYPES | PL_ACCOUNT_TYPES
)

# IAS 34 period types. 'annual' is the default so every existing statement
# and every statement created without touching the field behaves exactly as
# before this feature existed.
PERIOD_TYPES = [
    ('annual', "Annual"),
    ('interim', "Interim (IAS 34)"),
]

# ---------------------------------------------------------------------------
# Default OCI recycling tag sources. Each entry names a model of another
# suite module (soft dependency: skipped when not installed), the field on it
# that holds a ledger OCI reserve account, the recycling verdict for that
# reserve, and an extra domain narrowing which records feed the verdict.
#
# Verdicts follow IAS 1.82A as applied by each source standard:
# - CTA / translation reserve: recyclable (IAS 21.48 reclassifies the reserve
#   to P&L on disposal of the foreign operation).
# - Cash flow hedge and net investment hedge reserves: recyclable
#   (IFRS 9.6.5.11(d) / IAS 21.48).
# - FVOCI-debt reserve: recyclable (IFRS 9.5.7.10).
# - FVOCI-equity election reserve: non-recyclable (IFRS 9.B5.7.1, transfers
#   stay within equity).
# - Defined benefit remeasurement reserve: non-recyclable (IAS 19.122).
# - Revaluation surplus: non-recyclable (IAS 16.41 / IAS 38.87), but the asset
#   module's wizard-selected account is not a persistent configuration source.
#   It is deliberately not auto-tagged: the preparer assigns the structural
#   tag explicitly and the SoCI completeness gate catches omissions without
#   relying on an English account name.
# ---------------------------------------------------------------------------
OCI_TAG_SOURCES = (
    # (model, account field, verdict, extra domain)
    ('eh.fx.cta.position', 'cta_account_id', 'recyclable', []),
    ('eh.consol.entity', 'cta_account_id', 'recyclable', []),
    ('eh.fx.hedge', 'oci_account_id', 'recyclable',
     [('hedge_type', 'in', ('cash_flow', 'net_investment'))]),
    ('eh.fair.value.item', 'oci_account_id', 'recyclable',
     ['|', ('ifrs9_classification', '=', 'fvoci_debt'),
      '&', ('ifrs9_classification', '=', False),
      ('fvoci_classification', '=', 'fvoci_debt')]),
    ('eh.fair.value.item', 'oci_account_id', 'non_recyclable',
     ['|', ('ifrs9_classification', '=', 'fvoci_equity'),
      '&', ('ifrs9_classification', '=', False),
      ('fvoci_classification', '=', 'fvoci_equity')]),
    ('eh.benefit.plan', 'oci_account_id', 'non_recyclable', []),
)


def statement_ledger_company_ids(statement):
    """Complete, actor-authorised legal-entity branch scope.

    Odoo 18+ stores branch journal items on each branch company while the
    primary IAS 1 statement belongs to the root/legal entity.  Exact-company
    domains therefore omit branch books.  A partial branch selection is just
    as unsafe: it would produce a plausible but incomplete signed statement.

    Older series have no branch API, so their established exact-company scope
    remains unchanged.
    """
    statement.ensure_one()
    company = statement.company_id
    if not company:
        return []
    if 'parent_id' not in company._fields \
            or not hasattr(company, '_accessible_branches'):
        return company.ids

    Company = statement.env['res.company']
    complete = Company.sudo().search([
        ('id', 'child_of', company.id),
    ])
    if statement.env.su:
        return complete.ids

    accessible = company._accessible_branches()
    if set(accessible.ids) != set(complete.ids):
        raise AccessError(_(
            "The statement company has accounting branches outside the "
            "actor's active company scope. Select every branch of %(company)s "
            "before deriving, checking, or confirming a primary statement.",
            company=company.display_name,
        ))
    return accessible.ids


def oci_recycling_tags(env):
    """Return (recyclable_tag, non_recyclable_tag), either may be empty."""
    return (
        env.ref(OCI_TAG_RECYCLABLE, raise_if_not_found=False),
        env.ref(OCI_TAG_NON_RECYCLABLE, raise_if_not_found=False),
    )


def apply_default_oci_recycling_tags(env, company):
    """Scan the installed suite modules' OCI account settings and tag the
    reserve accounts that carry NO recycling tag yet.

    Default assignment only: an account already carrying either tag (whether
    from an earlier run or set by hand) is never re-tagged, so a deliberate
    manual classification is never fought. The per-line discrepancy flag on
    the SOCI surfaces any tag that disagrees with a preparer's judgement.

    Returns the number of accounts newly tagged.
    """
    rec_tag, non_tag = oci_recycling_tags(env)
    if not (rec_tag and non_tag):
        return 0
    both = rec_tag + non_tag
    # account -> verdict; the first source to claim an account wins so a
    # conflicting later source never flip-flops the assignment within one run.
    verdicts = {}
    for model_name, field_name, verdict, extra_domain in OCI_TAG_SOURCES:
        if model_name not in env.registry:
            continue
        Model = env[model_name]
        if field_name not in Model._fields:
            continue
        domain = list(extra_domain)
        if 'company_id' in Model._fields:
            domain.append(('company_id', '=', company.id))
        elif 'parent_company_id' in Model._fields:
            domain.append(('parent_company_id', '=', company.id))
        for account in Model.search(domain).mapped(field_name):
            verdicts.setdefault(account.id, verdict)
    Account = env['account.account']
    applied = 0
    for account_id, verdict in verdicts.items():
        account = Account.browse(account_id)
        if account.tag_ids & both:
            continue  # already classified; never overwrite
        tag = rec_tag if verdict == 'recyclable' else non_tag
        account.write({'tag_ids': [(4, tag.id)]})
        applied += 1
    return applied


# ---------------------------------------------------------------------------
# IAS 1.60 completeness guard
# ---------------------------------------------------------------------------

def classification_misfit_pairs(statement):
    """Accounts with posted balances at the statement period end whose
    account type falls outside the recognised classification sets.

    Returns a list of (account, balance) pairs, balance in the ledger sign
    convention (debit-positive), zero balances dropped within currency
    rounding.
    """
    statement.ensure_one()
    if not (statement.company_id and statement.period_end):
        return []
    domain = [
        ('company_id', 'in', statement_ledger_company_ids(statement)),
        ('parent_state', '=', 'posted'),
        ('date', '<=', statement.period_end),
        ('account_id.account_type', 'not in',
        sorted(RECOGNISED_ACCOUNT_TYPES | NON_STATEMENT_ACCOUNT_TYPES)),
    ]
    rounding = (statement.currency_id
                or statement.company_id.currency_id).rounding or 0.01
    groups = read_group_compat(
        statement.env['account.move.line'], domain,
        ['account_id'], ['balance:sum'],
    )
    pairs = []
    for account, balance in groups:
        if not account:
            continue
        balance = balance or 0.0
        if float_is_zero(balance, precision_rounding=rounding):
            continue
        pairs.append((account, balance))
    return pairs


def format_statement_amount(statement, amount):
    """Locale-aware amount text using the statement currency precision."""
    statement.ensure_one()
    currency = statement.currency_id or statement.company_id.currency_id
    return formatLang(
        statement.env, amount, currency_obj=currency, grouping=True,
    )


def format_misfit_note(statement, pairs):
    """Human-readable listing of the IAS 1.60 misfit accounts."""
    lines = []
    for account, balance in pairs:
        account = account.with_company(statement.company_id)
        lines.append(_(
            "%(code)s %(name)s (type: %(atype)s, balance: %(balance)s)",
            code=account.code or '?', name=account.name,
            atype=account.account_type,
            balance=format_statement_amount(statement, balance)))
    return '\n'.join(lines)


def check_classification_completeness(statement, statement_label):
    """IAS 1.60 completeness gate, called from action_confirm.

    Blocks confirmation while any posted balance sits on an account whose
    type is outside the recognised current / non-current / equity / P&L
    sets, unless the manager override flag is set with a reason; the
    override is then logged to the chatter and the server log.
    """
    statement.ensure_one()
    pairs = classification_misfit_pairs(statement)
    if not pairs:
        return
    listing = format_misfit_note(statement, pairs)
    if not statement.classification_override:
        raise UserError(_(
            "IAS 1.60 completeness: the posted ledger carries balances on "
            "accounts whose type is outside the recognised current / "
            "non-current classification sets, so the %(label)s cannot be "
            "confirmed as complete:\n%(listing)s\n\nReclassify these "
            "accounts, or set the classification override with a reason "
            "to confirm anyway.",
            label=statement_label, listing=listing))
    reason = (statement.classification_override_reason or '').strip()
    if not reason:
        raise UserError(_(
            "The IAS 1.60 classification override requires a reason. "
            "Record why the unclassified balances are acceptable before "
            "confirming."))
    statement.message_post(body=_(
        "IAS 1.60 classification completeness OVERRIDDEN on confirm by "
        "%(user)s. Reason: %(reason)s\nUnclassified balances:\n%(listing)s",
        user=statement.env.user.display_name, reason=reason,
        listing=listing))
    _logger.info(
        "IAS 1.60 classification override on %s %s by %s: %s",
        statement._name, statement.display_name,
        statement.env.user.login, reason)


# ---------------------------------------------------------------------------
# IAS 1.82A OCI recycling-tag completeness guard
#
# The reclassification section a component lands in (items that may be
# reclassified vs items that will not) must be DERIVED from the recycling tag
# on the source OCI account, not left to a hand flag. A component whose source
# account carries no recycling tag - or which names no source account at all -
# has no structural signal: its section rests entirely on the manual
# will_reclassify flag, which is exactly the honour-system placement IAS 1.82A
# is meant to remove. Such a component is a recycling-classification misfit;
# confirming a statement of comprehensive income is blocked while any misfit
# with a non-zero amount exists, unless a manager overrides with a reason.
# ---------------------------------------------------------------------------

def oci_recycling_misfit_lines(soci):
    """OCI lines whose reclassification section is not tag-derived.

    A line is a misfit when it carries a non-zero amount AND its source
    account gives no recycling verdict: either no source account is set, or
    the account carries neither EH OCI recycling tag. A zero-amount line does
    not affect either OCI subtotal, so it is not flagged (it cannot mis-state
    the sections). Returns an ``eh.soci.line`` recordset.
    """
    soci.ensure_one()
    rounding = (soci.currency_id or soci.company_id.currency_id).rounding \
        or 0.01
    misfits = soci.env['eh.soci.line']
    for line in soci.line_ids:
        if float_is_zero(line.amount, precision_rounding=rounding):
            continue
        if line._eh_tag_verdict() is None:
            misfits |= line
    return misfits


def format_oci_misfit_note(misfits):
    """Human-readable listing of the untagged OCI recycling lines."""
    parts = []
    for line in misfits:
        account = line.account_id
        source = (
            _("%(code)s %(name)s (untagged)",
              code=account.code or '?', name=account.name)
            if account else _("no source account"))
        # The interpolation key must not be 'source': the translation
        # alias _() takes its own positional 'source' argument (the string
        # to translate) on Odoo 18, and a same-named kwarg collides.
        parts.append(_(
            "%(line)s (amount %(amount)s): %(detail)s",
            line=line.name or '?',
            amount=format_statement_amount(line.soci_id, line.amount),
            detail=source))
    return '\n'.join(parts)


def check_oci_recycling_completeness(soci):
    """IAS 1.82A OCI recycling completeness gate, called from action_confirm.

    Blocks confirmation while any OCI component with a non-zero amount has no
    tag-derived reclassification section (no source account, or an untagged
    source account), unless the OCI-tag override flag is set with a reason.
    The override is then logged to the chatter and the server log. Statements
    with no OCI lines, or whose OCI lines are all tag-derived, pass silently.
    """
    soci.ensure_one()
    misfits = oci_recycling_misfit_lines(soci)
    if not misfits:
        return
    listing = format_oci_misfit_note(misfits)
    if not soci.oci_tag_override:
        raise UserError(_(
            "IAS 1.82A completeness: these other comprehensive income "
            "components carry no recycling tag on their source account, so "
            "whether they may be reclassified to profit or loss rests on a "
            "manual flag rather than the account classification:\n"
            "%(listing)s\n\nTag the source accounts (EH OCI Recyclable / "
            "Non-Recyclable), or set the OCI recycling override with a "
            "reason to confirm anyway.",
            listing=listing))
    reason = (soci.oci_tag_override_reason or '').strip()
    if not reason:
        raise UserError(_(
            "The IAS 1.82A OCI recycling override requires a reason. Record "
            "why the untagged OCI components are acceptable before "
            "confirming."))
    soci.message_post(body=_(
        "IAS 1.82A OCI recycling completeness OVERRIDDEN on confirm by "
        "%(user)s. Reason: %(reason)s\nUntagged OCI components:\n%(listing)s",
        user=soci.env.user.display_name, reason=reason, listing=listing))
    _logger.info(
        "IAS 1.82A OCI recycling override on %s %s by %s: %s",
        soci._name, soci.display_name, soci.env.user.login, reason)


# ---------------------------------------------------------------------------
# NCI linkage to eh_account_consolidation (soft registry lookup)
# ---------------------------------------------------------------------------

CONSOL_SOURCE_STATES = ('reviewed', 'closed')
CONSOL_SOURCE_STATE_SELECTION = [
    ('reviewed', "Reviewed"),
    ('closed', "Closed"),
]
CONSOL_PROVENANCE_FIELDS = (
    'consol_run_res_id', 'consol_run_state',
    'consol_period_start', 'consol_period_end', 'consol_currency_id',
    'consol_engine_provenance_version', 'consol_source_move_version',
    'consol_reviewed_at', 'consol_reviewed_by_id',
)


def find_covering_consol_run(statement):
    """Unique signed consolidation run for the exact statement period.

    A period-flow source cannot come from a covering annual window, and a
    statement in company currency cannot consume a different presentation
    currency. ``computed`` is deliberately excluded: it has no manager review
    signature and can be reset/recomputed. Multiple signed candidates are
    ambiguous and therefore refused rather than selected by recency.
    """
    statement.ensure_one()
    env = statement.env
    if 'eh.consol.run' not in env.registry:
        return None
    if not (statement.company_id and statement.period_start
            and statement.period_end):
        return None
    runs = env['eh.consol.run'].search([
        ('entity_id.parent_company_id', '=', statement.company_id.id),
        ('state', 'in', CONSOL_SOURCE_STATES),
        ('period_from', '=', statement.period_start),
        ('period_to', '=', statement.period_end),
        ('presentation_currency_id', '=', statement.currency_id.id),
    ], order='id', limit=2)
    if len(runs) > 1:
        raise UserError(_(
            "More than one reviewed/closed consolidation run matches company "
            "%(company)s, exact period %(start)s to %(end)s and currency "
            "%(currency)s. Resolve the duplicate signed source before "
            "prefilling NCI.",
            company=statement.company_id.display_name,
            start=statement.period_start,
            end=statement.period_end,
            currency=statement.currency_id.display_name,
        ))
    return runs or None


def consol_nci_carve(run):
    """The run's NCI carve-out as a credit-positive equity figure.

    Consolidation run lines store amounts in the ledger sign convention
    (equity credit-negative), so the header nci_amount of a normal positive
    non-controlling interest is negative; the statements worksheets are
    credit-positive, hence the negation.
    """
    return -(run.nci_amount or 0.0)


def _validate_consol_run_contract(statement, run, source_kind):
    """Lock and prove one consolidation source under caller permissions."""
    statement.ensure_one()
    run.ensure_one()
    # Serialize against reset/reopen/close and then reload every field used by
    # this trust decision. The private method exists on supported suite runs;
    # absence means the installed consolidation module cannot prove evidence.
    lock = getattr(run, '_eh_lock_for_transition', None)
    prove = getattr(run, '_eh_assert_current_engine_provenance', None)
    prove_fresh = getattr(run, '_eh_assert_source_move_version_fresh', None)
    if not (lock and prove and prove_fresh):
        raise UserError(_(
            "Installed consolidation engine does not expose the evidence "
            "contract required by primary statements. Upgrade it first."
        ))
    if not run.env.su:
        run._eh_check_access('read')
    lock()
    run.invalidate_recordset()
    if run.entity_id.parent_company_id != statement.company_id:
        raise UserError(_(
            "Consolidation source parent company does not match the "
            "statement company."
        ))
    if run.state not in CONSOL_SOURCE_STATES or not (
        run.reviewed_at and run.reviewed_by_id
    ):
        raise UserError(_(
            "Consolidation source must be manager-reviewed or closed; "
            "computed output is unsigned and cannot source a statement."
        ))
    if (
        run.period_from != statement.period_start
        or run.period_to != statement.period_end
    ):
        raise UserError(_(
            "Consolidation source period must exactly match the statement "
            "period; a covering stock window cannot source a period flow."
        ))
    if run.presentation_currency_id != statement.currency_id:
        raise UserError(_(
            "Consolidation presentation currency must match the statement "
            "currency."
        ))
    prove()
    prove_fresh()
    if source_kind == 'period_flow':
        required = (
            'nci_period_flow_available',
            'nci_period_comprehensive_income_amount',
        )
        if any(name not in run._fields for name in required):
            raise UserError(_(
                "Consolidation source has no reviewed NCI period profit/OCI "
                "output. Upgrade and review a fresh run."
            ))
        if not run.nci_period_flow_available:
            raise UserError(_(
                "Consolidation source has no reviewed NCI period profit/OCI "
                "output. Review its exact-period attribution schedule first."
            ))
        amount = run.nci_period_comprehensive_income_amount
    elif source_kind == 'closing_equity':
        amount = consol_nci_carve(run)
    else:
        raise UserError(_("Unsupported consolidation source dimension."))
    return run.presentation_currency_id.round(amount or 0.0)


def consol_source_snapshot(statement, run, source_kind):
    """Validated amount plus durable, dependency-free source provenance."""
    amount = _validate_consol_run_contract(statement, run, source_kind)
    return amount, {
        'consol_run_name': run.name,
        'consol_run_res_id': run.id,
        'consol_run_state': run.state,
        'consol_period_start': run.period_from,
        'consol_period_end': run.period_to,
        'consol_currency_id': run.presentation_currency_id.id,
        'consol_engine_provenance_version': run.engine_provenance_version,
        'consol_source_move_version': run.source_move_version,
        'consol_reviewed_at': run.reviewed_at,
        'consol_reviewed_by_id': run.reviewed_by_id.id,
    }


def validate_statement_consol_source(statement, source_kind):
    """Revalidate a snapshotted source before statement confirmation."""
    statement.ensure_one()
    if not statement.consol_nci_available:
        return None
    if 'eh.consol.run' not in statement.env.registry \
            or not statement.consol_run_res_id:
        raise UserError(_(
            "NCI consolidation provenance is missing. Re-prefill from a "
            "reviewed exact-period run before confirming the statement."
        ))
    run = statement.env['eh.consol.run'].browse(
        statement.consol_run_res_id
    ).exists()
    if not run:
        raise UserError(_(
            "The consolidation run cited by the NCI snapshot no longer "
            "exists."
        ))
    amount = _validate_consol_run_contract(statement, run, source_kind)
    currency = statement.currency_id
    rounding = currency.rounding or 0.01
    snapshot_pairs = (
        (statement.consol_run_name, run.name),
        (statement.consol_period_start, run.period_from),
        (statement.consol_period_end, run.period_to),
        (statement.consol_currency_id, run.presentation_currency_id),
        (statement.consol_engine_provenance_version,
         run.engine_provenance_version),
        (statement.consol_source_move_version, run.source_move_version),
        (statement.consol_reviewed_at, run.reviewed_at),
        (statement.consol_reviewed_by_id, run.reviewed_by_id),
    )
    state_valid = (
        statement.consol_run_state == run.state
        or statement.consol_run_state == 'reviewed'
        and run.state == 'closed'
    )
    amount_valid = float_is_zero(
        currency.round(statement.consol_nci_amount) - amount,
        precision_rounding=rounding,
    )
    if not state_valid or not amount_valid or any(
        snap != current for snap, current in snapshot_pairs
    ):
        raise UserError(_(
            "NCI consolidation source changed after prefill. Re-prefill from "
            "the current reviewed exact-period run before confirming."
        ))
    return run


# ---------------------------------------------------------------------------
# IAS 34 interim presentation
# ---------------------------------------------------------------------------

def presentation_label(statement, base_label):
    """Statement heading per the period type.

    IAS 34.8 permits a condensed set of interim statements; IAS 34.20 sets
    the comparative convention. The label carries the interim / condensed
    qualification so a reader can never mistake an interim worksheet for the
    annual statement.
    """
    if statement.period_type == 'interim':
        if statement.condensed:
            return _("Condensed interim %(label)s (IAS 34.8)",
                     label=base_label)
        return _("Interim %(label)s (IAS 34)", label=base_label)
    return base_label[:1].upper() + base_label[1:]


def check_interim_fields(statement):
    """Validate annual and IAS 34 comparative fields.

    - The condensed flag and prior-interim comparative are interim-only.
      ``comparative_annual_id`` also serves as the IAS 1 annual prior-period
      comparative, so annual statements may carry it.
    - The prior-interim comparative must itself be an interim statement of
      the same company ending before this period starts (the comparable
      interim period of the immediately preceding financial year,
      IAS 34.20).
    - The prior-annual comparative must be an annual statement of the same
      company ending before this period starts (the immediately preceding
      annual period, IAS 34.20).
    """
    statement.ensure_one()
    if statement.period_type != 'interim':
        if statement.condensed or statement.comparative_interim_id:
            raise ValidationError(_(
                "The condensed flag and prior-interim comparative apply to "
                "interim statements only. Set the period type to interim "
                "first, or clear them."))
        candidates = ((statement.comparative_annual_id, 'annual',
                       _("prior annual comparative")),)
    else:
        candidates = (
            (statement.comparative_interim_id, 'interim',
             _("prior interim comparative")),
            (statement.comparative_annual_id, 'annual',
             _("prior annual comparative")),
        )
    for comp, wanted_type, label in candidates:
        if not comp:
            continue
        if comp == statement:
            raise ValidationError(_(
                "A statement cannot be its own IAS 34 comparative."))
        if comp.period_type != wanted_type:
            raise ValidationError(_(
                "The %(label)s must be a statement with period type "
                "'%(wanted)s' (IAS 34.20).",
                label=label, wanted=wanted_type))
        if comp.company_id != statement.company_id:
            raise ValidationError(_(
                "The %(label)s must belong to the same company.",
                label=label))
        if (comp.period_end and statement.period_start
                and fields.Date.to_date(comp.period_end)
                >= fields.Date.to_date(statement.period_start)):
            raise ValidationError(_(
                "The %(label)s must cover a period ending before this "
                "statement's period starts (IAS 34.20).",
                label=label))
