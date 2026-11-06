# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Extension on `account.bank.statement.line` to record the live-connector
provider reference. Used as the de-duplication key on every fetch run.
"""

from datetime import timedelta

from odoo import api, fields, models
from odoo.tools.mail import html2plaintext


# Window (each side, in days) used by the fuzzy duplicate detector.
# Three days catches typical bank settlement lag between two import
# sources (e.g., a same-day OFX feed and a next-day CSV statement)
# without picking up legitimate recurring same-amount payments a week
# apart.
_DUPLICATE_DATE_WINDOW_DAYS = 3


class AccountBankStatementLine(models.Model):
    _inherit = 'account.bank.statement.line'

    eh_provider_reference = fields.Char(
        index=True, copy=False,
        help=(
            "Identifier supplied by the bank's live API for this "
            "transaction. The live-connector framework rejects any "
            "transaction whose reference has been imported before for "
            "the same journal."
        ),
    )

    unique_import_ref = fields.Char(
        index=True, copy=False,
        help=(
            "Stable per-line idempotency key built from the row content "
            "(date, amount, reference, narration) plus an occurrence "
            "counter that distinguishes genuinely identical rows in the "
            "same file. It does not depend on the row's position, so "
            "re-importing the same statement, or one with rows added or "
            "removed elsewhere, does not duplicate transactions on the "
            "same journal."
        ),
    )

    eh_duplicate_of_id = fields.Many2one(
        'account.bank.statement.line',
        index=True,
        copy=False,
        ondelete='set null',
        help=(
            "When the import wizard's fuzzy duplicate scan flags this "
            "line as a probable duplicate of an earlier line, the "
            "match is recorded here. The line is NOT auto-deleted: "
            "the operator confirms, because two payments with the "
            "same date and amount can be legitimate."
        ),
    )

    # Single-column uniqueness: the wizard prefixes the ref with the
    # journal id ("J<id>:<sha1>") so scoping stays per-journal while the
    # constraint works on every series (journal_id has no SQL column on
    # the statement line in Odoo 16/17, so a two-column unique constraint
    # silently fails to install there).
    _eh_unique_import_ref = models.Constraint(
        'unique(unique_import_ref)',
        'A bank statement line with this import reference already exists for the journal.',
    )

    @staticmethod
    def _eh_canonical_import_ref(journal_id, source_reference):
        """Return one cross-channel key for a bank-supplied identifier.

        File parsers expose the bank reference as ``unique_import_ref``;
        live adapters expose the same identifier as ``eh_provider_reference``.
        Both corridors must persist and compare the same journal-scoped value
        or importing one transaction through feed then file (or vice versa)
        books it twice.
        """
        source_reference = str(source_reference or '').strip()
        if not source_reference:
            return False
        prefix = 'J%s:' % int(journal_id)
        return (
            source_reference
            if source_reference.startswith(prefix)
            else prefix + source_reference
        )

    @staticmethod
    def _eh_duplicate_text(value):
        """Normalise bank text before fuzzy duplicate comparison."""
        value = value or ''
        if '<' in value and '>' in value:
            value = html2plaintext(value)
        stripped = value.strip().lower()
        return '' if stripped in ('/', '-', '.', '') else stripped

    @staticmethod
    def _eh_duplicate_text_matches(candidate, payment_ref, narration):
        normalise = AccountBankStatementLine._eh_duplicate_text
        ref_lc = normalise(payment_ref)
        narration_lc = normalise(narration)
        candidate_ref = normalise(candidate.payment_ref)
        candidate_narration = normalise(candidate.narration)
        if not (ref_lc or narration_lc) and not (
            candidate_ref or candidate_narration
        ):
            return True
        if ref_lc and candidate_ref and (
            ref_lc == candidate_ref
            or ref_lc in candidate_ref
            or candidate_ref in ref_lc
        ):
            return True
        return bool(
            narration_lc and candidate_narration and (
                narration_lc == candidate_narration
                or narration_lc in candidate_narration
                or candidate_narration in narration_lc
            )
        )

    @api.model
    def _eh_find_probable_duplicates(self, inbound_lines, prior_max_id=None):
        """Return ``{new_line_id: prior_line}`` using one candidate query.

        ``prior_max_id`` freezes candidate population before current import,
        preventing two same-file siblings from flagging each other.
        """
        inbound_lines = inbound_lines.filtered(
            lambda line: line.journal_id and line.date
            and line.amount is not None
        )
        if not inbound_lines:
            return {}
        journal_ids = set(inbound_lines.mapped('journal_id').ids)
        if len(journal_ids) != 1:
            return {}
        min_date = min(inbound_lines.mapped('date')) - timedelta(
            days=_DUPLICATE_DATE_WINDOW_DAYS)
        max_date = max(inbound_lines.mapped('date')) + timedelta(
            days=_DUPLICATE_DATE_WINDOW_DAYS)
        rounded_amounts = [round(float(line.amount), 2)
                           for line in inbound_lines]
        domain = [
            ('journal_id', '=', next(iter(journal_ids))),
            ('date', '>=', min_date),
            ('date', '<=', max_date),
            ('amount', '>=', min(rounded_amounts) - 0.005),
            ('amount', '<=', max(rounded_amounts) + 0.005),
        ]
        if prior_max_id is not None:
            domain.append(('id', '<=', prior_max_id))
        candidates_by_amount = {}
        for candidate in self.search(domain, order='id asc'):
            candidates_by_amount.setdefault(
                round(float(candidate.amount), 2), self.browse())
            candidates_by_amount[round(float(candidate.amount), 2)] |= candidate

        matches = {}
        for line in inbound_lines.sorted('id'):
            for candidate in candidates_by_amount.get(
                round(float(line.amount), 2), self.browse()
            ):
                if abs((candidate.date - line.date).days) > (
                    _DUPLICATE_DATE_WINDOW_DAYS
                ):
                    continue
                if self._eh_duplicate_text_matches(
                    candidate, line.payment_ref, line.narration
                ):
                    matches[line.id] = candidate
                    break
        return matches

    @api.model
    def _eh_find_probable_duplicate(self, journal_id, line_date, amount,
                                    payment_ref=None, narration=None,
                                    exclude_id=None):
        """Locate the most likely existing duplicate for an inbound line.

        Match criteria, applied in order:

        * Same journal.
        * Date within `_DUPLICATE_DATE_WINDOW_DAYS` either side; banks
          and import formats sometimes shift the value date by a day or
          two between feeds (CSV books on settlement day, OFX on value
          date).
        * Same amount to the cent. We do NOT relax this; an amount
          mismatch is the strongest signal of two distinct payments,
          and tolerance here would surface false positives the operator
          ignores.
        * If both rows have a non-empty payment_ref or narration, at
          least one must overlap (case-insensitive substring). Empty
          strings on either side don't disqualify; many CSV exports
          ship blank narration.

        Returns the matching line record (singleton) or empty recordset.
        """
        if not (journal_id and line_date and amount is not None):
            return self.browse()
        if not isinstance(line_date, (str, type(None))):
            min_date = line_date - timedelta(days=_DUPLICATE_DATE_WINDOW_DAYS)
            max_date = line_date + timedelta(days=_DUPLICATE_DATE_WINDOW_DAYS)
        else:
            return self.browse()
        rounded = round(float(amount), 2)
        domain = [
            ('journal_id', '=', journal_id),
            ('date', '>=', min_date),
            ('date', '<=', max_date),
            ('amount', '>=', rounded - 0.005),
            ('amount', '<=', rounded + 0.005),
        ]
        if exclude_id:
            domain.append(('id', '!=', exclude_id))
        # Order by id only: in Odoo 19 the line's `date` is a non-
        # stored related field on move_id and cannot be used in the
        # SQL ORDER BY. id ascending preserves a deterministic match
        # order (oldest line wins ties) without needing the related
        # column.
        candidates = self.search(domain, order='id asc')
        if not candidates:
            return self.browse()

        for cand in candidates:
            if self._eh_duplicate_text_matches(
                cand, payment_ref, narration
            ):
                return cand
        return self.browse()
