# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IFRS 7.35A-N credit-risk disclosure note fed from the ECL engine.

The staging table (gross carrying amount and loss allowance per IFRS 9
impairment stage), the allowance opening-to-closing reconciliation
(IFRS 7.35H/35I) and the simplified-approach provision-matrix summary
(IFRS 7.35N) are pulled straight from the latest posted expected credit
loss run instead of being typed in. The lookup is soft: the note works
without the ECL engine installed (manual rows only) and links up
automatically when it is. A manual stage row keyed by the preparer becomes
an override: the engine figures are stamped alongside it and any
disagreement is flagged as a discrepancy rather than silently accepted.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

# Parent figures frozen once the note is finalised. Writing any of these on a
# finalised note is refused so a signed-off credit-risk disclosure cannot be
# silently re-keyed or re-populated. Computed totals and discrepancy flags
# are never in this set, so they still recompute; 'state' is never in it, so
# the finalise / reopen transition always passes.
_CREDIT_NOTE_FROZEN_FIELDS = frozenset({
    'name', 'company_id', 'reporting_date', 'stage_line_ids',
    'recon_line_ids', 'matrix_line_ids', 'ecl_run_res_id', 'ecl_run_name',
    'ecl_run_approach', 'ecl_run_write_date', 'notes',
})

# IFRS 7.35H stage columns; POCI is its own reconciliation line.
CREDIT_STAGES = [
    ('1', "Stage 1 - performing (12-month ECL)"),
    ('2', "Stage 2 - significant increase in credit risk (lifetime ECL)"),
    ('3', "Stage 3 - credit-impaired (lifetime ECL)"),
    ('poci', "Purchased or originated credit-impaired"),
]


class EhFinCreditNote(models.Model):
    _name = 'eh.fin.credit.note'
    _description = "Credit-risk disclosure note (IFRS 7.35A-N)"
    _inherit = [
        'mail.thread', 'eh.workflow.guard', 'eh.disclosure.parent.guard',
    ]
    _order = 'reporting_date desc, id desc'
    _rec_name = 'name'
    _check_company_auto = True
    # State is a manager-gated machine (draft <-> finalised via the Finalise /
    # Reopen actions, which run under sudo). The inherited eh.workflow.guard
    # refuses any non-superuser direct write to it, so a plain user cannot
    # RPC-flip state past action_finalise and its lock.
    _eh_guarded_fields = ('state',)
    _eh_disclosure_server_fields = frozenset({
        'currency_id', 'ecl_run_res_id', 'ecl_run_name',
        'ecl_run_approach', 'ecl_run_write_date', 'total_gross',
        'total_allowance', 'total_net', 'has_discrepancy',
    })

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('finalised', "Finalised")],
        default='draft', required=True, copy=False, tracking=True,
        help="A finalised note is locked: its staging table, reconciliation "
             "and provision-matrix summary cannot be edited, re-populated or "
             "appended. Only a manager can finalise or reopen it. The "
             "computed totals and discrepancy flags still recompute.")
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    reporting_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
        help="The note feeds from the latest POSTED expected credit loss "
             "run on or before this date.")

    # Reference to the feeding ECL run. Kept as a plain id + name pair, not
    # a Many2one, because the ECL engine is a soft dependency: a relational
    # field to a model that may not exist would break the registry on an
    # install without eh_account_ecl.
    ecl_run_res_id = fields.Integer(
        string="ECL run id", readonly=True, copy=False,
        help="Database id of the posted eh.ecl.run this note last populated "
             "from. Plain id (not a link) so the note installs without the "
             "ECL engine.")
    ecl_run_name = fields.Char(
        string="ECL run", readonly=True, copy=False, tracking=True)
    ecl_run_approach = fields.Selection(
        [('simplified', "Simplified (provision matrix)"),
         ('general', "General (3-stage)")],
        readonly=True, copy=False, string="Measurement approach",
        help="Approach of the feeding run. The provision-matrix summary "
             "(IFRS 7.35N) is built only for a simplified run.")
    ecl_run_write_date = fields.Datetime(
        string="ECL source revision", readonly=True, copy=False,
        help="Revision timestamp of the posted ECL run when this note was "
             "populated; finalisation refuses a missing, reversed or changed "
             "source.")
    ecl_source_valid = fields.Boolean(
        compute='_compute_ecl_source_status', string="ECL source valid")
    ecl_source_status = fields.Selection([
        ('manual', "Manual note (no ECL source)"),
        ('valid', "Valid posted ECL source"),
        ('unavailable', "ECL source unavailable"),
        ('missing', "ECL source missing"),
        ('reversed', "ECL source reversed or no longer posted"),
        ('changed', "ECL source changed after population"),
        ('wrong_company', "ECL source belongs to another company"),
    ], compute='_compute_ecl_source_status', string="ECL source status")

    stage_line_ids = fields.One2many(
        'eh.fin.credit.stage.line', 'note_id', copy=False,
        string="Staging table")
    recon_line_ids = fields.One2many(
        'eh.fin.credit.recon.line', 'note_id', copy=False, readonly=True,
        string="Allowance reconciliation")
    matrix_line_ids = fields.One2many(
        'eh.fin.credit.matrix.line', 'note_id', copy=False, readonly=True,
        string="Provision matrix summary")

    total_gross = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Sum of the staging-table gross carrying amounts (manual "
             "override values where a preparer keyed a stage by hand).")
    total_allowance = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    total_net = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    has_discrepancy = fields.Boolean(
        compute='_compute_totals', store=True,
        help="True when any manually keyed stage row disagrees with the "
             "figures the ECL engine reports for that stage.")
    notes = fields.Text()

    @api.depends('stage_line_ids.gross_carrying', 'stage_line_ids.allowance',
                 'stage_line_ids.net_carrying',
                 'stage_line_ids.has_discrepancy')
    def _compute_totals(self):
        for note in self:
            note.total_gross = sum(
                note.stage_line_ids.mapped('gross_carrying'))
            note.total_allowance = sum(
                note.stage_line_ids.mapped('allowance'))
            note.total_net = sum(note.stage_line_ids.mapped('net_carrying'))
            note.has_discrepancy = any(
                note.stage_line_ids.mapped('has_discrepancy'))

    def _ecl_source_status(self):
        self.ensure_one()
        if not self.ecl_run_res_id:
            return 'manual'
        if 'eh.ecl.run' not in self.env:
            return 'unavailable'
        run = self.env['eh.ecl.run'].browse(self.ecl_run_res_id)
        try:
            run._eh_check_access('read')
            if not run.exists():
                return 'missing'
            if run.company_id != self.company_id:
                return 'wrong_company'
            if run.state != 'posted' or run.reversal_move_id:
                return 'reversed'
            if not self.ecl_run_write_date \
                    or run.write_date != self.ecl_run_write_date:
                return 'changed'
        except AccessError:
            return 'unavailable'
        return 'valid'

    @api.depends('ecl_run_res_id', 'ecl_run_write_date', 'company_id')
    def _compute_ecl_source_status(self):
        for note in self:
            status = note._ecl_source_status()
            note.ecl_source_status = status
            note.ecl_source_valid = status in ('manual', 'valid')

    def _eh_assert_ecl_source_valid(self):
        invalid = self.filtered(
            lambda note: note._ecl_source_status() not in ('manual', 'valid'))
        if invalid:
            raise UserError(_(
                "The ECL evidence feeding %(notes)s is unavailable, "
                "reversed, or changed. Reopen and repopulate the note from "
                "a currently posted ECL run before finalising.",
                notes=', '.join(invalid.mapped('display_name')),
            ))

    # --- populate from the ECL engine (soft lookup) ----------------------

    def action_populate(self):
        """Rebuild the staging table, reconciliation and provision-matrix
        summary from the latest posted ECL run on or before the reporting
        date.

        Idempotent: engine-origin rows are wiped and rebuilt; manually keyed
        stage rows are preserved and become overrides, with the engine
        figures stamped alongside and any disagreement flagged (IFRS 7.35H
        figures must not silently drift from the measurement engine)."""
        self = self._eh_disclosure_prepare_action(engine=True)
        for note in self:
            if note.state == 'finalised':
                raise UserError(_(
                    "Credit-risk note %s is finalised; it cannot be "
                    "re-populated. Ask a manager to reopen it first.",
                    note.name))
            if 'eh.ecl.run' not in self.env:
                raise UserError(_(
                    "Populating the credit-risk note requires the Expected "
                    "Credit Loss module (eh_account_ecl). Install it, or "
                    "key the staging table manually."))
            run = self.env['eh.ecl.run'].search([
                ('company_id', '=', note.company_id.id),
                ('state', '=', 'posted'),
                ('reporting_date', '<=', note.reporting_date),
            ], order='reporting_date desc, id desc', limit=1)
            if not run:
                raise UserError(_(
                    "No posted expected credit loss run exists for %(company)s "
                    "on or before %(date)s.",
                    company=note.company_id.display_name,
                    date=note.reporting_date))
            note.sudo().write({
                'ecl_run_res_id': run.id,
                'ecl_run_name': run.display_name,
                'ecl_run_approach': run.measurement_approach,
                'ecl_run_write_date': run.write_date,
            })
            note._populate_stage_table(run)
            note._populate_recon(run)
            note._populate_matrix(run)
        return True

    def action_populate_from_ecl(self):
        """Named alias for action_populate: feed the credit-risk note's
        staging table, allowance reconciliation and provision-matrix summary
        from the latest posted ECL run. Kept as a distinct entry point so the
        ECL-feed capability is discoverable by name (IFRS 7.35A-N)."""
        return self.action_populate()

    @staticmethod
    def _bucket_stage_key(bucket):
        """Reconciliation stage key of an ECL bucket; POCI is its own
        IFRS 7.35H line. Duplicated from the ECL engine's convention so the
        note stays decoupled from that module's internals."""
        return 'poci' if bucket.poci else (bucket.stage or '1')

    def _populate_stage_table(self, run):
        """Per-stage gross carrying amount and loss allowance (IFRS 7.35H,
        35L, 35M). Gross comes from the run's buckets; the allowance is the
        stage's closing figure from the run's reconciliation (net of posted
        write-offs), falling back to the measured ECL when a legacy run has
        no reconciliation rows."""
        self.ensure_one()
        currency = self.currency_id
        stages = [key for key, _label in CREDIT_STAGES]
        gross = dict.fromkeys(stages, 0.0)
        allowance = dict.fromkeys(stages, 0.0)
        for bucket in run.bucket_ids:
            gross[self._bucket_stage_key(bucket)] += bucket.gross_carrying
        if run.recon_ids:
            for recon in run.recon_ids:
                allowance[recon.stage] += recon.closing
        else:
            for bucket in run.bucket_ids:
                allowance[self._bucket_stage_key(bucket)] += \
                    bucket.ecl_effective
        Stage = self.env['eh.fin.credit.stage.line']
        self.stage_line_ids.filtered(lambda l: l.origin == 'ecl').unlink()
        manual_by_stage = {}
        for line in self.stage_line_ids:
            manual_by_stage.setdefault(line.stage, line)
        for stage in stages:
            g = currency.round(gross[stage])
            a = currency.round(allowance[stage])
            manual = manual_by_stage.get(stage)
            if manual:
                # Manual row = override-with-discrepancy: the preparer's
                # figures stand, the engine figures are stamped alongside
                # and any disagreement is flagged, never silently merged.
                manual.write({'engine_gross': g, 'engine_allowance': a,
                              'engine_linked': True})
            elif not currency.is_zero(g) or not currency.is_zero(a):
                Stage.create({
                    'note_id': self.id,
                    'stage': stage,
                    'origin': 'ecl',
                    'gross_carrying': g,
                    'allowance': a,
                    'engine_gross': g,
                    'engine_allowance': a,
                    'engine_linked': True,
                })

    def _populate_recon(self, run):
        """Mirror the run's per-stage opening-to-closing allowance roll
        (IFRS 7.35H / 35I) into the note. Pure copy, rebuilt on every
        populate; all-zero stages are skipped."""
        self.ensure_one()
        currency = self.currency_id
        self.recon_line_ids.unlink()
        Recon = self.env['eh.fin.credit.recon.line']
        for recon in run.recon_ids:
            figures = (recon.opening, recon.transfers_in,
                       recon.transfers_out, recon.remeasurement,
                       recon.writeoffs, recon.closing)
            if all(currency.is_zero(f) for f in figures):
                continue
            Recon.create({
                'note_id': self.id,
                'stage': recon.stage,
                'opening': recon.opening,
                'transfers_in': recon.transfers_in,
                'transfers_out': recon.transfers_out,
                'remeasurement': recon.remeasurement,
                'writeoffs': recon.writeoffs,
                'closing': recon.closing,
            })

    def _populate_matrix(self, run):
        """Provision-matrix summary for a simplified run (IFRS 7.35N): one
        row per matrix bucket with its ageing band, loss rate, gross
        carrying amount and measured lifetime ECL. Empty for a general
        (3-stage) run."""
        self.ensure_one()
        self.matrix_line_ids.unlink()
        if run.measurement_approach != 'simplified':
            return
        Matrix = self.env['eh.fin.credit.matrix.line']
        for bucket in run.bucket_ids:
            Matrix.create({
                'note_id': self.id,
                'name': bucket.name,
                'days_from': bucket.days_from,
                'days_to': bucket.days_to,
                'loss_rate': bucket.loss_rate,
                'gross_carrying': bucket.gross_carrying,
                'ecl': bucket.ecl_effective,
            })

    # --- draft / finalised lock -------------------------------------------

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can finalise or reopen a "
                "credit-risk note."))

    @api.model_create_multi
    def create(self, vals_list):
        # Creating a note already finalised would skip the manager-gated
        # action_finalise; require a manager for that path.
        if any(v.get('state') == 'finalised' for v in vals_list):
            self._check_manager()
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                company = self.env['res.company'].browse(
                    vals.get('company_id') or self.env.company.id)
                vals['name'] = self._eh_disclosure_next_sequence(
                    'eh.fin.credit.note', company) or '/'
        return super().create(vals_list)

    def write(self, vals):
        # Freeze the staging table, reconciliation and matrix once finalised
        # (a signed-off note is frozen for everyone; restate via a
        # manager-gated reopen). The state field itself is owned by the
        # inherited eh.workflow.guard, which refuses any non-superuser direct
        # write; the sanctioned finalise / reopen actions run under sudo.
        if _CREDIT_NOTE_FROZEN_FIELDS.intersection(vals):
            for note in self:
                if note.state == 'finalised':
                    raise UserError(_(
                        "Credit-risk note %s is finalised and cannot be "
                        "edited. Ask a manager to reopen it first.",
                        note.name))
        return super().write(vals)

    def unlink(self):
        for note in self:
            if note.state == 'finalised':
                raise UserError(_(
                    "Credit-risk note %s is finalised and cannot be "
                    "deleted. Ask a manager to reopen it first.", note.name))
        return super().unlink()

    def action_finalise(self):
        """Lock the note: staging table, reconciliation and matrix freeze.
        Manager only."""
        self = self._eh_disclosure_prepare_action(engine=True)
        self._eh_assert_ecl_source_valid()
        self.stage_line_ids._compute_net()
        self.stage_line_ids._compute_discrepancy()
        self._compute_totals()
        self.stage_line_ids.flush_recordset([
            'net_carrying', 'gross_discrepancy', 'allowance_discrepancy',
            'has_discrepancy',
        ])
        self.flush_recordset([
            'total_gross', 'total_allowance', 'total_net', 'has_discrepancy',
        ])
        for note in self:
            if note.state == 'finalised':
                raise UserError(_(
                    "Credit-risk note %s is already finalised.", note.name))
        self.sudo().write(
            {'state': 'finalised'})
        return True

    def action_reopen(self):
        """Return a finalised note to draft. Manager only."""
        self = self._eh_disclosure_prepare_action()
        self.sudo().write(
            {'state': 'draft'})
        return True


class EhFinCreditStageLine(models.Model):
    _name = 'eh.fin.credit.stage.line'
    _description = "Credit-risk staging table row (IFRS 7.35H)"
    _order = 'note_id, stage, id'
    _inherit = ['eh.disclosure.child.guard']
    _check_company_auto = True
    _eh_disclosure_parent_field = 'note_id'
    _eh_disclosure_server_fields = frozenset({
        'company_id', 'currency_id', 'origin', 'net_carrying',
        'engine_linked', 'engine_gross', 'engine_allowance',
        'gross_discrepancy', 'allowance_discrepancy', 'has_discrepancy',
    })
    _eh_disclosure_engine_origins = frozenset({'ecl'})

    note_id = fields.Many2one(
        'eh.fin.credit.note', required=True, ondelete='cascade', index=True,
        check_company=True)
    company_id = fields.Many2one(
        related='note_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='note_id.currency_id', store=True, readonly=True)

    stage = fields.Selection(CREDIT_STAGES, required=True)
    origin = fields.Selection(
        [('ecl', "ECL engine"), ('manual', "Manual")],
        default='manual', required=True,
        help="Engine rows are wiped and rebuilt on every populate. A manual "
             "row survives populate and overrides the engine figure for its "
             "stage, with the discrepancy flagged.")
    gross_carrying = fields.Monetary(
        currency_field='currency_id',
        help="Gross carrying amount of the stage (IFRS 7.35L/35M).")
    allowance = fields.Monetary(
        currency_field='currency_id',
        help="Loss allowance recognised against the stage (IFRS 7.35H).")
    net_carrying = fields.Monetary(
        compute='_compute_net', store=True, currency_field='currency_id',
        help="Gross carrying amount less the loss allowance.")

    engine_linked = fields.Boolean(
        readonly=True, copy=False,
        help="True once a populate stamped the ECL engine's figures for "
             "this stage on the row.")
    engine_gross = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False,
        help="Gross carrying amount the ECL engine reports for this stage.")
    engine_allowance = fields.Monetary(
        currency_field='currency_id', readonly=True, copy=False,
        help="Closing loss allowance the ECL engine reports for this stage "
             "(net of posted write-offs).")

    _unique_note_stage = models.Constraint(
        'unique(note_id, stage)',
        'Each impairment stage can appear only once per credit-risk note.',
    )
    gross_discrepancy = fields.Monetary(
        compute='_compute_discrepancy', store=True,
        currency_field='currency_id',
        help="Entered gross less the engine gross. Zero when tied.")
    allowance_discrepancy = fields.Monetary(
        compute='_compute_discrepancy', store=True,
        currency_field='currency_id',
        help="Entered allowance less the engine allowance. Zero when tied.")
    has_discrepancy = fields.Boolean(
        compute='_compute_discrepancy', store=True,
        help="True when the row is linked to engine figures and either the "
             "gross or the allowance disagrees with them beyond currency "
             "rounding.")

    @api.depends('gross_carrying', 'allowance')
    def _compute_net(self):
        for line in self:
            currency = line.currency_id or line.company_id.currency_id
            net = line.gross_carrying - line.allowance
            line.net_carrying = currency.round(net) if currency else net

    @api.depends('gross_carrying', 'allowance', 'engine_gross',
                 'engine_allowance', 'engine_linked')
    def _compute_discrepancy(self):
        for line in self:
            currency = line.currency_id or line.company_id.currency_id
            gd = line.gross_carrying - line.engine_gross
            ad = line.allowance - line.engine_allowance
            if currency:
                gd = currency.round(gd)
                ad = currency.round(ad)
            line.gross_discrepancy = gd if line.engine_linked else 0.0
            line.allowance_discrepancy = ad if line.engine_linked else 0.0
            if not line.engine_linked:
                line.has_discrepancy = False
            elif currency:
                line.has_discrepancy = not currency.is_zero(gd) \
                    or not currency.is_zero(ad)
            else:
                line.has_discrepancy = bool(gd or ad)

    @api.model_create_multi
    def create(self, vals_list):
        # A create-append hole silently moves the note totals, so appending
        # a stage row to a finalised note is refused (create guard is
        # required on child lines feeding frozen parents).
        notes = self.env['eh.fin.credit.note'].browse([
            v.get('note_id') for v in vals_list if v.get('note_id')])
        for note in notes:
            if note.state == 'finalised':
                raise UserError(_(
                    "Credit-risk note %s is finalised; no stage row can be "
                    "added. Ask a manager to reopen it first.", note.name))
        return super().create(vals_list)

    def write(self, vals):
        for line in self:
            if line.note_id.state == 'finalised':
                raise UserError(_(
                    "Credit-risk note %s is finalised; its stage rows "
                    "cannot be edited. Ask a manager to reopen it first.",
                    line.note_id.name))
        return super().write(vals)

    def unlink(self):
        for line in self:
            if line.note_id.state == 'finalised':
                raise UserError(_(
                    "Credit-risk note %s is finalised; its stage rows "
                    "cannot be removed. Ask a manager to reopen it first.",
                    line.note_id.name))
        return super().unlink()


class EhFinCreditReconLine(models.Model):
    _name = 'eh.fin.credit.recon.line'
    _description = "Loss-allowance reconciliation row (IFRS 7.35H)"
    _order = 'note_id, stage, id'
    _inherit = ['eh.disclosure.child.guard']
    _check_company_auto = True
    _eh_disclosure_parent_field = 'note_id'
    _eh_disclosure_server_fields = frozenset({'company_id', 'currency_id'})
    _eh_disclosure_engine_owned = True

    note_id = fields.Many2one(
        'eh.fin.credit.note', required=True, ondelete='cascade', index=True,
        check_company=True)
    company_id = fields.Many2one(
        related='note_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='note_id.currency_id', store=True, readonly=True)

    stage = fields.Selection(CREDIT_STAGES, required=True)
    opening = fields.Monetary(currency_field='currency_id')
    transfers_in = fields.Monetary(currency_field='currency_id')
    transfers_out = fields.Monetary(currency_field='currency_id')
    remeasurement = fields.Monetary(currency_field='currency_id')
    writeoffs = fields.Monetary(currency_field='currency_id')
    closing = fields.Monetary(currency_field='currency_id')

    _unique_note_stage = models.Constraint(
        'unique(note_id, stage)',
        'Each allowance stage can appear only once per reconciliation.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        notes = self.env['eh.fin.credit.note'].browse([
            v.get('note_id') for v in vals_list if v.get('note_id')])
        for note in notes:
            if note.state == 'finalised':
                raise UserError(_(
                    "Credit-risk note %s is finalised; no reconciliation "
                    "row can be added. Ask a manager to reopen it first.",
                    note.name))
        return super().create(vals_list)

    def write(self, vals):
        for line in self:
            if line.note_id.state == 'finalised':
                raise UserError(_(
                    "Credit-risk note %s is finalised; its reconciliation "
                    "rows cannot be edited. Ask a manager to reopen it "
                    "first.", line.note_id.name))
        return super().write(vals)

    def unlink(self):
        for line in self:
            if line.note_id.state == 'finalised':
                raise UserError(_(
                    "Credit-risk note %s is finalised; its reconciliation "
                    "rows cannot be removed. Ask a manager to reopen it "
                    "first.", line.note_id.name))
        return super().unlink()


class EhFinCreditMatrixLine(models.Model):
    _name = 'eh.fin.credit.matrix.line'
    _description = "Provision-matrix summary row (IFRS 7.35N)"
    _order = 'note_id, days_from, id'
    _inherit = ['eh.disclosure.child.guard']
    _check_company_auto = True
    _eh_disclosure_parent_field = 'note_id'
    _eh_disclosure_server_fields = frozenset({'company_id', 'currency_id'})
    _eh_disclosure_engine_owned = True

    note_id = fields.Many2one(
        'eh.fin.credit.note', required=True, ondelete='cascade', index=True,
        check_company=True)
    company_id = fields.Many2one(
        related='note_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='note_id.currency_id', store=True, readonly=True)

    name = fields.Char(required=True, help="Ageing band label.")
    days_from = fields.Integer()
    days_to = fields.Integer(
        help="Zero means open-ended (and over).")
    loss_rate = fields.Float(
        digits=(7, 4), string="Loss rate (%)")
    gross_carrying = fields.Monetary(currency_field='currency_id')
    ecl = fields.Monetary(
        currency_field='currency_id', string="Lifetime ECL")

    @api.model_create_multi
    def create(self, vals_list):
        notes = self.env['eh.fin.credit.note'].browse([
            v.get('note_id') for v in vals_list if v.get('note_id')])
        for note in notes:
            if note.state == 'finalised':
                raise UserError(_(
                    "Credit-risk note %s is finalised; no matrix row can "
                    "be added. Ask a manager to reopen it first.", note.name))
        return super().create(vals_list)

    def write(self, vals):
        for line in self:
            if line.note_id.state == 'finalised':
                raise UserError(_(
                    "Credit-risk note %s is finalised; its matrix rows "
                    "cannot be edited. Ask a manager to reopen it first.",
                    line.note_id.name))
        return super().write(vals)

    def unlink(self):
        for line in self:
            if line.note_id.state == 'finalised':
                raise UserError(_(
                    "Credit-risk note %s is finalised; its matrix rows "
                    "cannot be removed. Ask a manager to reopen it first.",
                    line.note_id.name))
        return super().unlink()
