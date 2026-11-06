# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden presentation tests: structural OCI recycling (IAS 1.82A), the
IAS 1.60 current / non-current completeness guard, NCI prefill from a
covering consolidation run, and the IAS 34 thin interim support.

All expected amounts are hand-derived from the inputs stated in each test;
derivations are in the comments. Convention: worksheet figures are
credit-positive (a normal equity / OCI gain is a positive number), while the
consolidation run lines carry the ledger sign convention (equity
credit-negative), so the run's NCI carve-out of a positive minority interest
is a negative line amount.
"""

from odoo.exceptions import UserError, ValidationError
from odoo.tests import new_test_user, tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)
from odoo.addons.eh_account_statements.models import presentation


@tagged('eh_golden', 'eh_account_statements', 'post_install', '-at_install')
class TestGoldenPresentation(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # action_confirm is manager-gated; the acting user must be an EH
        # Accounting Manager for the confirm paths below.
        cls.group_manager = cls.env.ref('eh_account_base.group_eh_manager')
        cls.env.user.group_ids |= cls.group_manager
        cls.tag_recyclable = cls.env.ref(
            'eh_account_statements.tag_eh_oci_recyclable')
        cls.tag_non_recyclable = cls.env.ref(
            'eh_account_statements.tag_eh_oci_non_recyclable')
        cls.source_manager = new_test_user(
            cls.env,
            login='statements_consolidation_source_manager',
            groups='eh_account_base.group_eh_manager',
            company_id=cls.company.id,
        )

    # ---- structural OCI recycling (IAS 1.82A) ----

    def test_recycling_tags_drive_soci_sections(self):
        # GOLDEN: profit 100000. CTA reserve account tagged RECYCLABLE
        # carries the +8000 translation line; FVOCI-equity reserve account
        # tagged NON-RECYCLABLE carries the +5000 line. Neither line hands
        # in a manual flag, so the tag governs (IAS 1.82A):
        #   oci_will_reclassify = 8000
        #   oci_no_reclassify   = 5000
        #   total_oci           = 8000 + 5000  = 13000
        #   TCI                 = 100000 + 13000 = 113000
        acc_cta = self._ensure_account(
            self.env, '3910', 'FX Translation Reserve', 'equity')
        acc_fvoci_eq = self._ensure_account(
            self.env, '3920', 'FVOCI Equity Reserve', 'equity')
        acc_cta.tag_ids = [(4, self.tag_recyclable.id)]
        acc_fvoci_eq.tag_ids = [(4, self.tag_non_recyclable.id)]
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
            'line_ids': [
                (0, 0, {'name': 'CTA movement', 'oci_type': 'translation',
                        'account_id': acc_cta.id, 'amount': 8000.0}),
                (0, 0, {'name': 'FVOCI equity gain', 'oci_type': 'fvoci',
                        'account_id': acc_fvoci_eq.id, 'amount': 5000.0}),
            ],
        })
        cta_line = soci.line_ids.filtered(
            lambda l: l.account_id == acc_cta)
        eq_line = soci.line_ids.filtered(
            lambda l: l.account_id == acc_fvoci_eq)
        self.assertTrue(
            cta_line.will_reclassify,
            "CTA line must land in the recyclable section: its source "
            "account carries the EH OCI Recyclable tag (IAS 21.48).")
        self.assertFalse(
            eq_line.will_reclassify,
            "FVOCI-equity line must land in the non-recyclable section: "
            "its source account carries the EH OCI Non-Recyclable tag "
            "(IFRS 9.B5.7.1).")
        self.assertEqual(cta_line.tag_reclassify, 'recyclable')
        self.assertEqual(eq_line.tag_reclassify, 'non_recyclable')
        self.assertFalse(cta_line.reclassify_discrepancy)
        self.assertFalse(eq_line.reclassify_discrepancy)
        self.assertAlmostEqual(soci.oci_will_reclassify, 8000.0, places=2)
        self.assertAlmostEqual(soci.oci_no_reclassify, 5000.0, places=2)
        self.assertAlmostEqual(soci.total_oci, 13000.0, places=2)
        self.assertAlmostEqual(
            soci.total_comprehensive_income, 113000.0, places=2)

    def test_recycling_cta_recyclable_revaluation_non_recyclable(self):
        # DECISIVE GOLDEN (finding a): the reclassification section of each
        # OCI component is driven PURELY by the recycling tag on its source
        # account, with NO manual will_reclassify handed in on either line.
        #   - CTA reserve account carries EH OCI Recyclable (IAS 21.48): its
        #     +12000 translation line lands in the recyclable section.
        #   - Revaluation surplus account carries EH OCI Non-Recyclable
        #     (IAS 16.41): its +7000 line lands in the non-recyclable section.
        # Derivation:
        #   oci_will_reclassify = 12000 (CTA only)
        #   oci_no_reclassify   =  7000 (revaluation surplus only)
        #   total_oci           = 12000 + 7000 = 19000
        #   TCI                 = 90000 + 19000 = 109000
        acc_cta = self._ensure_account(
            self.env, '3911', 'Foreign Translation Reserve', 'equity')
        acc_reval = self._ensure_account(
            self.env, '3931', 'Asset Revaluation Surplus', 'equity')
        acc_cta.tag_ids = [(4, self.tag_recyclable.id)]
        acc_reval.tag_ids = [(4, self.tag_non_recyclable.id)]
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 90000.0,
            'line_ids': [
                # NOTE: neither line sets will_reclassify; the tag governs.
                (0, 0, {'name': 'CTA movement', 'oci_type': 'translation',
                        'account_id': acc_cta.id, 'amount': 12000.0}),
                (0, 0, {'name': 'Revaluation uplift',
                        'oci_type': 'revaluation',
                        'account_id': acc_reval.id, 'amount': 7000.0}),
            ],
        })
        cta_line = soci.line_ids.filtered(lambda l: l.account_id == acc_cta)
        reval_line = soci.line_ids.filtered(
            lambda l: l.account_id == acc_reval)
        # Section placement is tag-derived, not from any manual input.
        self.assertEqual(cta_line.tag_reclassify, 'recyclable')
        self.assertEqual(reval_line.tag_reclassify, 'non_recyclable')
        self.assertTrue(
            cta_line.will_reclassify,
            "CTA (recyclable tag) must derive into the recyclable section "
            "with no manual flag (IAS 21.48).")
        self.assertFalse(
            reval_line.will_reclassify,
            "Revaluation surplus (non-recyclable tag) must derive into the "
            "non-recyclable section with no manual flag (IAS 16.41).")
        # No discrepancy: the derived flag agrees with each tag.
        self.assertFalse(cta_line.reclassify_discrepancy)
        self.assertFalse(reval_line.reclassify_discrepancy)
        self.assertEqual(soci.recycling_discrepancy_count, 0)
        # No untagged component: the IAS 1.82A completeness gate is clear.
        self.assertEqual(soci.oci_untagged_count, 0)
        self.assertFalse(soci.oci_recycling_misfit_note)
        # Subtotals split by tag, hand-derived above.
        self.assertAlmostEqual(soci.oci_will_reclassify, 12000.0, places=2)
        self.assertAlmostEqual(soci.oci_no_reclassify, 7000.0, places=2)
        self.assertAlmostEqual(soci.total_oci, 19000.0, places=2)
        self.assertAlmostEqual(
            soci.total_comprehensive_income, 109000.0, places=2)
        # A fully tag-driven statement confirms with no override needed.
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')

    def test_recycling_tag_contradiction_sets_discrepancy_field(self):
        # DECISIVE GOLDEN (finding c): a manual will_reclassify that
        # CONTRADICTS the tag sets the discrepancy field on the line and the
        # header count, with no other input. The CTA account is tagged
        # RECYCLABLE, so the tag verdict is recyclable (True); the preparer
        # forces will_reclassify=False, contradicting it.
        acc_cta = self._ensure_account(
            self.env, '3912', 'CTA Reserve Contradiction', 'equity')
        acc_cta.tag_ids = [(4, self.tag_recyclable.id)]
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 60000.0,
            'line_ids': [
                # Manual flag False contradicts the recyclable tag.
                (0, 0, {'name': 'CTA movement', 'oci_type': 'translation',
                        'account_id': acc_cta.id, 'amount': 9000.0,
                        'will_reclassify': False}),
            ],
        })
        line = soci.line_ids
        self.assertEqual(line.tag_reclassify, 'recyclable')
        self.assertFalse(
            line.will_reclassify,
            "The manual False flag must be kept (override wins the value).")
        self.assertTrue(
            line.reclassify_discrepancy,
            "A manual flag contradicting the tag must set the discrepancy "
            "field (IAS 1.82A), never silently win.")
        self.assertEqual(soci.recycling_discrepancy_count, 1)
        # The tagged line is NOT an IAS 1.82A completeness misfit (it has a
        # tag); the discrepancy is a separate, softer signal (chatter on
        # confirm, not a block).
        self.assertEqual(soci.oci_untagged_count, 0)
        # Overridden into the non-recyclable bucket: 9000 no-reclass, 0 recl.
        self.assertAlmostEqual(soci.oci_no_reclassify, 9000.0, places=2)
        self.assertAlmostEqual(soci.oci_will_reclassify, 0.0, places=2)

    def test_untagged_oci_component_blocks_confirm_until_override(self):
        # DECISIVE GOLDEN (finding b): an OCI component whose source account
        # carries NO recycling tag has no tag-derived reclassification
        # section, so its placement rests on a manual flag (honour system).
        # It must surface in the IAS 1.82A completeness note and BLOCK confirm
        # until a manager overrides with a reason.
        acc_untagged = self._ensure_account(
            self.env, '3960', 'Untagged OCI Reserve', 'equity')
        # Deliberately NOT tagged with either recycling tag.
        self.assertFalse(acc_untagged.tag_ids & (
            self.tag_recyclable + self.tag_non_recyclable))
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 70000.0,
            'line_ids': [
                (0, 0, {'name': 'Untagged reserve movement',
                        'oci_type': 'other',
                        'account_id': acc_untagged.id, 'amount': 6000.0,
                        'will_reclassify': True}),
            ],
        })
        line = soci.line_ids
        # The line is a recycling misfit: no verdict, surfaced in the note.
        self.assertIsNone(line._eh_tag_verdict())
        self.assertEqual(line.tag_reclassify, 'none')
        self.assertEqual(soci.oci_untagged_count, 1)
        self.assertTrue(soci.oci_recycling_misfit_note)
        self.assertIn('3960', soci.oci_recycling_misfit_note)
        # Blocked without the override.
        with self.assertRaises(UserError):
            soci.action_confirm()
        self.assertEqual(soci.state, 'draft')
        # Override flag alone (no reason) is still blocked.
        soci.oci_tag_override = True
        with self.assertRaises(UserError):
            soci.action_confirm()
        self.assertEqual(soci.state, 'draft')
        # Override WITH a reason confirms, and the override is logged.
        soci.oci_tag_override_reason = (
            'Bespoke reserve; recycling classified manually per note 6.')
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')
        self.assertTrue(
            any('IAS 1.82A' in (m.body or '') and 'OVERRIDDEN' in (m.body or '')
                for m in soci.message_ids),
            "The IAS 1.82A OCI recycling override must be logged to the "
            "chatter.")

    def test_untagged_oci_completeness_ignores_zero_amount_lines(self):
        # A zero-amount OCI line cannot mis-state either subtotal, so it is
        # not a completeness misfit even with an untagged / absent source
        # account: the gate stays silent and the statement confirms.
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 50000.0,
            'line_ids': [
                (0, 0, {'name': 'Nil placeholder', 'oci_type': 'other',
                        'amount': 0.0}),
            ],
        })
        self.assertEqual(soci.oci_untagged_count, 0)
        self.assertFalse(soci.oci_recycling_misfit_note)
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')

    def test_recycling_manual_override_flags_discrepancy(self):
        # A preparer may override the structural classification, but never
        # silently: the discrepancy is flagged on the line, counted on the
        # header, and recorded in the chatter on confirm.
        acc_cta = self._ensure_account(
            self.env, '3910', 'FX Translation Reserve', 'equity')
        acc_cta.tag_ids = [(4, self.tag_recyclable.id)]
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 50000.0,
            'line_ids': [
                (0, 0, {'name': 'CTA movement', 'oci_type': 'translation',
                        'account_id': acc_cta.id, 'amount': 8000.0}),
            ],
        })
        line = soci.line_ids
        self.assertTrue(line.will_reclassify)
        # Manual override against the tag.
        line.will_reclassify = False
        self.assertTrue(
            line.reclassify_discrepancy,
            "Overriding the flag against the account tag must raise the "
            "discrepancy flag.")
        self.assertEqual(soci.recycling_discrepancy_count, 1)
        # The overridden line now sums in the non-recyclable bucket:
        # oci_no_reclassify = 8000, oci_will_reclassify = 0.
        self.assertAlmostEqual(soci.oci_no_reclassify, 8000.0, places=2)
        self.assertAlmostEqual(soci.oci_will_reclassify, 0.0, places=2)
        # Confirm succeeds (warning, not a block) and records the override.
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')
        self.assertTrue(
            any('IAS 1.82A' in (m.body or '') for m in soci.message_ids),
            "Confirming with a recycling override must record the "
            "overridden lines in the chatter.")

    def test_default_tag_applier_uses_explicit_sources_and_no_overwrite(self):
        # Free-text account names are not configuration. Even an English
        # revaluation-surplus name must remain untouched until a preparer
        # assigns a tag or an installed suite model exposes an explicit
        # account setting. Creating/opening a statement also never mutates CoA.
        acc_surplus = self._ensure_account(
            self.env, '3930', 'Revaluation Surplus', 'equity')
        # Never-overwrite rule: an account already classified (here, hand
        # tagged recyclable against the default) must be left alone.
        acc_pretagged = self._ensure_account(
            self.env, '3940', 'Revaluation Surplus Special', 'equity')
        acc_pretagged.tag_ids = [(4, self.tag_recyclable.id)]
        self.assertFalse(acc_surplus.tag_ids & (
            self.tag_recyclable + self.tag_non_recyclable))
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
        })
        self.assertFalse(
            acc_surplus.tag_ids & (
                self.tag_recyclable + self.tag_non_recyclable),
            "Statement creation must not mutate chart classification.")
        soci.action_apply_oci_recycling_tags()
        self.assertFalse(
            acc_surplus.tag_ids & (
                self.tag_recyclable + self.tag_non_recyclable),
            "The tag action must not infer classification from an English "
            "or translated account name.")
        self.assertIn(self.tag_recyclable, acc_pretagged.tag_ids)
        self.assertNotIn(
            self.tag_non_recyclable, acc_pretagged.tag_ids,
            "The applier must never re-tag an account that already "
            "carries a recycling tag (manual classification wins).")
        # Config-field source (soft): when the consolidation module is
        # installed, its entity-level CTA account is tagged RECYCLABLE
        # (IAS 21.48) by the runnable action.
        if 'eh.consol.entity' in self.env.registry:
            acc_group_cta = self._ensure_account(
                self.env, '3950', 'Group Translation Adjustment', 'equity')
            self.env['eh.consol.entity'].create({
                'name': 'Tag Source Group', 'code': 'TSG1',
                'cta_account_id': acc_group_cta.id,
            })
            soci.action_apply_oci_recycling_tags()
            self.assertIn(
                self.tag_recyclable, acc_group_cta.tag_ids,
                "The applier must tag the consolidation entity's CTA "
                "reserve recyclable (IAS 21.48).")

    # ---- IAS 1.60 completeness guard ----

    def _post_off_balance_pair(self, date='2026-03-15'):
        """Post a balanced move carried entirely on off-balance accounts,
        the account type outside every recognised IAS 1.60 set. Both
        accounts end with a non-zero balance (500 / -500)."""
        off_one = self._ensure_account(
            self.env, '9901', 'Off Balance Memo One', 'off_balance')
        off_two = self._ensure_account(
            self.env, '9902', 'Off Balance Memo Two', 'off_balance')
        self.post_balanced_move(
            [
                {'account': off_one, 'debit': 500.0},
                {'account': off_two, 'credit': 500.0},
            ],
            date=date,
        )
        return off_one, off_two

    def test_soci_completeness_guard_ignores_off_balance_memo_accounts(self):
        off_one, _off_two = self._post_off_balance_pair()
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
        })
        self.assertFalse(soci.classification_misfit_note)
        self.assertNotIn('9901', soci.classification_misfit_note or '')
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')

    def test_soce_completeness_guard_ignores_off_balance_memo_accounts(self):
        self._post_off_balance_pair()
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
        })
        self.assertFalse(soce.classification_misfit_note)
        self.assertNotIn('9902', soce.classification_misfit_note or '')
        soce.action_confirm()
        self.assertEqual(soce.state, 'confirmed')

    def test_completeness_guard_ignores_recognised_types(self):
        # Balances on recognised types only (cash / revenue / expense):
        # no misfits, and the guard stays silent - prior behaviour intact.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 25000.0},
                {'account': self.account_expense, 'debit': 15000.0},
                {'account': self.account_revenue, 'credit': 40000.0},
            ],
            date='2026-06-30',
        )
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 25000.0,
        })
        self.assertFalse(soci.classification_misfit_note)
        soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')

    # ---- NCI prefill from a covering consolidation run ----

    def _ensure_unaffected_earnings_account(self, env, code, name):
        """Reuse a chart's unique current-year earnings account when set.

        Odoo 16 enforces one ``equity_unaffected`` account per company and a
        full-suite chart may already contain its localisation-provided row.
        The focused statements fixture must therefore discover that semantic
        account before falling back to creating one with its deterministic
        test code.
        """
        Account = env['account.account']
        company_field = (
            'company_ids' if 'company_ids' in Account._fields
            else 'company_id'
        )
        account = Account.search([
            ('account_type', '=', 'equity_unaffected'),
            (company_field, 'in', env.company.ids),
        ], limit=1)
        return account or self._ensure_account(
            env, code, name, 'equity_unaffected',
        )

    def _seed_consol_run(self, state='reviewed', period_profit=2500.0,
                         period_oci=500.0):
        """Build real NCI stock, then review a distinct period-flow output."""
        sub = self.env['res.company'].create({
            'name': 'Statements NCI Sub',
            'currency_id': self.company.currency_id.id,
        })
        self.source_manager.sudo().write({
            'company_ids': [(4, sub.id)],
        })
        sub_env = self.env['account.account'].with_company(sub).env
        sub_cash = self._ensure_account(
            sub_env, 'ST1000', 'Statements Sub Cash', 'asset_cash')
        sub_equity = self._ensure_account(
            sub_env, 'ST3000', 'Statements Sub Equity', 'equity')
        sub_retained = self._ensure_unaffected_earnings_account(
            sub_env, 'ST3999', 'Statements Sub Retained Earnings')
        sub_journal = self._ensure_journal(
            sub_env, sub, 'general', 'STMT', 'Statements NCI Opening')
        opening = sub_env['account.move'].create({
            'move_type': 'entry',
            'journal_id': sub_journal.id,
            'date': '2025-12-31',
            'line_ids': [
                (0, 0, {'account_id': sub_cash.id, 'debit': 100000.0}),
                (0, 0, {'account_id': sub_equity.id, 'credit': 100000.0}),
            ],
        })
        opening.action_post()
        parent_nci = self._ensure_account(
            self.env, 'ST3900', 'Statements NCI Equity', 'equity')
        parent_retained = self._ensure_unaffected_earnings_account(
            self.env, 'ST3999', 'Statements Parent Retained Earnings')
        entity = self.env['eh.consol.entity'].create({
            'name': 'Statements Group', 'code': 'STGRP',
            'parent_retained_earnings_account_id': parent_retained.id,
        })
        self.env['eh.consol.member'].create({
            'entity_id': entity.id,
            'company_id': sub.id,
            'ownership_pct': 80.0,
            'nci_account_id': parent_nci.id,
            'equity_elimination_account_id': self.account_equity.id,
            'retained_earnings_account_id': sub_retained.id,
        })
        run = self.env['eh.consol.run'].create({
            'entity_id': entity.id,
            'period_from': '2026-01-01', 'period_to': '2026-12-31',
        })
        actor_run = run.with_user(self.source_manager)
        actor_run.action_compute()
        if state == 'computed':
            return run
        actor_run.write({
            'nci_period_profit_input': period_profit,
            'nci_period_oci_input': period_oci,
            'nci_period_flow_confirmed': True,
            'nci_period_flow_basis': 'Reviewed NCI attribution schedule NCI-1',
        })
        actor_run.action_review()
        if state == 'closed':
            actor_run.action_close()
        return run

    def test_soci_nci_prefill_and_discrepancy(self):
        if 'eh.consol.run' not in self.env.registry:
            self.skipTest('eh_account_consolidation not installed')
        run = self._seed_consol_run()
        # Closing stock is 20,000; reviewed period TCI flow is only 3,000.
        self.assertAlmostEqual(run.nci_amount, -20000.0, places=2)
        self.assertAlmostEqual(
            run.nci_period_comprehensive_income_amount, 3000.0, places=2)
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
        })
        soci.with_user(
            self.source_manager
        ).action_prefill_nci_from_consolidation()
        # Flow = 2500 profit + 500 OCI. Closing stock must not leak here.
        self.assertTrue(soci.consol_nci_available)
        self.assertEqual(soci.consol_run_name, run.name)
        self.assertEqual(soci.consol_run_state, 'reviewed')
        self.assertEqual(soci.consol_period_start.isoformat(), '2026-01-01')
        self.assertEqual(soci.consol_currency_id, self.company.currency_id)
        self.assertAlmostEqual(soci.consol_nci_amount, 3000.0, places=2)
        self.assertAlmostEqual(
            soci.attributable_to_nci, 3000.0, places=2,
            msg="Blank NCI attribution must prefill period flow, not stock.")
        self.assertTrue(soci.nci_consol_tied)
        self.assertAlmostEqual(soci.nci_consol_discrepancy, 0.0, places=2)
        # Manual divergence: 2000 - 3000 = -1000 discrepancy, untied.
        soci.attributable_to_nci = 2000.0
        self.assertAlmostEqual(
            soci.nci_consol_discrepancy, -1000.0, places=2)
        self.assertFalse(soci.nci_consol_tied)
        with self.assertRaisesRegex(
            UserError, 'does not match the reviewed consolidation'
        ):
            soci.with_user(self.source_manager).action_confirm()

    def test_soci_nci_prefill_keeps_manual_figure(self):
        if 'eh.consol.run' not in self.env.registry:
            self.skipTest('eh_account_consolidation not installed')
        self._seed_consol_run()
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 100000.0,
            'attributable_to_nci': 2000.0,
        })
        soci.with_user(
            self.source_manager
        ).action_prefill_nci_from_consolidation()
        # Manually keyed 2000 stays; reference is reviewed 3000 flow.
        self.assertAlmostEqual(
            soci.attributable_to_nci, 2000.0, places=2,
            msg="Prefill must never overwrite a manually keyed NCI figure.")
        self.assertAlmostEqual(soci.consol_nci_amount, 3000.0, places=2)
        self.assertAlmostEqual(
            soci.nci_consol_discrepancy, -1000.0, places=2)
        self.assertFalse(soci.nci_consol_tied)

    def test_soce_nci_prefill_creates_component_and_discrepancy(self):
        if 'eh.consol.run' not in self.env.registry:
            self.skipTest('eh_account_consolidation not installed')
        self._seed_consol_run()
        soce = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
        })
        soce.with_user(
            self.source_manager
        ).action_prefill_nci_from_consolidation()
        nci_line = soce.line_ids.filtered(lambda l: l.component == 'nci')
        self.assertEqual(
            len(nci_line), 1,
            "Prefill must create the missing NCI component line.")
        # Closing stock is reference only. It must not be mislabeled opening.
        self.assertAlmostEqual(nci_line.opening_balance, 0.0, places=2)
        self.assertAlmostEqual(nci_line.closing_balance, 0.0, places=2)
        self.assertAlmostEqual(soce.consol_nci_amount, 20000.0, places=2)
        self.assertAlmostEqual(
            soce.nci_consol_discrepancy, -20000.0, places=2)
        self.assertFalse(soce.nci_consol_tied)
        with self.assertRaisesRegex(
            UserError, 'NCI closing equity does not match'
        ):
            soce.with_user(self.source_manager).action_confirm()

    def test_unsigned_computed_run_cannot_source_soci(self):
        if 'eh.consol.run' not in self.env.registry:
            self.skipTest('eh_account_consolidation not installed')
        run = self._seed_consol_run(state='computed')
        self.assertEqual(run.state, 'computed')
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
        })
        with self.assertRaisesRegex(UserError, 'reviewed/closed'):
            soci.with_user(
                self.source_manager
            ).action_prefill_nci_from_consolidation()
        self.assertFalse(soci.consol_nci_available)

    def test_source_reset_after_prefill_blocks_statement_confirm(self):
        if 'eh.consol.run' not in self.env.registry:
            self.skipTest('eh_account_consolidation not installed')
        run = self._seed_consol_run()
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
        })
        actor_soci = soci.with_user(self.source_manager)
        actor_soci.action_prefill_nci_from_consolidation()
        run.with_user(self.source_manager).action_reset_to_draft()
        with self.assertRaisesRegex(UserError, 'manager-reviewed or closed'):
            actor_soci.action_confirm()

    def test_confirmed_statement_prevents_source_evidence_reset(self):
        if 'eh.consol.run' not in self.env.registry:
            self.skipTest('eh_account_consolidation not installed')
        run = self._seed_consol_run()
        soci = self.env['eh.soci'].create({
            'period_start': '2026-01-01', 'period_end': '2026-12-31',
            'profit_for_period': 10000.0,
            'attributable_to_owners': 7000.0,
        })
        actor_soci = soci.with_user(self.source_manager)
        actor_soci.action_prefill_nci_from_consolidation()
        actor_soci.action_confirm()
        self.assertEqual(soci.state, 'confirmed')
        with self.assertRaisesRegex(
            UserError, 'cited by confirmed primary statement'
        ):
            run.with_user(self.source_manager).action_reset_to_draft()
        actor_soci.action_set_to_draft()
        run.with_user(self.source_manager).action_reset_to_draft()
        self.assertEqual(run.state, 'draft')

    # ---- IAS 34 thin interim support ----

    def test_interim_labels_and_comparatives(self):
        Soci = self.env['eh.soci']
        annual_2025 = Soci.create({
            'period_start': '2025-01-01', 'period_end': '2025-12-31',
            'profit_for_period': 80000.0,
        })
        # Default preserves prior behaviour: annual, plain heading.
        self.assertEqual(annual_2025.period_type, 'annual')
        self.assertEqual(
            annual_2025.presentation_label,
            'Statement of comprehensive income')
        interim_2025 = Soci.create({
            'period_start': '2025-01-01', 'period_end': '2025-06-30',
            'period_type': 'interim',
            'profit_for_period': 30000.0,
        })
        self.assertEqual(
            interim_2025.presentation_label,
            'Interim statement of comprehensive income (IAS 34)')
        # Current condensed H1-2026 interim: IAS 34.20 comparatives are the
        # comparable prior-year interim plus the preceding annual period.
        interim_2026 = Soci.create({
            'period_start': '2026-01-01', 'period_end': '2026-06-30',
            'period_type': 'interim',
            'condensed': True,
            'comparative_interim_id': interim_2025.id,
            'comparative_annual_id': annual_2025.id,
            'profit_for_period': 40000.0,
        })
        self.assertEqual(
            interim_2026.presentation_label,
            'Condensed interim statement of comprehensive income '
            '(IAS 34.8)')
        self.assertEqual(
            interim_2026.comparative_interim_id, interim_2025)
        self.assertEqual(
            interim_2026.comparative_annual_id, annual_2025)
        # SOCE mirrors the labelling.
        soce_interim = self.env['eh.soce'].create({
            'period_start': '2026-01-01', 'period_end': '2026-06-30',
            'period_type': 'interim', 'condensed': True,
        })
        self.assertEqual(
            soce_interim.presentation_label,
            'Condensed interim statement of changes in equity (IAS 34.8)')

    def test_interim_field_validation(self):
        Soci = self.env['eh.soci']
        annual_2025 = Soci.create({
            'period_start': '2025-01-01', 'period_end': '2025-12-31',
        })
        interim_2025 = Soci.create({
            'period_start': '2025-01-01', 'period_end': '2025-06-30',
            'period_type': 'interim',
        })
        # Condensed / comparatives are interim-only (IAS 34.8 / 34.20).
        with self.assertRaises(ValidationError):
            Soci.create({
                'period_start': '2026-01-01', 'period_end': '2026-12-31',
                'condensed': True,
            })
        # The prior-annual comparative must be an annual statement.
        with self.assertRaises(ValidationError):
            Soci.create({
                'period_start': '2026-01-01', 'period_end': '2026-06-30',
                'period_type': 'interim',
                'comparative_annual_id': interim_2025.id,
            })
        # A comparative must end before the statement period starts.
        with self.assertRaises(ValidationError):
            Soci.create({
                'period_start': '2025-04-01', 'period_end': '2025-09-30',
                'period_type': 'interim',
                'comparative_interim_id': interim_2025.id,
            })
        # And the well-formed pair passes.
        ok = Soci.create({
            'period_start': '2026-01-01', 'period_end': '2026-06-30',
            'period_type': 'interim',
            'comparative_interim_id': interim_2025.id,
            'comparative_annual_id': annual_2025.id,
        })
        self.assertEqual(ok.period_type, 'interim')

    def test_recognised_type_sets_are_disjoint_and_complete(self):
        # The IAS 1.60 sets must stay disjoint (an account type can only
        # classify one way) and must cover every non-off-balance type the
        # framework ships, so the guard never misfires on a stock chart.
        sets = [
            presentation.CURRENT_ACCOUNT_TYPES,
            presentation.NON_CURRENT_ACCOUNT_TYPES,
            presentation.EQUITY_ACCOUNT_TYPES,
            presentation.PL_ACCOUNT_TYPES,
        ]
        union = set()
        total = 0
        for s in sets:
            union |= s
            total += len(s)
        self.assertEqual(
            len(union), total,
            "The IAS 1.60 classification sets must be disjoint.")
        selection = dict(
            self.env['account.account']._fields['account_type'].selection)
        framework_types = set(selection) - {'off_balance'}
        self.assertFalse(
            framework_types - union,
            "Every framework account type except off_balance must belong "
            "to a recognised IAS 1.60 set: missing %s" % sorted(
                framework_types - union))
