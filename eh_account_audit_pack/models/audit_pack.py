# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.audit.pack: an audit-grade period close.

Enables the inalterable hash chain on the company's journals, scans the
period for integrity (no draft entries, every posted entry balanced and
hashed, no open suspense), and requires a segregated sign-off by a manager
other than the preparer, which advances the fiscal-year lock date.
"""

import inspect

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from odoo.addons.eh_account_base.tools.orm_compat import grouped_sum

# Segregation-of-duties identity anchors. Once set by the workflow these
# record who prepared and who signed off the period and must never be
# reassigned to a different user or cleared.
_SOD_ID_ANCHORS = ('prepared_by_id', 'signed_by_id')
# Timestamp anchors may not be written without (validly) re-recording the
# matching identity, so a preparation / sign-off time cannot be backdated.
_SOD_AT_ANCHORS = {
    'prepared_at': 'prepared_by_id',
    'signed_at': 'signed_by_id',
}
_AUDIT_ENGINE_CONTEXT = 'eh_audit_engine_capability'
_AUDIT_ENGINE_CAPABILITY = object()
_CONTROL_FIELDS = {
    'company_id', 'period_from', 'period_to', 'advance_lock',
    'close_run_ref', 'year_end_run_ref',
}


class EhAuditPack(models.Model):
    _name = 'eh.audit.pack'
    _description = "Audit pack (period integrity & sign-off)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'period_to desc, id desc'
    _rec_name = 'name'

    # The period-close state machine (draft -> checks_run -> signed_off) may
    # only advance through action_run_checks / action_sign_off, which run under
    # sudo. A direct RPC write of 'state' by a plain user is refused by the
    # inherited guard, closing the "write past the sign-off gate and its
    # integrity scan / lock-date advance" bypass. Engine evidence and
    # amendment links are guarded for the same reason. User-supplied control
    # inputs remain editable while draft, then write() freezes them as soon as
    # checks have run.
    _eh_guarded_fields = (
        'state', 'hash_snapshot', 'hash_chain_verified',
        'revision', 'supersedes_id', 'superseded_by_id', 'amendment_reason',
    )
    _eh_create_guarded_fields = (
        'state', 'hash_snapshot', 'hash_chain_verified',
        'revision', 'supersedes_id', 'superseded_by_id', 'amendment_reason',
    )

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('checks_run', "Checks Run"),
         ('signed_off', "Signed Off")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    period_from = fields.Date(required=True, tracking=True)
    period_to = fields.Date(required=True, tracking=True)
    advance_lock = fields.Boolean(
        default=True,
        readonly=True,
        help="Advance both the fiscal-year lock and, where supported, the "
             "irreversible hard lock to the period end on sign-off so no "
             "lock exception can reopen the vouched period.")

    check_ids = fields.One2many('eh.audit.check', 'pack_id', copy=False)
    has_blocking_failure = fields.Boolean(
        compute='_compute_check_state', store=True, index=True)

    # ---- period-control chain (opt-in cross-module links) ----
    #
    # The three period-control modules install independently. Reference fields
    # provide a real record picker without declaring a hard comodel dependency;
    # their dynamic selections are empty when the optional model is absent.
    # Unlike the former raw integer ids, a selected target is type checked,
    # displayed by name, and validated against the pack's company and period.
    close_run_ref = fields.Reference(
        selection='_selection_close_run',
        string="Linked period-close run",
        copy=False,
        help="Period-close run that must be approved before "
             "this period can be signed off. Left unset the close-approval "
             "gate is inactive.")
    year_end_run_ref = fields.Reference(
        selection='_selection_year_end_run',
        string="Linked year-end run",
        copy=False,
        help="Year-end run whose closing entry must be "
             "posted before this period can be signed off. Left unset the "
             "year-end gate is inactive.")
    chain_status = fields.Char(
        compute='_compute_chain_status',
        help="Human-readable state of the period-control chain: whether the "
             "linked close run is approved and the year-end close posted.")
    hash_chain_enabled = fields.Boolean(
        compute='_compute_hash_chain',
        help="Whether the inalterable hash chain is on for the company's "
             "posting journals.")
    hash_snapshot = fields.Char(
        copy=False, readonly=True,
        help="Database-side fingerprint of the hashed ledger when core hash "
             "integrity was last verified. An unchanged fingerprint lets "
             "sign-off reuse that expensive result while all other checks "
             "still run live.")
    hash_chain_verified = fields.Boolean(copy=False, readonly=True)

    prepared_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    prepared_at = fields.Datetime(readonly=True, copy=False)
    signed_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    signed_at = fields.Datetime(readonly=True, copy=False)

    revision = fields.Integer(default=1, required=True, readonly=True,
                              copy=False, index=True)
    supersedes_id = fields.Many2one(
        'eh.audit.pack', readonly=True, copy=False, ondelete='restrict',
        index=True, string="Amends")
    superseded_by_id = fields.Many2one(
        'eh.audit.pack', readonly=True, copy=False, ondelete='restrict',
        index=True, string="Amended By")
    amendment_reason = fields.Text(copy=False, readonly=True)
    lock_intact = fields.Boolean(
        compute='_compute_lock_intact', string="Signed Lock Intact",
        help="For a signed pack, both lock dates still cover the vouched "
             "period. This is re-evaluated from the live company controls.")

    notes = fields.Text()

    _unique_company_period = models.Constraint(
        'unique(company_id, period_to, revision)',
        'Only one audit-pack revision per company and period end.')

    @api.constrains('period_from', 'period_to')
    def _check_period_order(self):
        for pack in self:
            if (
                pack.period_from
                and pack.period_to
                and pack.period_from > pack.period_to
            ):
                raise ValidationError(_(
                    "The audit period start must be on or before its end."))

    @api.constrains('advance_lock')
    def _check_mandatory_lock(self):
        if any(not pack.advance_lock for pack in self):
            raise ValidationError(_(
                "Audit-grade sign-off must advance both company lock dates. "
                "The lock control cannot be disabled."
            ))

    @api.depends('state', 'period_to', 'company_id.fiscalyear_lock_date')
    def _compute_lock_intact(self):
        for pack in self:
            pack.lock_intact = pack._locks_cover_signed_period()

    def _locks_cover_signed_period(self):
        self.ensure_one()
        if self.state != 'signed_off' or not self.period_to:
            return True
        company = self.company_id.sudo()
        lock_fields = [
            name for name in ('fiscalyear_lock_date', 'hard_lock_date')
            if name in company._fields
        ]
        company.flush_recordset(lock_fields)
        company.invalidate_recordset(lock_fields, flush=False)
        return bool(lock_fields) and all(
            company[field_name]
            and company[field_name] >= self.period_to
            for field_name in lock_fields
        )

    def _assert_signed_lock_intact(self):
        self.ensure_one()
        self.invalidate_recordset(['state', 'lock_intact'], flush=False)
        if self.state == 'signed_off' and not self._locks_cover_signed_period():
            raise UserError(_(
                "The live company lock dates no longer cover %(period)s. "
                "Restore the vouched locks before creating an amendment.",
                period=self.period_to,
            ))

    def _assert_period_closed(self):
        """Refuse evidence over a period that has not ended yet."""
        self.ensure_one()
        today = fields.Date.context_today(self)
        if self.period_to and self.period_to > today:
            raise UserError(_(
                "The audit period ends on %(end)s, after today's date "
                "(%(today)s). Wait until the period has ended before running "
                "or signing its integrity checks.",
                end=self.period_to, today=today))

    @api.depends('check_ids.status', 'check_ids.is_blocking')
    def _compute_check_state(self):
        for pack in self:
            pack.has_blocking_failure = bool(pack.check_ids.filtered(
                lambda c: c.is_blocking and c.status == 'fail'))

    # ---- period-control chain resolution ----

    _CLOSE_MODEL = 'eh.close.run'
    _YEAR_END_MODEL = 'eh.year.end.run'

    @api.model
    def _selection_close_run(self):
        return [(self._CLOSE_MODEL, _("Period Close Run"))] \
            if self._CLOSE_MODEL in self.env else []

    @api.model
    def _selection_year_end_run(self):
        return [(self._YEAR_END_MODEL, _("Year-End Run"))] \
            if self._YEAR_END_MODEL in self.env else []

    @api.model
    def _selection_label(self, record, field_name):
        value = record[field_name]
        return dict(
            record._fields[field_name]._description_selection(record.env)
        ).get(value, value or _("Unknown"))

    @api.constrains(
        'company_id', 'period_from', 'period_to',
        'close_run_ref', 'year_end_run_ref',
    )
    def _check_chain_links(self):
        for pack in self:
            for field_name, model_name, start_name, end_name in (
                ('close_run_ref', self._CLOSE_MODEL,
                 'period_from', 'period_to'),
                ('year_end_run_ref', self._YEAR_END_MODEL,
                 'fiscal_year_start', 'fiscal_year_end'),
            ):
                target = pack[field_name]
                if not target:
                    continue
                target = target.sudo().exists()
                if not target or target._name != model_name:
                    raise ValidationError(_(
                        "The linked control run no longer exists or has the "
                        "wrong record type. Select it again from the picker."
                    ))
                if target.company_id != pack.company_id:
                    raise ValidationError(_(
                        "Linked run %(run)s belongs to %(run_company)s, not "
                        "audit-pack company %(pack_company)s.",
                        run=target.display_name,
                        run_company=target.company_id.display_name,
                        pack_company=pack.company_id.display_name,
                    ))
                if (
                    target[start_name]
                    and target[start_name] != pack.period_from
                ) or (
                    target[end_name]
                    and target[end_name] != pack.period_to
                ):
                    raise ValidationError(_(
                        "Linked run %(run)s does not cover the audit pack's "
                        "exact period %(start)s to %(end)s.",
                        run=target.display_name,
                        start=pack.period_from,
                        end=pack.period_to,
                    ))

    def _resolve_close_run(self):
        """Return the linked close run, or an empty recordset.

        Registry-guarded and elevated: auditors can render the pack even when
        record rules hide the linked run. Only its display/state are exposed.
        """
        self.ensure_one()
        if not self.close_run_ref or self._CLOSE_MODEL not in self.env:
            return None
        run = self.close_run_ref.sudo()
        return run if run._name == self._CLOSE_MODEL and run.exists() else None

    def _resolve_year_end_run(self):
        """Return the linked year-end run, or an empty recordset.

        Registry-guarded and elevated exactly like
        :meth:`_resolve_close_run`.
        """
        self.ensure_one()
        if not self.year_end_run_ref or self._YEAR_END_MODEL not in self.env:
            return None
        run = self.year_end_run_ref.sudo()
        return run if run._name == self._YEAR_END_MODEL and run.exists() else None

    @api.depends('close_run_ref', 'year_end_run_ref')
    def _compute_chain_status(self):
        for pack in self:
            parts = []
            close = pack._resolve_close_run()
            if close is not None:
                parts.append(_(
                    "Close run: %s",
                    pack._selection_label(close, 'state'),
                ))
            elif pack.close_run_ref:
                parts.append(_("Close run: Missing"))
            year_end = pack._resolve_year_end_run()
            if year_end is not None:
                parts.append(_(
                    "Year-end: %s",
                    pack._selection_label(year_end, 'state'),
                ))
            elif pack.year_end_run_ref:
                parts.append(_("Year-end: Missing"))
            pack.chain_status = "; ".join(parts) or _("No chain configured")

    def _compute_hash_chain(self):
        for pack in self:
            journals = self._posting_journals(pack.company_id)
            pack.hash_chain_enabled = bool(journals) and all(
                self._journal_hash_on(j) for j in journals)

    @api.model_create_multi
    def create(self, vals_list):
        forged = set(_SOD_ID_ANCHORS) | set(_SOD_AT_ANCHORS)
        if (
            any(forged.intersection(vals) for vals in vals_list)
            and self.env.context.get(_AUDIT_ENGINE_CONTEXT)
            is not _AUDIT_ENGINE_CAPABILITY
        ):
            raise AccessError(_(
                "Audit-pack preparer and signer provenance is stamped "
                "only by workflow actions."
            ))
        prepared = []
        for incoming in vals_list:
            vals = dict(incoming)
            if vals.get('advance_lock') is False:
                raise ValidationError(_(
                    "Audit-grade sign-off must advance the company locks."
                ))
            if vals.get('name', '/') == '/':
                company = self.env['res.company'].browse(
                    vals.get('company_id') or self.env.company.id,
                ).exists()
                if not company:
                    raise ValidationError(_(
                        "Select an existing company for the audit pack."
                    ))
                if not self.env.su and company not in self.env.companies:
                    raise AccessError(_(
                        "The audit-pack company is outside your allowed "
                        "companies."
                    ))
                sequence_date = fields.Date.to_date(
                    vals.get('period_to') or fields.Date.context_today(self)
                )
                sequence = self._eh_company_sequence(company)
                vals['name'] = sequence.with_context(
                    ir_sequence_date=sequence_date,
                ).next_by_id(sequence_date=sequence_date) or '/'
            prepared.append(vals)
        return super().create(prepared)

    @api.model
    def _eh_company_sequence(self, company):
        """Return one race-safe counter owned by the pack company."""
        company.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM res_company WHERE id = %s FOR UPDATE",
            (company.id,),
        )
        Sequence = self.env['ir.sequence'].sudo().with_context(
            active_test=False,
        )
        sequence = Sequence.search([
            ('code', '=', 'eh.audit.pack'),
            ('company_id', '=', company.id),
        ], limit=1)
        if not sequence:
            sequence = Sequence.create({
                'name': _(
                    "EH Audit Pack Reference - %s", company.display_name,
                ),
                'code': 'eh.audit.pack',
                'prefix': 'AUDIT/%s/%%(year)s/' % company.id,
                'padding': 4,
                'company_id': company.id,
                'use_date_range': True,
            })
        return sequence

    def write(self, vals):
        engine = (
            self.env.context.get(_AUDIT_ENGINE_CONTEXT)
            is _AUDIT_ENGINE_CAPABILITY
        )
        if not engine and set(vals).intersection(_CONTROL_FIELDS):
            frozen = self.filtered(lambda pack: pack.state != 'draft')
            if frozen:
                raise AccessError(_(
                    "Company, period, mandatory lock and chain links are "
                    "frozen after integrity checks run. Create a controlled "
                    "amendment after sign-off instead of changing the gate."
                ))
            if vals.get('advance_lock') is False:
                raise ValidationError(_(
                    "Audit-grade sign-off must advance the company locks."
                ))
        if not engine and any(pack.state == 'signed_off' for pack in self):
            raise AccessError(_(
                "A signed-off audit pack is immutable. Add follow-up "
                "evidence in chatter; do not rewrite the signed record."
            ))
        self._eh_guard_sod_anchors(vals)
        return super().write(vals)

    def _eh_lock_transition(self):
        """Serialize checks, sign-off and amendment creation per pack."""
        if not self:
            return
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_audit_pack WHERE id IN %s FOR UPDATE",
            (tuple(sorted(self.ids)),),
        )
        self.invalidate_recordset([
            'state', 'signed_by_id', 'signed_at', 'superseded_by_id',
            'hash_snapshot', 'hash_chain_verified',
        ])

    def _eh_guard_sod_anchors(self, vals):
        """Reject every non-engine write to SoD identity/time anchors."""
        protected = set(_SOD_ID_ANCHORS) | set(_SOD_AT_ANCHORS)
        touched = protected.intersection(vals)
        if (
            touched
            and self.env.context.get(_AUDIT_ENGINE_CONTEXT)
            is not _AUDIT_ENGINE_CAPABILITY
        ):
            raise AccessError(_(
                "Audit-pack segregation-of-duties provenance is server-owned; "
                "cannot change %s.",
                ', '.join(sorted(touched)),
            ))

    def unlink(self):
        if any(pack.state == 'signed_off' for pack in self):
            raise UserError(_(
                "Signed-off audit packs cannot be deleted."
            ))
        return super().unlink()

    def action_open_amendment_wizard(self):
        self.ensure_one()
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can amend a signed pack."
            ))
        if self.state != 'signed_off':
            raise UserError(_("Only a signed-off audit pack can be amended."))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Create Audit-Pack Amendment"),
            'res_model': 'eh.audit.pack.amendment.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_pack_id': self.id},
        }

    def _create_amendment(self, reason):
        self.ensure_one()
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can amend a signed pack."
            ))
        if not (reason or '').strip():
            raise ValidationError(_("Document an amendment reason."))
        pack = self.sudo()
        pack._eh_lock_transition()
        if pack.state != 'signed_off':
            raise UserError(_("Only a signed-off audit pack can be amended."))
        if pack.superseded_by_id:
            raise UserError(_(
                "This pack already has amendment %(name)s.",
                name=pack.superseded_by_id.display_name,
            ))
        pack._assert_signed_lock_intact()
        engine_context = {
            _AUDIT_ENGINE_CONTEXT: _AUDIT_ENGINE_CAPABILITY,
        }
        amendment = self.env['eh.audit.pack'].sudo().with_context(
            engine_context,
        ).create({
            'company_id': pack.company_id.id,
            'period_from': pack.period_from,
            'period_to': pack.period_to,
            'advance_lock': True,
            'revision': pack.revision + 1,
            'supersedes_id': pack.id,
            'amendment_reason': reason.strip(),
            'close_run_ref': (
                '%s,%s' % (pack.close_run_ref._name, pack.close_run_ref.id)
                if pack.close_run_ref else False
            ),
            'year_end_run_ref': (
                '%s,%s' % (
                    pack.year_end_run_ref._name, pack.year_end_run_ref.id,
                ) if pack.year_end_run_ref else False
            ),
        })
        pack.with_context(engine_context).write({
            'superseded_by_id': amendment.id,
        })
        pack.message_post(body=_(
            "Controlled amendment %(amendment)s created by %(user)s. "
            "Reason: %(reason)s",
            amendment=amendment.display_name,
            user=self.env.user.display_name,
            reason=reason.strip(),
        ))
        amendment.message_post(body=_(
            "Amends signed pack %(original)s. Reason: %(reason)s",
            original=pack.display_name,
            reason=reason.strip(),
        ))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'eh.audit.pack',
            'res_id': amendment.id,
            'view_mode': 'form',
        }

    # ---- hash chain ----

    @api.model
    def _posting_journals(self, company):
        return self.env['account.journal'].sudo().search([
            ('company_id', 'child_of', company.id),
            ('type', 'in', ('sale', 'purchase', 'general', 'bank', 'cash')),
        ])

    @api.model
    def _journal_hash_on(self, journal):
        if 'restrict_mode_hash_table' in journal._fields:
            return bool(journal.restrict_mode_hash_table)
        return False

    def _hash_chain_corrupt(self):
        """Recompute the inalterable hash chain and return True when broken.

        Delegates to core account's canonical ``_check_hash_integrity``, which
        partitions the chain PER sequence prefix (the entry sequence resets each
        fiscal year, so a hashed journal spanning years holds several
        independent chains, each seeded from an empty previous hash) and handles
        every hash version across Odoo 16-19. A hand-rolled single-thread walk
        seeds the next prefix from the prior prefix's last hash and orders on the
        removed ``secure_sequence_number``, so it mis-flags a legitimate
        multi-year ledger; delegating to core avoids both faults and stays
        default-safe (a clean ledger reports every prefix verified).
        """
        self.ensure_one()
        company = (self.company_id or self.env.company).sudo()
        check = getattr(company, '_check_hash_integrity', None)
        if not check:
            return False
        try:
            # Core's integrity report uses a server-side cursor. A database
            # failure can abort that cursor/transaction; contain it inside a
            # savepoint before converting the failure to a closed gate.
            with self.env.cr.savepoint():
                report = check()
        except Exception:
            # A recompute that errors must not silently vouch the ledger.
            return True
        results = report.get('results', []) if isinstance(report, dict) \
            else (report or [])
        for r in results:
            if not isinstance(r, dict):
                continue
            status = (r.get('status') or '').lower()
            msg = '%s %s' % (r.get('msg_cover') or '', r.get('msg') or '')
            if status == 'corrupted' or 'corrupt' in msg.lower():
                return True
        return False

    def _ledger_integrity_fingerprint(self):
        """Return a compact database-side fingerprint of the hashed chain.

        The digest covers the core move/line integrity fields, stored hashes
        and chain ordering for the root company and every branch. PostgreSQL
        reduces each line and move to a fixed-size digest, so Python never
        materialises the full ledger. It is not evidence by itself: its sole
        purpose is deciding whether a previously successful *core* integrity
        walk can safely be reused at sign-off.
        """
        self.ensure_one()
        companies = self.env['res.company'].sudo().search([
            ('id', 'child_of', self.company_id.id),
        ])
        self.env.cr.execute("""
            WITH move_digests AS (
                SELECT move.id,
                       md5(concat_ws(chr(31),
                           move.id::text,
                           COALESCE(move.name, ''),
                           move.date::text,
                           move.journal_id::text,
                           move.company_id::text,
                           COALESCE(move.inalterable_hash, ''),
                           COALESCE(move.secure_sequence_number, 0)::text,
                           COALESCE(move.sequence_prefix, ''),
                           COALESCE(move.sequence_number, 0)::text,
                           COALESCE((
                               SELECT md5(COALESCE(string_agg(
                                   md5(concat_ws(chr(30),
                                       line.id::text,
                                       COALESCE(line.name, ''),
                                       line.debit::text,
                                       line.credit::text,
                                       line.account_id::text,
                                       COALESCE(line.partner_id, 0)::text
                                   )), '' ORDER BY line.id), ''))
                                 FROM account_move_line AS line
                                WHERE line.move_id = move.id
                           ), md5(''))
                       )) AS digest
                  FROM account_move AS move
                 WHERE move.company_id = ANY(%s)
                   AND move.inalterable_hash IS NOT NULL
                   AND move.inalterable_hash != ''
            )
            SELECT md5(COALESCE(string_agg(digest, '' ORDER BY id), ''))
              FROM move_digests
        """, (companies.ids,))
        return self.env.cr.fetchone()[0]

    def _assert_chain_complete(self):
        """Block sign-off unless every configured chain leg is complete.

        This enforces the ordered close chain end-to-end at the sign-off
        gate: close run approved (closed) -> year-end close posted -> audit
        pack signed. Legs left unlinked are silent, so a stand-alone audit
        pack still signs off. Verified against the live linked records rather
        than the stored check rows, so a link added after the last check run
        is honoured.
        """
        self.ensure_one()
        close = self._resolve_close_run()
        if self.close_run_ref and close is None:
            raise UserError(_(
                "The linked period-close run is missing. Select a valid run "
                "and run the integrity checks again."
            ))
        if close is not None and close.state != 'closed':
            raise UserError(_(
                "The linked period-close run '%s' is not approved (state "
                "'%s'). Approve the close run before signing off the period.",
                close.display_name, self._selection_label(close, 'state')))
        year_end = self._resolve_year_end_run()
        if self.year_end_run_ref and year_end is None:
            raise UserError(_(
                "The linked year-end run is missing. Select a valid run and "
                "run the integrity checks again."
            ))
        if year_end is not None and year_end.state != 'posted':
            raise UserError(_(
                "The linked year-end closing entry '%s' is not posted (state "
                "'%s'). Post the year-end close before signing off the "
                "period.", year_end.display_name,
                self._selection_label(year_end, 'state')))

    def _assert_hash_chain_active(self):
        """Block the caller unless an inalterable hash chain is in force.

        Sign-off and the lock-date advance vouch that the period can no
        longer be altered. That guarantee rests on the core inalterable
        hash chain: if the capability is absent (the version does not carry
        ``inalterable_hash`` / ``restrict_mode_hash_table``) or the hash
        table is off on any posting journal in scope, there is no chain and
        the vouch would be hollow. Treat that as a blocking failure, not a
        warning, so a manager cannot complete sign-off or advance the lock
        date over an unprotected ledger.
        """
        self.ensure_one()
        Move = self.env['account.move'].sudo()
        if ('inalterable_hash' not in Move._fields
                or 'restrict_mode_hash_table'
                not in self.env['account.journal']._fields):
            raise UserError(_(
                "This Odoo version does not provide the inalterable hash "
                "chain, so an audit-grade sign-off that advances the lock "
                "date cannot be vouched. Sign-off is blocked."))
        journals = self._posting_journals(self.company_id)
        unprotected = journals.filtered(
            lambda j: not self._journal_hash_on(j))
        if not journals or unprotected:
            raise UserError(_(
                "The inalterable hash chain is not active on every posting "
                "journal. Enable the hash chain before signing off so the "
                "period cannot be altered after the lock date."))

    def action_enable_hash_chain(self):
        self.ensure_one()
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can enable the hash chain."))
        journals = self._posting_journals(self.company_id)
        if 'restrict_mode_hash_table' not in self.env['account.journal']._fields:
            raise UserError(_(
                "This Odoo version does not expose the journal hash-chain "
                "setting."))
        secured = 0
        try:
            # Activation and historical hashing form one control. Never leave
            # journals labelled protected when period entries remain outside
            # the chain. The savepoint also keeps callers that catch UserError
            # from retaining a half-enabled configuration.
            with self.env.cr.savepoint():
                journals.filtered(
                    lambda j: not j.restrict_mode_hash_table
                ).write({'restrict_mode_hash_table': True})
                Move = self.env['account.move']
                historical = Move.search(
                    self._move_domain(state='posted')).filtered(
                    lambda m: self._journal_hash_on(m.journal_id)
                    and not m.inalterable_hash)
                if historical:
                    # Match core Secure Entries' deliberate gap tolerance and
                    # also include older unhashed entries before a later hash.
                    # Strict defaults can reject a valid historical sequence;
                    # swallowing that rejection previously made sign-off
                    # impossible while falsely reporting activation success.
                    hash_kwargs = {'force_hash': True}
                    chain_builder = getattr(
                        Move, '_get_chains_to_hash', None)
                    if chain_builder:
                        supported = inspect.signature(
                            chain_builder).parameters
                        for option, value in (
                            ('raise_if_gap', False),
                            ('raise_if_no_document', False),
                            ('include_pre_last_hash', True),
                        ):
                            if option in supported:
                                hash_kwargs[option] = value
                    historical.sudo()._hash_moves(**hash_kwargs)
                    historical.invalidate_recordset(['inalterable_hash'])
                    remaining = historical.filtered(
                        lambda move: not move.inalterable_hash)
                    if remaining:
                        raise UserError(_(
                            "%(count)d historical entr(ies) could not be "
                            "secured. Resolve unreconciled statements or "
                            "sequence defects before enabling the chain.",
                            count=len(remaining),
                        ))
                    secured = len(historical)
        except UserError as exc:
            journals.invalidate_recordset(['restrict_mode_hash_table'])
            raise UserError(_(
                "Hash-chain activation was rolled back because historical "
                "entries could not be secured: %s",
                exc,
            )) from exc
        self.message_post(body=_(
            "Inalterable hash chain enabled on %(j)d posting journal(s); "
            "%(s)d historical entr(ies) secured.",
            j=len(journals), s=secured))
        return True

    # ---- checks ----

    def _eh_refresh_check_rows(self, allow_cached_hash=False):
        """Rescan the ledger and rewrite the check rows from the result.

        Rows are created and replaced on an elevated recordset only after
        caller access is checked by the surrounding workflow action. Used
        both by action_run_checks and, as a
        recompute-at-the-gate guard, by action_sign_off, so a stale or tampered
        stored row cannot vouch a period that no longer passes the checks.
        """
        self.ensure_one()
        starting_snapshot = self._ledger_integrity_fingerprint()
        can_reuse = bool(
            allow_cached_hash
            and self.hash_snapshot
            and self.hash_snapshot == starting_snapshot
        )
        chain_corrupt = (
            not self.hash_chain_verified
            if can_reuse else self._hash_chain_corrupt()
        )
        checks = self._compute_checks(chain_corrupt=chain_corrupt)
        ending_snapshot = self._ledger_integrity_fingerprint()
        if ending_snapshot != starting_snapshot:
            # The ledger changed while the live scan was running. Recompute
            # every check once against the new snapshot and never reuse the
            # earlier chain result.
            chain_corrupt = self._hash_chain_corrupt()
            checks = self._compute_checks(chain_corrupt=chain_corrupt)
            ending_snapshot = self._ledger_integrity_fingerprint()
        engine_context = {_AUDIT_ENGINE_CONTEXT: _AUDIT_ENGINE_CAPABILITY}
        self.check_ids.sudo().with_context(engine_context).unlink()
        Check = self.env['eh.audit.check'].sudo().with_context(engine_context)
        for vals in checks:
            Check.create(dict(vals, pack_id=self.id))
        self.with_context(engine_context).write({
            'hash_snapshot': ending_snapshot,
            'hash_chain_verified': not chain_corrupt,
        })

    def action_run_checks(self):
        self.ensure_one()
        # Guarded state/anchor writes below run as su (env.su True, real
        # env.user preserved for the SoD stamps and access checks).
        self = self._eh_workflow_action()
        self._eh_lock_transition()
        if self.state == 'signed_off':
            raise UserError(_("The period is already signed off."))
        self._assert_period_closed()
        self._eh_refresh_check_rows()
        vals = {'state': 'checks_run'}
        # Preparer-of-record is set once, on the first run, and is never
        # overwritten on re-run. Freezing it preserves segregation of duties:
        # a later re-run by a different user cannot silently reset the
        # preparer and thereby let the original preparer sign off.
        if not self.prepared_by_id:
            vals['prepared_by_id'] = self.env.user.id
            vals['prepared_at'] = fields.Datetime.now()
        self.with_context(**{
            _AUDIT_ENGINE_CONTEXT: _AUDIT_ENGINE_CAPABILITY,
        }).write(vals)
        return True

    def _compute_checks(self, chain_corrupt=None):
        self.ensure_one()
        checks = [
            self._check_no_draft(),
            self._check_balanced(),
            self._check_hashed(chain_corrupt=chain_corrupt),
            self._check_suspense(),
        ]
        # Period-control chain checks are appended only when the matching
        # link is configured. An unlinked pack yields exactly the four
        # checks above, so a stand-alone install and every pre-existing
        # test reproduces today's output unchanged.
        checks += self._chain_checks()
        return checks

    def _chain_checks(self):
        """Return the period-control chain checks that are in force.

        Each leg contributes a blocking check only when its link is set and
        the linked record still exists. A missing target module, an unset
        link, or a deleted target all leave the leg silent (opt-in), so the
        gate never fires on a stand-alone audit pack.
        """
        self.ensure_one()
        out = []
        close = self._resolve_close_run()
        if close is not None:
            out.append(self._check_close_approved(close))
        elif self.close_run_ref:
            out.append({
                'code': 'chain_close_approved',
                'name': _("Period-close run approved"),
                'status': 'fail', 'count': 1, 'is_blocking': True,
                'detail': _(
                    "The linked period-close run is missing; select a valid "
                    "run before sign-off."
                ),
            })
        year_end = self._resolve_year_end_run()
        if year_end is not None:
            out.append(self._check_year_end_posted(year_end))
        elif self.year_end_run_ref:
            out.append({
                'code': 'chain_year_end_posted',
                'name': _("Year-end closing entry posted"),
                'status': 'fail', 'count': 1, 'is_blocking': True,
                'detail': _(
                    "The linked year-end run is missing; select a valid run "
                    "before sign-off."
                ),
            })
        return out

    def _check_close_approved(self, close_run):
        # The period-close run must reach its terminal approved state
        # (closed). A run still open / in progress / pending approval, or a
        # reopened run, means the checklist controls are not signed off, so
        # the audit pack must not be signed off over it.
        approved = close_run.state == 'closed'
        return {
            'code': 'chain_close_approved',
            'name': _("Period-close run approved"),
            'status': 'pass' if approved else 'fail',
            'count': 0 if approved else 1,
            'is_blocking': True,
            'detail': '' if approved else _(
                "The linked period-close run '%s' is in state '%s'; it must "
                "be approved (closed) before sign-off.",
                close_run.display_name,
                self._selection_label(close_run, 'state')),
        }

    def _check_year_end_posted(self, year_end_run):
        # The year-end closing entry must be posted. A run that is draft /
        # computed / cancelled has not zeroed income and expense to retained
        # earnings, so the accounting for the fiscal year is not complete.
        # A reversed run means the close was undone, which is also not a
        # completed close.
        posted = year_end_run.state == 'posted'
        return {
            'code': 'chain_year_end_posted',
            'name': _("Year-end closing entry posted"),
            'status': 'pass' if posted else 'fail',
            'count': 0 if posted else 1,
            'is_blocking': True,
            'detail': '' if posted else _(
                "The linked year-end run '%s' is in state '%s'; its closing "
                "entry must be posted before sign-off.",
                year_end_run.display_name,
                self._selection_label(year_end_run, 'state')),
        }

    def _move_domain(self, state=None):
        domain = [
            ('company_id', 'child_of', self.company_id.id),
            ('date', '>=', self.period_from),
            ('date', '<=', self.period_to),
        ]
        if state:
            domain.append(('state', '=', state))
        return domain

    def _check_no_draft(self):
        count = self.env['account.move'].sudo().search_count(
            self._move_domain(state='draft'))
        return {
            'code': 'no_draft', 'name': _("No draft entries in period"),
            'status': 'fail' if count else 'pass', 'count': count,
            'is_blocking': True,
            'detail': _("%d draft entries must be posted or deleted.", count)
            if count else '',
        }

    def _check_balanced(self):
        # Aggregate in PostgreSQL by move. Python receives one scalar per
        # entry, never every journal item in the period.
        domain = [
            ('move_id.company_id', 'child_of', self.company_id.id),
            ('move_id.state', '=', 'posted'),
            ('date', '>=', self.period_from),
            ('date', '<=', self.period_to),
        ]
        totals = grouped_sum(
            self.env['account.move.line'].sudo(), domain, 'move_id', 'balance',
        )
        moves = self.env['account.move'].sudo().browse(
            [move_id for move_id, _total in totals if move_id]
        )
        currency_by_move = {
            move.id: move.company_id.currency_id or self.currency_id
            for move in moves
        }
        count = sum(
            1 for move_id, total in totals
            if move_id
            and not currency_by_move[move_id].is_zero(total)
        )
        return {
            'code': 'balanced', 'name': _("Posted entries balanced"),
            'status': 'fail' if count else 'pass', 'count': count,
            'is_blocking': True,
            'detail': _("%d posted entries do not balance.", count)
            if count else '',
        }

    def _check_hashed(self, chain_corrupt=None):
        Move = self.env['account.move'].sudo()
        if 'inalterable_hash' not in Move._fields:
            return {
                'code': 'hashed', 'name': _("Posted entries hashed"),
                'status': 'fail', 'count': 0, 'is_blocking': True,
                'detail': _("The inalterable hash chain is not available in "
                            "this version, so the period cannot be vouched "
                            "as unalterable."),
            }
        unhashed_count = Move.search_count(
            self._move_domain(state='posted') + [
                ('journal_id.restrict_mode_hash_table', '=', True),
                ('inalterable_hash', '=', False),
            ]
        )
        # Presence of a hash is necessary but not sufficient: recompute the
        # inalterable chain (via core's per-prefix integrity check) and confirm
        # every link. A tampered hash or hashed field breaks the recompute, so a
        # forged hash cannot pass this gate.
        if chain_corrupt is None:
            chain_corrupt = self._hash_chain_corrupt()
        count = unhashed_count + (1 if chain_corrupt else 0)
        if not count:
            detail = ''
        elif unhashed_count and chain_corrupt:
            detail = _(
                "%(unhashed)d posted entries are not hashed and the "
                "inalterable hash chain fails re-verification (tampered); the "
                "period cannot be vouched as unalterable.",
                unhashed=unhashed_count)
        elif chain_corrupt:
            detail = _(
                "The inalterable hash chain fails re-verification (a link does "
                "not recompute); the period has been tampered with and cannot "
                "be vouched as unalterable.")
        else:
            detail = _(
                "%d posted entries on a hash-restricted journal are not "
                "hashed. Use Enable Hash Chain on this pack to secure the "
                "existing posted entries, then run the checks again.",
                unhashed_count)
        return {
            'code': 'hashed', 'name': _("Posted entries hashed"),
            'status': 'fail' if count else 'pass', 'count': count,
            'is_blocking': True,
            'detail': detail,
        }

    def _check_suspense(self):
        journals = self.env['account.journal'].sudo().search([
            ('type', 'in', ('bank', 'cash')),
            ('company_id', 'child_of', self.company_id.id)])
        suspense = journals.mapped('suspense_account_id')
        suspense_count = 0
        if suspense:
            suspense_count = self.env['account.move.line'].sudo().search_count([
                ('account_id', 'in', suspense.ids),
                ('reconciled', '=', False),
                ('parent_state', '=', 'posted'),
                ('company_id', 'child_of', self.company_id.id),
                ('date', '<=', self.period_to),
            ])
        statement_count = 0
        statement_model = self.env['account.bank.statement.line'].sudo()
        domain_builder = getattr(
            self.company_id.sudo(),
            '_get_unreconciled_statement_lines_domain',
            None,
        )
        if domain_builder:
            statement_count = statement_model.search_count(
                domain_builder(self.period_to)
            )
        count = suspense_count + statement_count
        return {
            'code': 'suspense', 'name': _("No open bank/cash suspense"),
            'status': 'fail' if count else 'pass', 'count': count,
            'is_blocking': True,
            'detail': _(
                "%(suspense)d suspense journal item(s) and %(statement)d "
                "unreconciled bank statement line(s) remain. Reconcile or "
                "delete them before sign-off can advance the hard lock.",
                suspense=suspense_count,
                statement=statement_count,
            )
            if count else '',
        }

    # ---- sign-off ----

    def action_sign_off(self):
        self.ensure_one()
        # Guarded state/anchor writes below run as su. sudo preserves env.user,
        # so the manager and segregation-of-duties checks still evaluate against
        # the real acting user.
        self = self._eh_workflow_action()
        self._eh_lock_transition()
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can sign off a period."))
        if self.state != 'checks_run':
            raise UserError(_(
                "Run the integrity checks before signing off."))
        self._assert_period_closed()
        # Recompute the checks against the live ledger at the gate, so a stale
        # or hand-tampered stored check row cannot vouch a period that has
        # since gained a draft / unbalanced / unhashed entry. The freshly
        # written rows drive has_blocking_failure below.
        self._eh_refresh_check_rows(allow_cached_hash=True)
        if self.has_blocking_failure:
            raise UserError(_(
                "A blocking integrity check has failed; resolve it before "
                "signing off."))
        if self.prepared_by_id and self.prepared_by_id.id == self.env.user.id:
            raise UserError(_(
                "Segregation of duties: %s ran the checks and cannot also "
                "sign off. Another manager must sign off this period.",
                self.prepared_by_id.display_name))
        # The period-control chain must be complete: any linked close run
        # approved and any linked year-end close posted. This is re-verified
        # live (not read off possibly-stale check rows) so linking a run and
        # signing off without re-running the checks is still blocked.
        self._assert_chain_complete()
        # The inalterable hash chain must be in force before the period can be
        # vouched and the mandatory lock dates advanced.
        self._assert_hash_chain_active()
        # Apply the locks before stamping the signed state. Core validates
        # drafts, hashes and unreconciled bank statements here; any refusal is
        # therefore raised while the pack is still in checks_run.
        self._advance_lock_date()
        self.with_context(**{
            _AUDIT_ENGINE_CONTEXT: _AUDIT_ENGINE_CAPABILITY,
        }).write({
            'state': 'signed_off',
            'signed_by_id': self.env.user.id,
            'signed_at': fields.Datetime.now(),
        })
        self.message_post(body=_("Period signed off."))
        return True

    def _advance_lock_date(self):
        self.ensure_one()
        company = self.company_id
        values = {}
        for field_name in ('fiscalyear_lock_date', 'hard_lock_date'):
            if field_name not in company._fields:
                continue
            current = company[field_name]
            if not current or current < self.period_to:
                values[field_name] = self.period_to
        if values:
            company.sudo().write(values)


class EhAuditPackAmendmentWizard(models.TransientModel):
    _name = 'eh.audit.pack.amendment.wizard'
    _description = "Create a controlled audit-pack amendment"

    pack_id = fields.Many2one(
        'eh.audit.pack', required=True, readonly=True, ondelete='cascade')
    reason = fields.Text(required=True)

    def action_confirm(self):
        self.ensure_one()
        return self.pack_id._create_amendment(self.reason)


class EhAuditCheck(models.Model):
    _name = 'eh.audit.check'
    _description = "Audit pack integrity check result"
    _order = 'pack_id, id'

    pack_id = fields.Many2one(
        'eh.audit.pack', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='pack_id.company_id', store=True, readonly=True)
    code = fields.Char(required=True)
    name = fields.Char(required=True)
    status = fields.Selection(
        [('pass', "Pass"), ('warn', "Warning"), ('fail', "Fail")],
        default='pass', required=True)
    count = fields.Integer()
    is_blocking = fields.Boolean()
    detail = fields.Char()

    # ---- system-written / append-only ----
    # These rows ARE the sign-off blocking gate
    # (eh.audit.pack.has_blocking_failure). They carry the result of an
    # automated integrity scan and are written only by the pack's check
    # refresh. A direct create / write / unlink - by anyone, including a
    # manager - could flip a failed blocking check to 'pass' and clear the
    # gate without fixing the underlying issue, so every mutation must carry
    # the internal flag set by that refresh. The ACL grants no group direct
    # create / write / unlink on top of this env.su guard.
    @api.model_create_multi
    def create(self, vals_list):
        if (
            self.env.context.get(_AUDIT_ENGINE_CONTEXT)
            is not _AUDIT_ENGINE_CAPABILITY
        ):
            raise AccessError(_(
                "Audit check rows are produced by Run Checks, not created "
                "directly."))
        return super().create(vals_list)

    def write(self, vals):
        if (
            self.env.context.get(_AUDIT_ENGINE_CONTEXT)
            is not _AUDIT_ENGINE_CAPABILITY
        ):
            raise AccessError(_(
                "Audit check rows record an automated integrity scan and "
                "cannot be edited directly; re-run the checks to refresh "
                "them."))
        return super().write(vals)

    def unlink(self):
        if (
            self.env.context.get(_AUDIT_ENGINE_CONTEXT)
            is not _AUDIT_ENGINE_CAPABILITY
        ):
            raise AccessError(_(
                "Audit check rows cannot be deleted directly; re-run the "
                "checks to refresh them."))
        return super().unlink()
