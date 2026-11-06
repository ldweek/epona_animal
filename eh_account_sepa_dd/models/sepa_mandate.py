# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.sepa.mandate: a debtor's authorisation to collect via SEPA DD.

Mandate lifecycle per the SEPA Direct Debit Scheme Rulebook:

    draft -> active -> completed
                    \\-> revoked
                    \\-> expired (36 months without a collection)

Sequence type lifecycle within an active mandate:

    FRST (first collection)
      -> RCUR (subsequent recurring collections)
        -> RCUR
          -> ...
          -> FNAL (last collection; mandate flips to completed)

OOFF (one-off) is a separate mandate flavour: a single collection
authorised by the debtor, marked completed immediately on use.

The model exposes consume_for_collection(amount) which advances the
sequence counter atomically via SQL UPDATE so two concurrent collection
attempts cannot race the FRST -> RCUR transition. The 36-month expiry
rule is enforced at consume time: a mandate that has not been used for
36 calendar months is flipped to expired before consume can succeed.
"""

from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import SQL

from odoo.addons.eh_account_sepa_ct.tools.iban_validator import (
    validate_iban, IbanValidationError,
)
from odoo.addons.eh_account_sepa_ct.tools.bic_validator import (
    validate_bic, BicValidationError,
)


_SEPA_MANDATE_EXPIRY_MONTHS = 36
_SEPA_DD_CONSUME_CTX = 'eh_sepa_dd_collection_engine'
_SEPA_DD_EXPIRY_PREFLIGHT_CTX = 'eh_sepa_dd_expiry_preflight_done'
_SEPA_DD_AMENDMENT_REPORT_CONTEXT = 'eh_sepa_dd_amendment_report_capability'
_SEPA_DD_AMENDMENT_REPORT_CAPABILITY = object()

_MANDATE_SIGNED_IDENTITY_FIELDS = frozenset({
    'mandate_id',
    'creditor_id',
    'partner_id',
    'signature_date',
    'is_one_off',
    'local_instrument',
    'b2b_bank_confirmed',
})

_MANDATE_AMENDABLE_FIELDS = frozenset({
    'debtor_name',
    'debtor_iban',
    'debtor_bic',
})


class EhSepaMandate(models.Model):
    _name = 'eh.sepa.mandate'
    _description = "SEPA Direct Debit mandate"
    _inherit = [
        'mail.thread', 'mail.activity.mixin', 'eh.cron.batch.mixin',
        'eh.workflow.guard',
    ]
    _order = 'state, signature_date desc, id desc'
    _rec_name = 'mandate_id'
    _check_company_auto = True

    # Lifecycle state and sequence facts are server-owned. They move only
    # through sanctioned manager actions / the collection engine, never a
    # direct RPC/ORM write.
    _eh_guarded_fields = (
        'state',
        'collection_count',
        'last_collection_date',
    )

    # ---- identity ----
    mandate_id = fields.Char(
        required=True, copy=False, index=True, tracking=True,
        default=lambda self: self.env['ir.sequence'].next_by_code(
            'eh.sepa.mandate',
        ),
        help=(
            "Unique mandate reference issued to the debtor. Capped at "
            "35 characters per the scheme. Once the mandate is active "
            "this id is immutable; changing it would invalidate every "
            "future collection."
        ),
    )
    creditor_id = fields.Many2one(
        'eh.sepa.creditor', required=True,
        ondelete='restrict', tracking=True, check_company=True,
    )
    company_id = fields.Many2one(
        related='creditor_id.company_id', store=True, readonly=True,
        index=True,
    )
    partner_id = fields.Many2one(
        'res.partner', required=True,
        ondelete='restrict', tracking=True, check_company=True,
        help="The debtor: the party authorising the collection.",
    )
    debtor_name = fields.Char(
        tracking=True,
        help=(
            "Debtor legal name captured on the signed mandate. It is stored "
            "rather than rendered from the mutable partner name; a later "
            "change is a controlled mandate amendment."
        ),
    )

    # ---- bank ----
    debtor_iban = fields.Char(
        string="Debtor IBAN", required=True, tracking=True,
        help=(
            "Account the collections debit. Validated locally on save. "
            "Changing this on an active mandate counts as a mandate "
            "amendment per the scheme; the engine tracks amendment "
            "history but does not block the change."
        ),
    )
    debtor_bic = fields.Char(string="Debtor BIC / SWIFT")

    # ---- scheme variant ----
    local_instrument = fields.Selection(
        [
            ('CORE', "CORE (consumer)"),
            ('B2B', "B2B (business-to-business)"),
        ],
        required=True, tracking=True,
        default=lambda self: (
            self.env.company.eh_sepa_dd_default_instrument
            if self.env.company.eh_sepa_dd_default_instrument in ('CORE', 'B2B')
            else 'CORE'
        ),
    )
    b2b_bank_confirmed = fields.Boolean(
        string="Debtor bank confirmed B2B mandate",
        tracking=True,
        help=(
            "Required before activating a B2B mandate. Confirms debtor bank "
            "accepted mandate details; B2B collections have no refund right."
        ),
    )

    # ---- lifecycle ----
    signature_date = fields.Date(
        required=True, tracking=True,
        help=(
            "Date the debtor signed the mandate. Goes into "
            "MndtRltdInf/DtOfSgntr on every collection that uses this "
            "mandate."
        ),
    )
    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('active', "Active"),
            ('completed', "Completed"),
            ('revoked', "Revoked"),
            ('expired', "Expired"),
        ],
        default='draft', required=True, tracking=True, index=True,
    )
    is_one_off = fields.Boolean(
        default=False, tracking=True,
        help=(
            "When True the mandate authorises a single collection only. "
            "First (and last) use renders as OOFF and the mandate "
            "flips to completed immediately."
        ),
    )

    # ---- collection counter ----
    next_sequence_type = fields.Selection(
        [
            ('FRST', "First (FRST)"),
            ('RCUR', "Recurring (RCUR)"),
            ('FNAL', "Final (FNAL)"),
            ('OOFF', "One-off (OOFF)"),
        ],
        compute='_compute_next_sequence_type',
        help=(
            "Sequence type the next collection will use given the "
            "mandate's state, is_one_off flag, and collection_count."
        ),
    )
    collection_count = fields.Integer(
        readonly=True, copy=False, default=0,
        help="How many collections have been rendered against this mandate.",
    )
    last_collection_date = fields.Date(readonly=True, copy=False)
    expires_on = fields.Date(
        compute='_compute_expires_on',
        help=(
            "Date this mandate auto-expires given the SEPA scheme's "
            "36-month dormancy rule. Recomputed from "
            "last_collection_date + 36 months. When state moves out "
            "of active for any other reason, this date stops mattering."
        ),
    )

    # ---- amendments ----
    amendment_ids = fields.One2many(
        'eh.sepa.mandate.amendment', 'mandate_id', readonly=True,
    )

    notes = fields.Text()

    _unique_mandate_id = models.Constraint(
        'unique(mandate_id, company_id)',
        'Mandate id must be unique per company.',
    )

    # ---- onchange (live form feedback) ----

    @api.onchange('partner_id')
    def _onchange_partner_id_iban(self):
        """Pre-fill the debtor IBAN/BIC from the partner's primary
        bank account when a partner is picked.

        Without this, an operator creating a mandate has to look up the
        IBAN separately and paste it. With the pre-fill, picking the
        partner is enough; they only edit if the partner has multiple
        banks and the wrong one was first.
        """
        for rec in self:
            if not rec.partner_id:
                continue
            bank = rec.partner_id.bank_ids[:1]
            if bank:
                if not rec.debtor_iban:
                    rec.debtor_iban = bank.acc_number
                if not rec.debtor_bic and bank.bank_bic:
                    rec.debtor_bic = bank.bank_bic

    @api.onchange('creditor_id')
    def _onchange_creditor_id_local_instrument(self):
        """Adopt the creditor's default scheme variant when picked.

        A creditor configured for B2B should not have CORE mandates
        accidentally created against it. The default copy at create
        time only fires for the FIRST creditor; this onchange covers
        the case where the user changes creditor on an in-progress
        mandate form.
        """
        for rec in self:
            if not rec.creditor_id:
                continue
            if rec.creditor_id.default_local_instrument:
                rec.local_instrument = rec.creditor_id.default_local_instrument

    @api.constrains('mandate_id')
    def _check_mandate_id_length(self):
        for rec in self:
            if rec.mandate_id and len(rec.mandate_id) > 35:
                raise ValidationError(_(
                    "Mandate id is capped at 35 characters per the "
                    "SEPA scheme; got %d.",
                ) % len(rec.mandate_id))

    @api.constrains('debtor_iban')
    def _check_iban(self):
        for rec in self:
            try:
                validate_iban(rec.debtor_iban)
            except IbanValidationError as exc:
                raise ValidationError(_(
                    "Debtor IBAN failed validation: %s",
                ) % str(exc))

    @api.constrains('debtor_bic')
    def _check_bic(self):
        for rec in self:
            if not rec.debtor_bic:
                continue
            try:
                validate_bic(rec.debtor_bic)
            except BicValidationError as exc:
                raise ValidationError(_(
                    "Debtor BIC failed validation: %s",
                ) % str(exc))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('debtor_name') and vals.get('partner_id'):
                vals['debtor_name'] = self.env['res.partner'].browse(
                    vals['partner_id'],
                ).display_name
            if vals.get('debtor_iban'):
                try:
                    vals['debtor_iban'] = validate_iban(vals['debtor_iban'])
                except IbanValidationError as exc:
                    raise ValidationError(_(
                        "Debtor IBAN failed validation: %s",
                    ) % str(exc))
            if vals.get('debtor_bic'):
                try:
                    vals['debtor_bic'] = validate_bic(vals['debtor_bic'])
                except BicValidationError as exc:
                    raise ValidationError(_(
                        "Debtor BIC failed validation: %s",
                    ) % str(exc))
        return super().create(vals_list)

    def write(self, vals):
        if 'local_instrument' in vals:
            scheme_changes = self.filtered(
                lambda mandate: mandate.state != 'draft'
                and vals['local_instrument'] != mandate.local_instrument
            )
            if scheme_changes:
                raise UserError(_(
                    "SEPA scheme is part of signed mandate identity. Issue a "
                    "new mandate instead of changing scheme on %s.",
                    ', '.join(scheme_changes.mapped('mandate_id')),
                ))
        protected = (
            _MANDATE_SIGNED_IDENTITY_FIELDS | _MANDATE_AMENDABLE_FIELDS
        ).intersection(vals)
        if not self.env.su and protected:
            self._eh_check_access('write')
            self._eh_lock_for_update()
            signed_changes = _MANDATE_SIGNED_IDENTITY_FIELDS.intersection(vals)
            signed = self.filtered(lambda mandate: mandate.state != 'draft')
            if signed_changes and signed:
                raise AccessError(_(
                    "Signed mandate identity is immutable. Create a new "
                    "mandate instead of changing %(fields)s on %(mandates)s.",
                    fields=', '.join(sorted(signed_changes)),
                    mandates=', '.join(signed.mapped('display_name')),
                ))
            amendments = _MANDATE_AMENDABLE_FIELDS.intersection(vals)
            active = self.filtered(lambda mandate: mandate.state == 'active')
            if amendments and active and not self.env.user.has_group(
                    'eh_account_base.group_eh_manager'):
                raise AccessError(_(
                    "Only an EH Accounting Manager can amend an active SEPA "
                    "mandate's debtor or scheme details.",
                ))
        if vals.get('debtor_iban'):
            try:
                vals['debtor_iban'] = validate_iban(vals['debtor_iban'])
            except IbanValidationError as exc:
                raise ValidationError(_(
                    "Debtor IBAN failed validation: %s",
                ) % str(exc))
        if vals.get('debtor_bic'):
            try:
                vals['debtor_bic'] = validate_bic(vals['debtor_bic'])
            except BicValidationError as exc:
                raise ValidationError(_(
                    "Debtor BIC failed validation: %s",
                ) % str(exc))
        # Capture pre-write values on active mandates so an amendment
        # trail exists for any IBAN, BIC, or debtor-name change. The
        # scheme requires the next collection after such a change to
        # carry AmdmntInf; without a recorded amendment that history
        # could never be reconstructed.
        amend_specs = []
        for mandate in self:
            if mandate.state != 'active':
                continue
            amend_specs.extend(mandate._eh_prepare_amendments(vals))
        res = super().write(vals)
        if amend_specs:
            # Amendment evidence is engine-owned and append-only. sudo is the
            # unforgeable provenance proof; it preserves the real editor as
            # env.user for amended_by_id.
            self.env['eh.sepa.mandate.amendment'].sudo().create(amend_specs)
        return res

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
            self._eh_lock_for_update()
            retained = self.filtered(
                lambda mandate: mandate.state != 'draft'
                or bool(mandate.collection_count)
                or bool(mandate.amendment_ids),
            )
            if retained:
                raise AccessError(_(
                    "Signed or used SEPA mandates are audit evidence and "
                    "cannot be deleted. Revoke them instead.",
                ))
        return super().unlink()

    def _eh_prepare_amendments(self, vals):
        """Return amendment create-dicts for the changes in `vals`.

        Called before super().write() so old_value reflects the value
        on record prior to the change. Only IBAN, BIC, and debtor-name
        changes count as scheme amendments; a no-op change is skipped.
        """
        self.ensure_one()
        specs = []
        if 'debtor_iban' in vals and vals['debtor_iban'] != self.debtor_iban:
            specs.append({
                'mandate_id': self.id,
                'amendment_type': 'iban',
                'old_value': self.debtor_iban,
                'new_value': vals['debtor_iban'],
            })
        if 'debtor_bic' in vals and vals['debtor_bic'] != self.debtor_bic:
            specs.append({
                'mandate_id': self.id,
                'amendment_type': 'agent',
                'old_value': self.debtor_bic,
                'new_value': vals['debtor_bic'],
            })
        if 'debtor_name' in vals and vals['debtor_name'] != self.debtor_name:
            specs.append({
                'mandate_id': self.id,
                'amendment_type': 'name',
                'old_value': self.debtor_name,
                'new_value': vals['debtor_name'],
            })
        return specs

    def _eh_pending_amendment_for_rendering(self):
        """Return the newest engine amendment not yet used in a collection."""
        self.ensure_one()
        return self.amendment_ids.filtered(
            lambda amendment: amendment.engine_owned
            and not amendment.reported_payment_id
        )[:1]

    def _eh_latest_amendment_for_rendering(self, amendment=None):
        """Return the AmdmntInf dict for the most recent amendment, or
        None if this mandate has never been amended.

        Maps our amendment rows onto the pain_008 amendment keys:
        an IBAN change becomes OrgnlDbtrAcct (original IBAN); a debtor
        bank / agent change (BIC) becomes OrgnlDbtrAcct/Id/Othr/Id with the
        SMNDA marker (same mandate, new debtor agent); a debtor-name change
        carries the exact original debtor name. Scheme changes
        require a new mandate and never enter this amendment path.
        """
        self.ensure_one()
        # Ignore pre-hardening/legacy rows that cannot be proven to have been
        # generated by the mandate engine. A forged latest row must never
        # alter the AmdmntInf block sent to the bank.
        amendment = amendment or self._eh_pending_amendment_for_rendering()
        if not amendment:
            return None
        details = {}
        if amendment.amendment_type == 'agent':
            # SMNDA: the debtor moved bank. Per the scheme the next
            # collection carries OrgnlDbtrAcct/Id/Othr/Id=SMNDA so the new
            # debtor bank knows to honour the existing mandate.
            details['same_mandate_new_debtor_agent'] = True
        elif amendment.amendment_type == 'iban' and amendment.old_value:
            details['original_iban'] = amendment.old_value
        elif amendment.amendment_type == 'name' and amendment.old_value:
            details['original_debtor_name'] = amendment.old_value
        return details

    @api.depends('state', 'is_one_off', 'collection_count')
    def _compute_next_sequence_type(self):
        for mandate in self:
            if mandate.state != 'active':
                mandate.next_sequence_type = False
                continue
            if mandate.is_one_off:
                mandate.next_sequence_type = 'OOFF'
                continue
            mandate.next_sequence_type = (
                'FRST' if mandate.collection_count == 0 else 'RCUR'
            )

    @api.depends('last_collection_date', 'signature_date', 'state')
    def _compute_expires_on(self):
        for mandate in self:
            basis_date = mandate.last_collection_date or mandate.signature_date
            if mandate.state != 'active' or not basis_date:
                mandate.expires_on = False
                continue
            from dateutil.relativedelta import relativedelta
            mandate.expires_on = (
                basis_date
                + relativedelta(months=_SEPA_MANDATE_EXPIRY_MONTHS)
            )

    # ---- transitions ----

    def _eh_lock_for_update(self):
        """Serialize lifecycle/amendment decisions for these mandates."""
        if not self.ids:
            return
        self.flush_recordset([
            'state', 'collection_count', 'last_collection_date',
        ])
        self.env.cr.execute(
            "SELECT id FROM eh_sepa_mandate WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            (tuple(sorted(self.ids)),),
        )
        self.invalidate_recordset()

    def _eh_authorize_mandate_action(self):
        """Prove ACL, company rule, and manager status before any sudo."""
        self._eh_check_access('write')
        if not self.env.su and not self.env.user.has_group(
                'eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can operate SEPA mandates.",
            ))

    def action_activate(self):
        self._eh_authorize_mandate_action()
        self._eh_lock_for_update()
        self = self._eh_workflow_action()
        for mandate in self:
            if mandate.state != 'draft':
                raise UserError(_(
                    "Only draft mandates can be activated.",
                ))
            mandate._eh_assert_scheme_eligibility()
            mandate.state = 'active'

    def _eh_assert_scheme_eligibility(self):
        """Fail closed on B2B mandates lacking business/bank proof."""
        for mandate in self:
            if mandate.local_instrument != 'B2B':
                continue
            if not mandate.partner_id.commercial_partner_id.is_company:
                raise UserError(_(
                    "B2B SEPA Direct Debit requires a business debtor. Use "
                    "CORE for consumers.",
                ))
            if not mandate.b2b_bank_confirmed:
                raise UserError(_(
                    "B2B mandate %s cannot activate or export until debtor "
                    "bank confirmation is recorded.",
                    mandate.mandate_id,
                ))
        return True

    def action_revoke(self):
        self._eh_authorize_mandate_action()
        self._eh_lock_for_update()
        self = self._eh_workflow_action()
        for mandate in self:
            if mandate.state in ('completed', 'revoked'):
                continue
            mandate.state = 'revoked'

    def action_set_to_draft(self):
        self._eh_authorize_mandate_action()
        self._eh_lock_for_update()
        for mandate in self:
            raise UserError(_(
                "Mandate %s is signed evidence and cannot be reissued under "
                "the same reference. Retain it and issue a new mandate.",
                mandate.mandate_id,
            ))

    # ---- consume ----

    @api.private
    def _eh_expire_dormant_durably(self, collection_date):
        """Commit a persisted dormant mandate's expiry independently.

        Raising ``UserError`` rolls the request cursor back. Updating on
        that same cursor (even with raw SQL and a flush) therefore cannot
        make the expiry durable. Use a separate cursor *before* taking the
        request cursor's row lock and commit only the narrowly-scoped,
        compare-and-set expiry transition.

        A mandate created earlier in the current uncommitted transaction is
        intentionally invisible to the independent cursor; the caller keeps
        a same-transaction fallback for that exceptional path.
        """
        self.ensure_one()
        from dateutil.relativedelta import relativedelta
        cutoff = collection_date - relativedelta(
            months=_SEPA_MANDATE_EXPIRY_MONTHS,
        )
        with self.env.registry.cursor() as durable_cr:
            durable_cr.execute(
                "UPDATE eh_sepa_mandate "
                "SET state = 'expired', write_uid = %s, write_date = %s "
                "WHERE id = %s AND state = 'active' "
                "  AND COALESCE(last_collection_date, signature_date) < %s "
                "RETURNING id",
                (
                    self.env.uid, fields.Datetime.now(), self.id, cutoff,
                ),
            )
            updated = bool(durable_cr.fetchone())
            if updated:
                durable_cr.commit()
        if updated:
            self.invalidate_recordset(['state', 'write_uid', 'write_date'])
        return updated

    @api.private
    def consume_for_collection(self, collection_date, mark_final=False):
        """Reserve the next sequence type and advance the counter.

        Returns the sequence type to use (FRST / RCUR / FNAL / OOFF).
        Atomic via SQL UPDATE so two concurrent collection runs cannot
        race the FRST -> RCUR transition.

        :param collection_date: date.date the collection will hit.
        :param mark_final: when True, render this collection as FNAL
            and flip the mandate to completed after.
        """
        self.ensure_one()
        if not (
            self.env.su and self.env.context.get(_SEPA_DD_CONSUME_CTX)
        ):
            raise AccessError(_(
                "Mandate consumption is a private collection-engine "
                "operation and cannot be invoked directly."
            ))
        # Do this before _eh_lock_for_update(): an independent transaction
        # cannot update a row already locked by this request cursor.
        if (
            self.state == 'active'
            and self._eh_is_expired_at(collection_date)
            and not self.env.context.get(_SEPA_DD_EXPIRY_PREFLIGHT_CTX)
            and self._eh_expire_dormant_durably(collection_date)
        ):
            raise UserError(_(
                "Mandate %s has been dormant for more than %d months "
                "and is now expired. Issue a new mandate before "
                "collecting again.",
                self.mandate_id, _SEPA_MANDATE_EXPIRY_MONTHS,
            ))
        self._eh_lock_for_update()
        # Server-initiated state advance (state -> completed on FNAL/OOFF).
        # Run under sudo so the guard passes; the real env.user is preserved
        # for the message_post audit stamp.
        self = self._eh_workflow_action()
        if self.state != 'active':
            raise UserError(_(
                "Mandate %s is %s; only active mandates can be "
                "consumed.",
                self.mandate_id, self.state,
            ))
        if self._eh_is_expired_at(collection_date):
            # Same-transaction fallback for a newly-created/uncommitted
            # mandate, which the independent cursor cannot see. Persisted
            # production mandates take the durable path above.
            self.state = 'expired'
            raise UserError(_(
                "Mandate %s has been dormant for more than %d months "
                "and is now expired. Issue a new mandate before "
                "collecting again.",
                self.mandate_id, _SEPA_MANDATE_EXPIRY_MONTHS,
            ))

        # Atomic compare-and-set: increment the counter and decide the
        # sequence type from the post-update value returned by the
        # database. Two concurrent runs both see RETURNING and one will
        # get 1 (FRST), the other 2 (RCUR). Pre-update reads cannot
        # race the assignment.
        self.flush_recordset(['collection_count', 'last_collection_date'])
        self.env.cr.execute(SQL(
            "UPDATE eh_sepa_mandate "
            "SET collection_count = collection_count + 1, "
            "    last_collection_date = %s "
            "WHERE id = %s "
            "RETURNING collection_count",
            collection_date, self.id,
        ))
        row = self.env.cr.fetchone()
        if not row:
            raise UserError(_(
                "Mandate %s vanished mid-collection.", self.mandate_id,
            ))
        new_count = int(row[0])
        if self.is_one_off:
            seq_type = 'OOFF'
            mark_final = True
        elif mark_final:
            seq_type = 'FNAL'
        elif new_count == 1:
            seq_type = 'FRST'
        else:
            seq_type = 'RCUR'
        self.invalidate_recordset(
            ['collection_count', 'last_collection_date'],
        )
        if mark_final:
            self.state = 'completed'
        self.message_post(body=_(
            "Mandate consumed for collection on %(date)s as %(seq)s.",
            date=collection_date, seq=seq_type,
        ))
        return seq_type

    def _eh_is_expired_at(self, collection_date):
        self.ensure_one()
        basis_date = self.last_collection_date or self.signature_date
        if not basis_date:
            return False
        from dateutil.relativedelta import relativedelta
        cutoff = basis_date + relativedelta(
            months=_SEPA_MANDATE_EXPIRY_MONTHS,
        )
        return collection_date > cutoff

    # ---- cron: dormancy expiry ----

    @api.model
    def _cron_expire_dormant(self, batch_size=500):
        """Flip dormant mandates to expired so they cannot be used.

        A live cron pass per day enforces the SEPA 36-month rule; a
        mandate that has not been used in 36 months cannot be consumed
        until a fresh mandate is signed. The check is also done at
        consume time, but the cron makes the lifecycle visible in the
        list view without waiting for a collection attempt.
        """
        from dateutil.relativedelta import relativedelta
        cutoff = fields.Date.context_today(self) - relativedelta(
            months=_SEPA_MANDATE_EXPIRY_MONTHS,
        )
        candidates = self.search(
            [
                ('state', '=', 'active'),
                '|',
                ('last_collection_date', '<', cutoff),
                '&',
                ('last_collection_date', '=', False),
                ('signature_date', '<', cutoff),
            ],
            limit=batch_size,
        )
        def _expire(mandate):
            mandate._eh_lock_for_update()
            mandate = mandate._eh_workflow_action()
            if mandate.state != 'active' \
                    or (mandate.last_collection_date or mandate.signature_date) >= cutoff:
                return
            mandate.state = 'expired'
            mandate.message_post(body=_(
                "Auto-expired after %d months of dormancy.",
                _SEPA_MANDATE_EXPIRY_MONTHS,
            ))

        self._eh_for_each_savepoint(
            candidates, _expire, log_label="Mandate dormancy expiry",
        )


class EhSepaMandateAmendment(models.Model):
    """Amendment trail per SEPA scheme.

    The scheme requires that every collection following an amendment
    (IBAN change, name change, or scheme change) carries the AmdmntInf
    block in MndtRltdInf. eh.sepa.mandate.write() records a row here
    whenever one of those fields changes on an active mandate, and the
    batch export renders the latest amendment into AmdmntInfDtls.
    """
    _name = 'eh.sepa.mandate.amendment'
    _description = "SEPA mandate amendment record"
    _order = 'amended_at desc, id desc'

    mandate_id = fields.Many2one(
        'eh.sepa.mandate', required=True, ondelete='restrict',
        index=True,
    )
    amended_at = fields.Datetime(
        required=True, default=fields.Datetime.now,
    )
    amended_by_id = fields.Many2one(
        'res.users', required=True,
        default=lambda self: self.env.user,
    )
    engine_owned = fields.Boolean(
        readonly=True,
        copy=False,
        default=False,
        index=True,
        help=(
            "True only for amendment evidence created by the mandate engine. "
            "Legacy/untrusted rows are excluded from PAIN.008 rendering."
        ),
    )
    amendment_type = fields.Selection(
        [
            ('iban', "Debtor IBAN changed"),
            ('agent', "Debtor bank / agent changed"),
            ('name', "Debtor name changed"),
            ('scheme', "Scheme variant changed"),
            ('other', "Other"),
        ],
        required=True, default='other',
    )
    old_value = fields.Char()
    new_value = fields.Char()
    notes = fields.Text()
    reported_payment_id = fields.Many2one(
        'account.payment', readonly=True, copy=False, ondelete='restrict',
        index=True,
        help="First exact collection payment whose PAIN.008 carried this "
             "amendment. Re-cuts reuse that payment snapshot; later "
             "collections omit the amendment block.")
    reported_at = fields.Datetime(readonly=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(_(
                "SEPA mandate amendment evidence can only be created by "
                "the mandate engine.",
            ))
        for vals in vals_list:
            vals['engine_owned'] = True
        return super().create(vals_list)

    def write(self, vals):
        report_fields = {'reported_payment_id', 'reported_at'}
        if report_fields.intersection(vals) and not (
            self.env.context.get(_SEPA_DD_AMENDMENT_REPORT_CONTEXT)
            is _SEPA_DD_AMENDMENT_REPORT_CAPABILITY
        ):
            raise AccessError(_(
                "SEPA mandate amendment reporting evidence is engine-owned."
            ))
        if not set(vals).issubset(report_fields):
            raise AccessError(_(
                "SEPA mandate amendment evidence is append-only.",
            ))
        return super().write(vals)

    @api.private
    def _eh_mark_reported_by_payment(self, payment):
        self.ensure_one()
        if self.reported_payment_id and self.reported_payment_id != payment:
            raise UserError(_(
                "Mandate amendment was already reported by another "
                "collection payment."
            ))
        if not self.reported_payment_id:
            self.sudo().with_context(**{
                _SEPA_DD_AMENDMENT_REPORT_CONTEXT:
                    _SEPA_DD_AMENDMENT_REPORT_CAPABILITY,
            }).write({
                'reported_payment_id': payment.id,
                'reported_at': fields.Datetime.now(),
            })
        return True

    def unlink(self):
        if not self.env.su:
            raise AccessError(_(
                "SEPA mandate amendment evidence is append-only.",
            ))
        return super().unlink()
