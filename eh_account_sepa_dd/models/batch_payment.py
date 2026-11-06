# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Extend eh.batch.payment with a SEPA Direct Debit export action.

Inbound batches collect from customer accounts; the export consumes
each payment's matching mandate (advancing the FRST -> RCUR counter
atomically), assembles the PAIN.008 input dict grouped by sequence
type, and renders one file per sequence type.

Why one file per sequence type rather than one big mixed file: SEPA
banks process the file as a batch and some refuse mixed-sequence
files. Splitting per sequence type is the safe path; the export action
generates and stores them as separate eh.sepa.dd.export rows so the
audit trail records exactly what was submitted.
"""

import base64
import hashlib
import hmac
import json
import logging
import uuid
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from odoo.addons.eh_account_sepa_dd.tools import pain_008
from odoo.addons.eh_account_sepa_ct.tools.sepa_charset import (
    sanitize_sepa_text,
)
from odoo.addons.eh_account_sepa_ct.tools.iban_validator import (
    validate_iban, IbanValidationError,
)
from odoo.addons.eh_account_sepa_ct.tools.bic_validator import (
    validate_bic, BicValidationError,
)
from odoo.addons.eh_account_batch_payment.models.batch_payment import (
    POSTED_STATES,
)

from .sepa_mandate import (
    _SEPA_DD_CONSUME_CTX,
    _SEPA_DD_EXPIRY_PREFLIGHT_CTX,
)
from .sepa_creditor import normalize_creditor_identifier

_logger = logging.getLogger(__name__)

_DD_CONSUMPTION_FIELDS = frozenset({
    'eh_sepa_dd_mandate_id',
    'eh_sepa_dd_sequence_type',
    'eh_sepa_dd_local_instrument',
    'eh_sepa_dd_amendment_id',
})
_DD_CONSUMPTION_CONTEXT = 'eh_authorized_sepa_dd_consumption_snapshot'
_DD_SOURCE_FINGERPRINT_FIELD = 'sepa_dd_source_fingerprint'
_DD_SOURCE_FINGERPRINT_CONTEXT = 'eh_authorized_sepa_dd_source_fingerprint'
_DD_SOURCE_FINGERPRINT_CAPABILITY = object()
_DD_SOURCE_APPROVAL_CONTEXT = 'eh_authorized_sepa_dd_source_approval'
_DD_SOURCE_APPROVAL_CAPABILITY = object()
_DD_SERVER_OWNED_FIELDS = frozenset({
    _DD_SOURCE_FINGERPRINT_FIELD,
    'sepa_dd_source_approved_at',
    'sepa_dd_source_approved_by_id',
})


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    # A re-cut must reproduce the sequence that was consumed by the first
    # file; resolving the mandate again would turn FRST into RCUR (and makes a
    # completed OOFF mandate impossible to re-cut).  Persist the server-owned
    # consumption decision on the immutable posted payment.
    eh_sepa_dd_mandate_id = fields.Many2one(
        'eh.sepa.mandate', copy=False, readonly=True, ondelete='restrict',
    )
    eh_sepa_dd_sequence_type = fields.Selection(
        [
            ('FRST', "First"),
            ('RCUR', "Recurring"),
            ('FNAL', "Final"),
            ('OOFF', "One-off"),
        ],
        copy=False, readonly=True,
    )
    eh_sepa_dd_local_instrument = fields.Selection(
        [
            ('CORE', "CORE"),
            ('B2B', "B2B"),
        ],
        copy=False, readonly=True,
    )
    eh_sepa_dd_final_collection = fields.Boolean(
        string="Final collection (FNAL)",
        copy=False,
        help=(
            "Render this recurring mandate collection as FNAL and complete "
            "the mandate. Set before confirming the batch."
        ),
    )
    eh_sepa_dd_debtor_bank_id = fields.Many2one(
        'res.partner.bank',
        string="SEPA debtor bank account",
        copy=False,
        check_company=True,
        domain="[('partner_id', '=', partner_id)]",
        help=(
            "Debtor IBAN used to select one exact active mandate. This is "
            "separate from Odoo's inbound payment destination bank account."
        ),
    )
    eh_sepa_dd_amendment_id = fields.Many2one(
        'eh.sepa.mandate.amendment', copy=False, readonly=True,
        ondelete='restrict',
        help="Exact one-time mandate amendment rendered by this collection; "
             "retained so an explicit file re-cut is byte-semantically stable.")

    @api.model_create_multi
    def create(self, vals_list):
        if any(_DD_CONSUMPTION_FIELDS.intersection(vals) for vals in vals_list):
            if not (
                self.env.su
                and self.env.context.get(_DD_CONSUMPTION_CONTEXT)
            ):
                raise AccessError(_(
                    "SEPA Direct Debit consumption evidence is "
                    "generator-owned and cannot be supplied by a client."
                ))
        return super().create(vals_list)

    def write(self, vals):
        if _DD_CONSUMPTION_FIELDS.intersection(vals) and not (
            self.env.su
            and self.env.context.get(_DD_CONSUMPTION_CONTEXT)
        ):
            raise AccessError(_(
                "SEPA Direct Debit consumption evidence is immutable."
            ))
        editable_dd_fields = {
            'eh_sepa_dd_final_collection', 'eh_sepa_dd_debtor_bank_id',
        }
        if editable_dd_fields.intersection(vals) and any(
            payment.state in POSTED_STATES
            or (
                payment.eh_batch_payment_id
                and payment.eh_batch_payment_id.state != 'draft'
            )
            for payment in self
        ):
            raise AccessError(_(
                "SEPA debtor-bank and final-collection choices are frozen "
                "when their payment or batch leaves draft."
            ))
        return super().write(vals)


class EhBatchPayment(models.Model):
    _inherit = 'eh.batch.payment'

    sepa_dd_export_ids = fields.One2many(
        'eh.sepa.dd.export', 'batch_id', copy=False,
    )
    sepa_dd_export_count = fields.Integer(
        compute='_compute_sepa_dd_export_count',
    )
    sepa_dd_source_fingerprint = fields.Char(
        copy=False,
        readonly=True,
        groups='eh_account_base.group_eh_manager',
        help=(
            "SHA-256 of the approved creditor and mandate payload sources. "
            "Export recomputes it under row locks and blocks post-approval "
            "configuration or mandate drift."
        ),
    )
    sepa_dd_collection = fields.Boolean(
        string="SEPA Direct Debit Collection", default=False, tracking=True,
        help="Explicitly opt this inbound batch into mandate validation and "
             "PAIN.008 export. Ordinary inbound receipt batches remain "
             "unaffected even when their journal has a SEPA creditor.")
    sepa_dd_source_approval_ids = fields.One2many(
        'eh.sepa.dd.source.approval', 'batch_id', readonly=True, copy=False)
    sepa_dd_source_approved_at = fields.Datetime(readonly=True, copy=False)
    sepa_dd_source_approved_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False)
    sepa_dd_reapproval_reason = fields.Text(
        copy=False,
        help="Manager explanation required to approve changed creditor, "
             "mandate, or collection-sequence sources on a posted batch.")

    @api.model_create_multi
    def create(self, vals_list):
        if any(_DD_SERVER_OWNED_FIELDS.intersection(vals) for vals in vals_list):
            if not (
                self.env.su
                and self.env.context.get(_DD_SOURCE_FINGERPRINT_CONTEXT)
                is _DD_SOURCE_FINGERPRINT_CAPABILITY
            ):
                raise AccessError(_(
                    "SEPA Direct Debit approval fingerprints are "
                    "server-owned and cannot be supplied by a client."
                ))
        return super().create(vals_list)

    def write(self, vals):
        if _DD_SERVER_OWNED_FIELDS.intersection(vals) and not (
            self.env.su
            and self.env.context.get(_DD_SOURCE_FINGERPRINT_CONTEXT)
            is _DD_SOURCE_FINGERPRINT_CAPABILITY
        ):
            raise AccessError(_(
                "SEPA Direct Debit approval fingerprints are immutable."
            ))
        if 'sepa_dd_collection' in vals and any(
                batch.state != 'draft' for batch in self):
            raise AccessError(_(
                "The SEPA Direct Debit designation is frozen after "
                "confirmation. Return an unposted batch to draft through its "
                "workflow; posted batches retain their approved rail."
            ))
        return super().write(vals)

    def action_confirm(self):
        """Seal mutable creditor/mandate payload inputs at approval time."""
        with self.env.cr.savepoint():
            result = super().action_confirm()
            for batch in self.filtered(lambda rec: rec.batch_type == 'inbound'):
                creditor = (
                    batch._eh_dd_creditor()
                    if batch.sepa_dd_collection else batch.env['eh.sepa.creditor']
                )
                fingerprint = False
                if creditor:
                    fingerprint, _mandates = (
                        batch._eh_dd_source_fingerprint(
                            creditor, lock=True,
                        )
                    )
                batch.sudo().with_context(
                    **{
                        _DD_SOURCE_FINGERPRINT_CONTEXT:
                            _DD_SOURCE_FINGERPRINT_CAPABILITY,
                    }
                ).write({
                    _DD_SOURCE_FINGERPRINT_FIELD: fingerprint,
                    'sepa_dd_source_approved_at': fields.Datetime.now()
                        if fingerprint else False,
                    'sepa_dd_source_approved_by_id': self.env.user.id
                        if fingerprint else False,
                })
                if fingerprint:
                    batch._eh_record_dd_source_approval(
                        previous_fingerprint=False,
                        fingerprint=fingerprint,
                        reason=_("Captured during batch confirmation."),
                    )
            return result

    @api.depends('sepa_dd_export_ids', 'sepa_dd_export_ids.state')
    def _compute_sepa_dd_export_count(self):
        for batch in self:
            batch.sepa_dd_export_count = len(
                batch.sepa_dd_export_ids.filtered(
                    lambda e: e.state in ('generated', 'downloaded'),
                ),
            )

    def action_view_sepa_dd_exports(self):
        self.ensure_one()
        action = self.env['ir.actions.actions']._for_xml_id(
            'eh_account_sepa_dd.action_eh_sepa_dd_export',
        )
        action['domain'] = [('batch_id', '=', self.id)]
        action['context'] = {
            'search_default_filter_generated': 0,
            'search_default_filter_downloaded': 0,
        }
        return action

    def _eh_dd_creditor(self):
        self.ensure_one()
        return self.env['eh.sepa.creditor'].search(
            [
                ('journal_id', '=', self.journal_id.id),
                ('company_id', '=', self.company_id.id),
            ],
            limit=1,
        )

    def _eh_dd_mandates_by_payment(self, creditor):
        """Resolve the exact approved mandate for each batch payment.

        After a first cut, the immutable consumption snapshot is
        authoritative so completed OOFF/FNAL mandates remain verifiable and
        re-cuttable. Before a first cut, normal active-mandate resolution is
        used; a newly inserted or amended mandate after confirmation changes
        the fingerprint and blocks export.
        """
        self.ensure_one()
        result = {}
        for payment in self.payment_ids.sorted('id'):
            mandate = payment.eh_sepa_dd_mandate_id
            if not mandate:
                mandate = self._eh_resolve_mandate(payment, creditor)
            if (
                mandate.creditor_id != creditor
                or mandate.partner_id != payment.partner_id
            ):
                raise UserError(_(
                    "Payment %(payment)s mandate does not match its approved "
                    "creditor and debtor.",
                    payment=payment.display_name,
                ))
            mandate._eh_assert_scheme_eligibility()
            approved_sequence = payment.eh_sepa_dd_sequence_type
            if approved_sequence and mandate.state != 'active':
                # A first OOFF/FNAL cut legitimately completes its mandate;
                # an explicit void may then re-cut that exact consumed
                # instruction without consuming it again. Revoked/expired
                # recurring mandates must never ride an old payment snapshot
                # into a replacement bank file.
                completed_recut = (
                    mandate.state == 'completed'
                    and approved_sequence in ('OOFF', 'FNAL')
                    and mandate.last_collection_date == self.payment_date
                )
                if not completed_recut:
                    raise UserError(_(
                        "Payment %(payment)s mandate %(mandate)s is %(state)s. "
                        "A revoked, expired, or otherwise inactive mandate "
                        "cannot be used in a replacement bank file.",
                        payment=payment.display_name,
                        mandate=mandate.mandate_id,
                        state=mandate.state,
                    ))
            result[payment.id] = mandate
        return result

    def _eh_lock_dd_source_rows(self, creditor, mandates_by_payment):
        """Hold creditor, mandate/amendment and debtor-name sources stable."""
        self.ensure_one()
        mandates = self.env['eh.sepa.mandate'].browse(sorted({
            mandate.id for mandate in mandates_by_payment.values()
        }))
        self.env['eh.sepa.creditor'].flush_model()
        self.env['eh.sepa.mandate'].flush_model()
        self.env['eh.sepa.mandate.amendment'].flush_model()
        self.env['res.partner'].flush_model()
        self.env.cr.execute(
            "SELECT id FROM eh_sepa_creditor WHERE id = %s FOR SHARE",
            (creditor.id,),
        )
        if mandates:
            self.env.cr.execute(
                "SELECT id FROM eh_sepa_mandate WHERE id IN %s "
                # Export later advances the same rows. Acquire the exclusive
                # lock now instead of upgrading a shared lock after another
                # batch has done the same (a classic two-session deadlock).
                "ORDER BY id FOR UPDATE",
                (tuple(mandates.ids),),
            )
            self.env.cr.execute(
                "SELECT id FROM eh_sepa_mandate_amendment "
                "WHERE mandate_id IN %s ORDER BY id FOR UPDATE",
                (tuple(mandates.ids),),
            )
        partner_ids = sorted(set(
            self.payment_ids.mapped('partner_id').ids
            + mandates.mapped('partner_id').ids
        ))
        if partner_ids:
            self.env.cr.execute(
                "SELECT id FROM res_partner WHERE id IN %s "
                "ORDER BY id FOR SHARE",
                (tuple(partner_ids),),
            )
        debtor_bank_ids = tuple(sorted(set(
            self.payment_ids.mapped('eh_sepa_dd_debtor_bank_id').ids
        )))
        if debtor_bank_ids:
            self.env['res.partner.bank'].flush_model()
            self.env.cr.execute(
                "SELECT id FROM res_partner_bank WHERE id IN %s "
                "ORDER BY id FOR SHARE",
                (debtor_bank_ids,),
            )
        creditor.invalidate_recordset()
        mandates.invalidate_recordset()
        mandates.mapped('amendment_ids').invalidate_recordset()
        self.env['res.partner'].browse(partner_ids).invalidate_recordset()
        self.env['res.partner.bank'].browse(
            debtor_bank_ids,
        ).invalidate_recordset()
        self.payment_ids.invalidate_recordset()
        return mandates

    def _eh_dd_amendment_for_payment(self, payment, mandate):
        amendment = payment.eh_sepa_dd_amendment_id
        if amendment:
            if amendment.mandate_id != mandate or not amendment.engine_owned:
                raise UserError(_(
                    "Payment %(payment)s has invalid mandate-amendment "
                    "consumption evidence.",
                    payment=payment.display_name,
                ))
            return amendment
        return mandate._eh_pending_amendment_for_rendering()

    def _eh_dd_source_fingerprint(self, creditor, lock=False):
        """Hash every mutable value which can alter the PAIN.008 payload."""
        self.ensure_one()
        mandates_by_payment = self._eh_dd_mandates_by_payment(creditor)
        if lock:
            self._eh_lock_dd_source_rows(creditor, mandates_by_payment)
        source = {
            'creditor': {
                'id': creditor.id,
                'company_id': creditor.company_id.id,
                'journal_id': creditor.journal_id.id,
                'creditor_identifier': creditor.creditor_identifier or '',
                'creditor_name': creditor.creditor_name or '',
                'iban': creditor.iban or '',
                'bic': creditor.bic or '',
                'default_local_instrument': (
                    creditor.default_local_instrument or 'CORE'
                ),
                'pre_notification_days': int(
                    creditor.pre_notification_days or 0
                ),
                'batch_booking': bool(creditor.batch_booking),
            },
            'payments': [],
        }
        for payment in self.payment_ids.sorted('id'):
            mandate = mandates_by_payment[payment.id]
            amendment = self._eh_dd_amendment_for_payment(payment, mandate)
            approved_sequence_type = payment.eh_sepa_dd_sequence_type
            if not approved_sequence_type:
                if mandate.is_one_off:
                    approved_sequence_type = 'OOFF'
                elif mandate.collection_count == 0:
                    approved_sequence_type = 'FRST'
                else:
                    approved_sequence_type = 'RCUR'
                if payment.eh_sepa_dd_final_collection:
                    approved_sequence_type = 'FNAL'
            source['payments'].append({
                'payment_id': payment.id,
                'partner_id': payment.partner_id.id,
                'debtor_bank_id': payment.eh_sepa_dd_debtor_bank_id.id or 0,
                'debtor_bank_iban': (
                    payment.eh_sepa_dd_debtor_bank_id.acc_number or ''
                ),
                'mandate_record_id': mandate.id,
                'mandate_id': mandate.mandate_id or '',
                'creditor_id': mandate.creditor_id.id,
                'debtor_name': (
                    mandate.debtor_name
                    or payment.partner_id.display_name
                    or ''
                ),
                'debtor_iban': mandate.debtor_iban or '',
                'debtor_bic': mandate.debtor_bic or '',
                'signature_date': fields.Date.to_string(
                    mandate.signature_date,
                ),
                'is_one_off': bool(mandate.is_one_off),
                'b2b_bank_confirmed': bool(mandate.b2b_bank_confirmed),
                'approved_sequence_type': approved_sequence_type,
                'final_collection': bool(
                    payment.eh_sepa_dd_final_collection
                ),
                'local_instrument': (
                    mandate.local_instrument
                    or creditor.default_local_instrument
                    or 'CORE'
                ),
                'amendment_record_id': amendment.id or 0,
                'amendment': (
                    mandate._eh_latest_amendment_for_rendering(
                        amendment=amendment,
                    ) or {}
                ),
            })
        canonical = json.dumps(
            source, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        ).encode('utf-8')
        return hashlib.sha256(canonical).hexdigest(), mandates_by_payment

    def _eh_assert_dd_sources_unchanged(self, creditor):
        self.ensure_one()
        approved = self.sepa_dd_source_fingerprint
        if not approved:
            raise UserError(_(
                "This posted batch has no SEPA Direct Debit source "
                "fingerprint captured at confirmation. Legacy/unapproved "
                "creditor and mandate inputs cannot be exported."
            ))
        # Durable dormancy expiry uses an independent cursor. Run it before
        # this request acquires mandate locks; doing it afterwards waits on our
        # own row lock and deadlocks the export.
        preflight = self._eh_dd_mandates_by_payment(creditor)
        for mandate in self.env['eh.sepa.mandate'].browse(sorted({
            mandate.id for mandate in preflight.values()
        })):
            if (
                mandate.state == 'active'
                and mandate._eh_is_expired_at(self.payment_date)
                and mandate._eh_expire_dormant_durably(self.payment_date)
            ):
                raise UserError(_(
                    "Mandate %s has expired after 36 months of dormancy. "
                    "Issue a new mandate before exporting.",
                    mandate.mandate_id,
                ))
        current, mandates_by_payment = self._eh_dd_source_fingerprint(
            creditor, lock=True,
        )
        if not hmac.compare_digest(approved, current):
            raise UserError(_(
                "SEPA Direct Debit source data changed after batch "
                "confirmation (creditor identity/account/policy or mandate "
                "details). Export was blocked because the changed collection "
                "was not approved."
            ))
        return mandates_by_payment

    def _eh_record_dd_source_approval(
        self, previous_fingerprint, fingerprint, reason,
    ):
        self.ensure_one()
        return self.env['eh.sepa.dd.source.approval'].sudo().with_context(**{
            _DD_SOURCE_APPROVAL_CONTEXT: _DD_SOURCE_APPROVAL_CAPABILITY,
        }).create({
            'batch_id': self.id,
            'previous_fingerprint': previous_fingerprint or False,
            'source_fingerprint': fingerprint,
            'reason': reason,
            'approved_by_id': self.env.user.id,
        })

    def action_reapprove_sepa_dd_sources(self):
        """Approve current mandate/sequence inputs on already-booked cash."""
        self.ensure_one()
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can re-approve SEPA Direct "
                "Debit sources."
            ))
        self._eh_lock_for_dd_export()
        if (
            self.state != 'posted'
            or self.batch_type != 'inbound'
            or not self.sepa_dd_collection
        ):
            raise UserError(_(
                "Only an explicitly designated, posted inbound SEPA Direct "
                "Debit batch can re-approve collection sources."
            ))
        if self.confirmed_by_id == self.env.user or self.posted_by_id == self.env.user:
            raise UserError(_(
                "Segregation of duties: the confirmer or poster cannot also "
                "approve changed collection sources."
            ))
        if self.sepa_dd_export_ids.filtered(
                lambda export: export.state in ('generated', 'downloaded')):
            raise UserError(_(
                "Void every live PAIN.008 file before re-approving its "
                "collection sources."
            ))
        reason = ' '.join((self.sepa_dd_reapproval_reason or '').split())
        if len(reason) < 20:
            raise UserError(_(
                "Record at least 20 characters explaining the changed "
                "mandate/sequence data and evidence reviewed."
            ))
        creditor = self._eh_dd_creditor()
        if not creditor:
            raise UserError(_(
                "The batch journal has no SEPA creditor to approve."
            ))
        previous = self.sepa_dd_source_fingerprint
        current, _mandates = self._eh_dd_source_fingerprint(
            creditor, lock=True)
        self.sudo().with_context(**{
            _DD_SOURCE_FINGERPRINT_CONTEXT:
                _DD_SOURCE_FINGERPRINT_CAPABILITY,
        }).write({
            _DD_SOURCE_FINGERPRINT_FIELD: current,
            'sepa_dd_source_approved_at': fields.Datetime.now(),
            'sepa_dd_source_approved_by_id': self.env.user.id,
        })
        self._eh_record_dd_source_approval(previous, current, reason)
        self.write({'sepa_dd_reapproval_reason': False})
        self.message_post(body=_(
            "SEPA Direct Debit source revision approved by %(user)s. "
            "Reason: %(reason)s",
            user=self.env.user.display_name, reason=reason,
        ))
        return True

    def action_export_sepa_dd(self):
        """Run the money-moving export atomically, even for callers which
        catch a raised validation error and continue their transaction."""
        self.ensure_one()
        with self.env.cr.savepoint():
            return self._eh_action_export_sepa_dd()

    def _eh_action_export_sepa_dd(self):
        """Generate one PAIN.008 file per sequence type in this batch.

        Strict prerequisites:
        * Inbound batch (DD collects from debtors).
        * Posted (we do not export pre-authorisation).
        * Creditor configured for the journal.
        * Every payment has a partner with a non-revoked mandate on
          file for this creditor.

        The action consumes each mandate via consume_for_collection,
        which advances the FRST -> RCUR counter atomically.
        """
        self.ensure_one()
        # A guessed cross-company id must fail record rules before any raw
        # row lock or sudo-backed generator work.  The private helper repeats
        # the complete authorization boundary so internal callers cannot
        # accidentally bypass the public wrapper.
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can generate the SEPA "
                "Direct Debit bank file.",
            ))
        # Idempotency guard. Generating the PAIN.008 consumes every
        # mandate in the batch (each consume_for_collection advances the
        # mandate's FRST -> RCUR sequence counter and its last collection
        # date) and cuts the collection file the bank debits against
        # debtor accounts. Re-running the export on a batch that was
        # already exported would consume every mandate a SECOND time --
        # double-advancing the sequence counter (a real first collection
        # would be re-rendered as RCUR) and producing a duplicate file
        # that, if submitted, debits each debtor twice. Take a row lock on
        # the batch so a concurrent second Export click serialises and
        # re-reads the committed export rows, then refuse when a live
        # (generated/downloaded) export already exists. A legitimate
        # re-cut goes through an explicit void of the prior export (which
        # flips it to superseded), never a silent re-run.
        self._eh_lock_for_dd_export()
        prior_exports = self.sepa_dd_export_ids.filtered(
            lambda e: e.state in ('generated', 'downloaded'),
        )
        if prior_exports:
            raise UserError(_(
                "This batch already has a live SEPA Direct Debit export "
                "(%(refs)s). Re-exporting would consume every mandate "
                "again and cut a duplicate collection file, so the bank "
                "could debit each debtor twice. Void the existing export "
                "first if you genuinely need to regenerate.",
                refs=', '.join(prior_exports.mapped('message_id')),
            ))
        # Bank-file segregation of duties (maker/checker). Generating the
        # PAIN.008 is a money-moving act: it is the collection instruction
        # the bank executes against debtor accounts. Require the manager
        # group, and require that the exporter is a different user from the
        # people who assembled the batch (confirmer / poster). Without this
        # gate the same user who builds the batch can also cut the bank
        # file, defeating the four-eyes control.
        if self.confirmed_by_id and self.confirmed_by_id == self.env.user:
            raise UserError(_(
                "Segregation of duties: the manager who confirmed this "
                "batch cannot also generate the SEPA bank file. A "
                "different EH Accounting Manager must export it.",
            ))
        if self.posted_by_id and self.posted_by_id == self.env.user:
            raise UserError(_(
                "Segregation of duties: the manager who posted this "
                "batch cannot also generate the SEPA bank file. A "
                "different EH Accounting Manager must export it.",
            ))
        if (
            self.sepa_dd_source_approved_by_id
            and self.sepa_dd_source_approved_by_id == self.env.user
        ):
            raise UserError(_(
                "Segregation of duties: the manager who approved the current "
                "SEPA Direct Debit source revision cannot also generate the "
                "bank file."
            ))
        if self.batch_type != 'inbound':
            raise UserError(_(
                "SEPA Direct Debit is for inbound batches only.",
            ))
        if not self.sepa_dd_collection:
            raise UserError(_(
                "This inbound batch is not designated as a SEPA Direct Debit "
                "collection. Set the designation while it is draft and "
                "confirm it again."
            ))
        if self.state != 'posted':
            raise UserError(_(
                "Export the SEPA file only after the batch is posted.",
            ))
        creditor = self._eh_dd_creditor()
        if not creditor:
            raise UserError(_(
                "Journal %s has no SEPA creditor configured. Add one "
                "under Configuration > SEPA Creditors.",
                self.journal_id.display_name,
            ))
        mandates_by_payment = self._eh_assert_dd_sources_unchanged(creditor)

        # Pre-notification window. The SEPA Direct Debit rulebook
        # requires the creditor to notify each debtor ahead of the
        # collection date (14 days for CORE/FRST, often 1-2 days for
        # B2B / RCUR variants depending on the bank). The creditor
        # config carries the policy as `pre_notification_days`. We
        # refuse the export when payment_date is closer than that
        # window so the batch never lands at the bank with the
        # debtor still unwarned.  A context key is not an authorization
        # capability: RPC callers control context values.  Keep the internal
        # force hook for controlled migrations/tests, but honour it only when
        # the caller was already superuser before any generator sudo.
        notice_days = max(int(creditor.pre_notification_days or 0), 0)
        force_now = bool(
            self.env.su
            and self.env.context.get('eh_sepa_dd_force_now')
        )
        if notice_days and not force_now:
            today = fields.Date.context_today(self)
            min_collection_date = today + timedelta(days=notice_days)
            if not self.payment_date or self.payment_date < min_collection_date:
                raise UserError(_(
                    "Collection date %(date)s violates the %(days)s-day "
                    "pre-notification window required by SEPA. Push the "
                    "batch's payment date out to %(min)s or later, or "
                    "shorten pre_notification_days on the creditor "
                    "config if your bank permits.",
                    date=fields.Date.to_string(self.payment_date) if self.payment_date else _("(unset)"),
                    days=notice_days,
                    min=fields.Date.to_string(min_collection_date),
                ))

        # Single-currency enforcement. The PAIN.008 message-level
        # control sum is computed in one currency; mixing currencies
        # within a single file is invalid per ISO 20022 and rejected
        # by every European bank we know of. Reject at submission
        # time so the user gets a clean message rather than a bank
        # rejection later.
        currencies = self.payment_ids.filtered(
            lambda p: p.state in POSTED_STATES,
        ).mapped('currency_id')
        if len(currencies) > 1:
            raise UserError(_(
                "Batch contains payments in %(count)s different "
                "currencies (%(names)s). Split the batch by currency "
                "before exporting; SEPA DD requires one currency "
                "per file.",
                count=len(currencies),
                names=', '.join(currencies.mapped('name')),
            ))
        # Euro enforcement. SEPA Direct Debit is a euro-denominated
        # scheme and the generated pain.008 hard-codes EUR at the amount
        # level. Refuse a non-euro batch here rather than emit a file
        # that mislabels the collected amounts as EUR.
        if currencies and currencies.name != 'EUR':
            raise UserError(_(
                "SEPA Direct Debit files are euro-denominated, but this "
                "batch is in %(name)s. Collect in EUR or use a non-SEPA "
                "collection method.",
                name=currencies.name,
            ))

        # Group payments by the sequence type their mandate will yield.  The
        # first successful cut persists that consumption decision on each
        # payment.  An explicit re-cut reuses it instead of consuming the
        # mandate again (FRST must stay FRST, and OOFF must remain re-cuttable
        # after the first cut completed its mandate).
        groups = defaultdict(list)
        seen_mandates = {}
        has_export_history = bool(self.sepa_dd_export_ids)
        for payment in self.payment_ids.filtered(
            lambda p: p.state in POSTED_STATES,
        ):
            snapshot = (
                payment.eh_sepa_dd_mandate_id,
                payment.eh_sepa_dd_sequence_type,
                payment.eh_sepa_dd_local_instrument,
            )
            if any(snapshot) and not all(snapshot):
                raise UserError(_(
                    "Payment %(payment)s has incomplete SEPA consumption "
                    "evidence. Re-cut was blocked; an administrator must "
                    "repair the audit snapshot.",
                    payment=payment.display_name,
                ))
            if all(snapshot):
                mandate, seq_type, local_instrument = snapshot
                if (
                    mandate.creditor_id != creditor
                    or mandate.partner_id != payment.partner_id
                ):
                    raise UserError(_(
                        "Payment %(payment)s consumption evidence no longer "
                        "matches its creditor and debtor. Re-cut was blocked.",
                        payment=payment.display_name,
                    ))
            else:
                if has_export_history:
                    # Pre-hardening exports have no per-payment consumption
                    # proof.  Guessing would silently advance FRST/OOFF audit
                    # state, so legacy re-cuts fail closed for manual review.
                    raise UserError(_(
                        "This batch has historic SEPA Direct Debit exports "
                        "but payment %(payment)s has no consumption snapshot. "
                        "Automatic re-cut is unsafe because it could advance "
                        "the mandate twice.",
                        payment=payment.display_name,
                    ))
                mandate = mandates_by_payment[payment.id]
                local_instrument = (
                    mandate.local_instrument
                    or creditor.default_local_instrument
                    or 'CORE'
                )
                mandate._eh_assert_scheme_eligibility()
                seq_type = mandate.sudo().with_context(**{
                    _SEPA_DD_CONSUME_CTX: True,
                    _SEPA_DD_EXPIRY_PREFLIGHT_CTX: True,
                }).consume_for_collection(
                    self.payment_date,
                    mark_final=bool(payment.eh_sepa_dd_final_collection),
                )
                payment.sudo().with_context(
                    **{_DD_CONSUMPTION_CONTEXT: True}
                ).write({
                    'eh_sepa_dd_mandate_id': mandate.id,
                    'eh_sepa_dd_sequence_type': seq_type,
                    'eh_sepa_dd_local_instrument': local_instrument,
                })

            amendment = self._eh_dd_amendment_for_payment(payment, mandate)
            if amendment:
                if not payment.eh_sepa_dd_amendment_id:
                    payment.sudo().with_context(
                        **{_DD_CONSUMPTION_CONTEXT: True}
                    ).write({'eh_sepa_dd_amendment_id': amendment.id})
                    payment.invalidate_recordset(['eh_sepa_dd_amendment_id'])
                amendment._eh_mark_reported_by_payment(payment)

            if mandate.id in seen_mandates:
                raise UserError(_(
                    "Mandate %(mandate)s is used by more than one payment in "
                    "this batch (%(first)s and %(second)s). Split or combine "
                    "the collection so one mandate is consumed once per file.",
                    mandate=mandate.mandate_id,
                    first=seen_mandates[mandate.id].display_name,
                    second=payment.display_name,
                ))
            seen_mandates[mandate.id] = payment
            groups[(seq_type, local_instrument)].append((payment, mandate))

        if not groups:
            raise UserError(_(
                "Batch contains no posted payments to collect.",
            ))

        # Only generator-owned (sudo-created) rows are accepted by the export
        # model. sudo keeps the real exporter as env.user for the audit stamp.
        exports = self.env['eh.sepa.dd.export'].sudo()
        for (seq_type, local_instrument), items in groups.items():
            payload = self._eh_build_pain_008_payload(
                creditor=creditor, seq_type=seq_type, items=items,
                local_instrument=local_instrument,
            )
            xml_bytes = pain_008.render(payload)
            attachment = self._eh_persist_dd_attachment(
                payload, xml_bytes, seq_type, local_instrument,
            )
            export = exports.create({
                'batch_id': self.id,
                'message_id': payload['message_id'],
                'transaction_count': sum(
                    len(p['transactions']) for p in payload['payments']
                ),
                'control_sum': float(sum(
                    Decimal(str(tx['amount']))
                    for p in payload['payments']
                    for tx in p['transactions']
                )),
                'file_hash': exports.compute_hash(
                    xml_bytes,
                ),
                'sequence_type': seq_type,
                'local_instrument': local_instrument,
                'attachment_id': attachment.id,
            })
            exports |= export
            self.message_post(body=_(
                "SEPA DD %(seq)s/%(instrument)s file generated. Message ID: "
                "%(id)s. "
                "Transactions: %(count)d. Control sum: %(sum).2f.",
                seq=seq_type,
                instrument=local_instrument,
                id=export.message_id,
                count=export.transaction_count,
                sum=export.control_sum,
            ))

        # If only one file, return its download URL directly. Multi-file
        # exports navigate to the export list so the user downloads each
        # in turn.
        if len(exports) == 1:
            return exports.action_download()
        return {
            'type': 'ir.actions.act_window',
            'name': _("SEPA DD exports for %s") % self.name,
            'res_model': 'eh.sepa.dd.export',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('batch_id', '=', self.id)],
        }

    def _eh_lock_for_dd_export(self):
        """Row-lock this batch so a concurrent second Export click
        serialises and re-reads the committed export rows rather than a
        stale pre-export snapshot.

        Closes the double-consume/double-file race in which two
        transactions both read zero prior exports, both consume every
        mandate (advancing each sequence counter twice) and both cut a
        collection file. Mirrors eh_account_fx_revaluation's
        _eh_lock_for_post.
        """
        self.ensure_one()
        self.flush_recordset()
        # Export rows use the same company -> batch -> export ordering for
        # download/void transitions.  Taking the company lock first here
        # prevents a generation/void deadlock and makes the batch the shared
        # serialization point for every live-file state change.
        self.env.cr.execute(
            "SELECT id FROM res_company WHERE id = %s FOR UPDATE",
            (self.company_id.id,),
        )
        self.env.cr.execute(
            "SELECT id FROM eh_batch_payment WHERE id = %s FOR UPDATE",
            (self.id,),
        )
        self.invalidate_recordset()

    def _eh_resolve_mandate(self, payment, creditor):
        """Pick the mandate that authorises this payment.

        Rules, in order:
        * Active mandates for this creditor and partner only.
        * A selected partner bank must match exactly one mandate IBAN.
        * Without a selected bank, exactly one active mandate must exist.
        """
        partner = payment.partner_id
        Mandate = self.env['eh.sepa.mandate']
        candidates = Mandate.search(
            [
                ('creditor_id', '=', creditor.id),
                ('partner_id', '=', partner.id),
                ('state', '=', 'active'),
            ],
            order='signature_date desc, id desc',
        )
        if not candidates:
            raise UserError(_(
                "Payment %(name)s: partner %(partner)s has no active "
                "SEPA mandate for creditor %(creditor)s. Issue a "
                "mandate before collecting.",
                name=payment.display_name,
                partner=partner.display_name,
                creditor=creditor.name,
            ))
        if payment.eh_sepa_dd_debtor_bank_id:
            iban = payment.eh_sepa_dd_debtor_bank_id.acc_number
            matches = self.env['eh.sepa.mandate']
            for mandate in candidates:
                try:
                    if validate_iban(mandate.debtor_iban) == validate_iban(iban):
                        matches |= mandate
                except IbanValidationError:
                    continue
            if len(matches) == 1:
                return matches
            raise UserError(_(
                "Payment %(payment)s bank account does not match exactly one "
                "active mandate. Select the mandated IBAN or resolve duplicate "
                "mandates before export.",
                payment=payment.display_name,
            ))
        if len(candidates) != 1:
            raise UserError(_(
                "Payment %(payment)s has %(count)d active mandates and no "
                "debtor bank selected. Select the mandated IBAN before export.",
                payment=payment.display_name,
                count=len(candidates),
            ))
        return candidates

    def _eh_build_pain_008_payload(self, creditor, seq_type, items,
                                   local_instrument=None):
        """Compose the PAIN.008 payload dict for one (sequence type,
        local instrument) group. Every transaction in `items` shares both
        the sequence type and the scheme variant, so the resulting PmtInf
        is homogeneous as the SEPA rulebook requires."""
        self.ensure_one()
        try:
            cdtr_iban = validate_iban(creditor.iban)
        except IbanValidationError as exc:
            raise UserError(_(
                "Creditor IBAN failed validation: %s",
            ) % str(exc))
        cdtr_bic = None
        if creditor.bic:
            try:
                cdtr_bic = validate_bic(creditor.bic)
            except BicValidationError as exc:
                raise UserError(_(
                    "Creditor BIC failed validation: %s",
                ) % str(exc))

        transactions = []
        for payment, mandate in items:
            try:
                dbtr_iban = validate_iban(mandate.debtor_iban)
            except IbanValidationError as exc:
                raise UserError(_(
                    "Mandate %(mid)s: debtor IBAN invalid (%(err)s).",
                    mid=mandate.mandate_id, err=str(exc),
                ))
            dbtr_bic = None
            if mandate.debtor_bic:
                try:
                    dbtr_bic = validate_bic(mandate.debtor_bic)
                except BicValidationError as exc:
                    raise UserError(_(
                        "Mandate %(mid)s: debtor BIC invalid (%(err)s).",
                        mid=mandate.mandate_id, err=str(exc),
                    ))
            mandate_block = {
                'id': mandate.mandate_id,
                'signature_date': mandate.signature_date,
            }
            # AmdmntInf: when the mandate carries an amendment trail, the
            # next collection must flag the change so the bank can
            # reconcile the amended mandate to its predecessor.
            amendment_record = payment.eh_sepa_dd_amendment_id
            amendment = mandate._eh_latest_amendment_for_rendering(
                amendment=amendment_record,
            ) if amendment_record else None
            if amendment:
                mandate_block['amendment'] = amendment
            transactions.append({
                'end_to_end_id': self._eh_clip(
                    payment.name or "PMT%05d" % payment.id, 35,
                ),
                'amount': Decimal(str(payment.amount)),
                'mandate': mandate_block,
                'debtor': {
                    'name': self._eh_required_sepa_text(
                        mandate.debtor_name
                        or payment.partner_id.display_name,
                        _("Debtor name"), 70,
                    ),
                    'iban': dbtr_iban,
                    'bic': dbtr_bic,
                },
                'remittance_info': self._eh_clip(
                    payment.memo or '', 140,
                ) or None,
            })

        return {
            'message_id': self._eh_sepa_dd_msg_id(
                seq_type, local_instrument),
            'creation_datetime': fields.Datetime.now(),
            'initiating_party': {
                'name': self._eh_required_sepa_text(
                    creditor.creditor_name, _("Creditor name"), 70,
                ),
                'identifier': normalize_creditor_identifier(
                    creditor.creditor_identifier,
                ),
            },
            'payments': [
                {
                    'payment_info_id': self._eh_clip(
                        "%s-%s-%s" % (
                            self.name.replace('/', '-'), seq_type,
                            local_instrument or creditor.default_local_instrument,
                        ),
                        35,
                    ),
                    'requested_collection_date': self.payment_date,
                    'sequence_type': seq_type,
                    'local_instrument': (
                        local_instrument or creditor.default_local_instrument
                    ),
                    'batch_booking': bool(creditor.batch_booking),
                    'creditor': {
                        'name': self._eh_required_sepa_text(
                            creditor.creditor_name, _("Creditor name"), 70,
                        ),
                        'iban': cdtr_iban,
                        'bic': cdtr_bic,
                        'identifier': normalize_creditor_identifier(
                            creditor.creditor_identifier,
                        ),
                    },
                    'transactions': transactions,
                },
            ],
        }

    @staticmethod
    def _eh_clip(value, max_length):
        if not value:
            return value
        return sanitize_sepa_text(value)[:max_length]

    @staticmethod
    def _eh_required_sepa_text(value, label, max_length):
        rendered = (sanitize_sepa_text(value or '') or '').strip()
        if not rendered:
            raise UserError(_(
                "%s must contain at least one character supported by SEPA "
                "bank-file character rules.",
                label,
            ))
        return rendered[:max_length]

    def _eh_sepa_dd_msg_id(self, seq_type, local_instrument=None):
        """Globally unique SEPA MsgId (max 35 chars) for one sequence-type
        file: a short sanitised batch name, the sequence type, a UTC-clock
        timestamp, and a uuid fragment so two exports of the same sequence
        type in the same second never collide.
        """
        self.ensure_one()
        name_part = sanitize_sepa_text(
            (self.name or 'EH').replace('/', '-'),
        )[:6]
        ts = fields.Datetime.now().strftime('%Y%m%d%H%M%S')
        instrument = sanitize_sepa_text(local_instrument or 'DD')[:4]
        return (
            "%s-%s-%s-%s-%s" % (
                name_part, seq_type, instrument, ts, uuid.uuid4().hex[:6],
            )
        )[:35]

    def _eh_persist_dd_attachment(
        self, payload, xml_bytes, seq_type, local_instrument,
    ):
        self.ensure_one()
        filename = "%s_%s_%s_%s.xml" % (
            self.name.replace('/', '_'),
            seq_type,
            local_instrument,
            fields.Datetime.now().strftime('%Y%m%d%H%M%S'),
        )
        return self.env['ir.attachment'].sudo().create({
            'name': filename,
            'type': 'binary',
            'datas': base64.b64encode(xml_bytes),
            'mimetype': 'application/xml',
            'res_model': self._name,
            'res_id': self.id,
            'company_id': self.company_id.id,
        })


class EhSepaDdSourceApproval(models.Model):
    _name = 'eh.sepa.dd.source.approval'
    _description = "SEPA Direct Debit source approval"
    _order = 'approved_at desc, id desc'

    batch_id = fields.Many2one(
        'eh.batch.payment', required=True, ondelete='restrict', index=True)
    company_id = fields.Many2one(
        related='batch_id.company_id', store=True, readonly=True, index=True)
    previous_fingerprint = fields.Char(readonly=True)
    source_fingerprint = fields.Char(required=True, readonly=True, index=True)
    reason = fields.Text(required=True, readonly=True)
    approved_at = fields.Datetime(
        required=True, readonly=True, default=fields.Datetime.now)
    approved_by_id = fields.Many2one(
        'res.users', required=True, readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get(
                _DD_SOURCE_APPROVAL_CONTEXT) is not _DD_SOURCE_APPROVAL_CAPABILITY:
            raise AccessError(_(
                "SEPA Direct Debit source approval evidence can only be "
                "created by the controlled approval workflow."
            ))
        return super().create(vals_list)

    def write(self, vals):
        raise AccessError(_(
            "SEPA Direct Debit source approval evidence is append-only."
        ))

    def unlink(self):
        raise AccessError(_(
            "SEPA Direct Debit source approval evidence cannot be deleted."
        ))
