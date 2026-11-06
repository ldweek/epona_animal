# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Bank statement import wizard.

User uploads a file, picks the format and (for CSV) the per-bank
profile, picks the target bank journal, clicks Import. The wizard:

1. Hashes the file content for the idempotent reimport guard.
2. Dispatches to the parser for the chosen format.
3. Creates an account.bank.statement record with the parsed lines.
4. Logs the result on eh.account.bank.statement.import.log so the audit
   trail records every attempt regardless of outcome.

Errors at parse time are caught and recorded on the log row. Lines that
match an already-imported unique_import_ref are skipped; the user sees
the skipped count in the wizard's success message.
"""

import base64
import binascii
import hashlib
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.eh_account_bank_statement_import.parsers import registry

_logger = logging.getLogger(__name__)

_MAX_IMPORT_BYTES = 20 * 1024 * 1024
_MAX_IMPORT_BASE64_BYTES = ((_MAX_IMPORT_BYTES + 2) // 3) * 4
_BANK_IMPORT_LOCK_NAMESPACE = 1162363465
_DELETED_STATEMENT_RECOVERY_CONTEXT = 'eh_deleted_statement_recovery'
_DELETED_STATEMENT_RECOVERY_CAPABILITY = object()


def _format_choices(model):
    # Odoo's Selection-callable contract passes the model recordset
    # as the single positional argument; the parser registry has no
    # use for it but we accept it to match the signature.
    return registry.format_choices()


class EhBankStatementImportWizard(models.TransientModel):
    _name = 'eh.account.bank.statement.import.wizard'
    _description = "Bank statement import wizard"

    journal_id = fields.Many2one(
        'account.journal', required=True,
        domain="[('type', 'in', ['bank', 'cash'])]",
    )
    profile_id = fields.Many2one(
        'eh.account.bank.statement.import.profile',
    )
    format_key = fields.Selection(
        selection=_format_choices, required=True, default='csv',
        string="File format",
    )
    file_data = fields.Binary(required=True, attachment=False)
    filename = fields.Char()

    statement_name = fields.Char(
        help="Optional. Defaults to the filename minus extension.",
    )

    @api.onchange('profile_id')
    def _onchange_profile_id(self):
        for wizard in self:
            if wizard.profile_id and wizard.profile_id.journal_id:
                wizard.journal_id = wizard.profile_id.journal_id
    reimport_deleted_statement = fields.Boolean(
        string="Re-import deleted statement",
        help=(
            "Manager recovery control. Allows this exact file to be imported "
            "again only when its prior successful statement was deleted. "
            "Existing statements and line-level duplicate keys remain "
            "protected."
        ),
    )

    def action_import(self):
        self.ensure_one()
        self.journal_id._eh_check_access('read')
        if self.profile_id:
            self.profile_id._eh_check_access('read')
        if not self.file_data:
            raise UserError(_("Pick a file to import."))
        if self.format_key == 'csv' and not self.profile_id:
            raise UserError(_(
                "CSV import requires a profile that maps the columns. "
                "Configure one under Configuration > Bank Statement "
                "Import Profiles, then try again.",
            ))

        encoded = self.file_data.encode('ascii') \
            if isinstance(self.file_data, str) else self.file_data
        if len(encoded) > _MAX_IMPORT_BASE64_BYTES:
            raise UserError(_(
                "Bank statement file is too large. Maximum size is %s MiB.",
                _MAX_IMPORT_BYTES // (1024 * 1024),
            ))
        try:
            content_bytes = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise UserError(_("Uploaded file is not valid base64 data.")) \
                from exc
        if len(content_bytes) > _MAX_IMPORT_BYTES:
            raise UserError(_(
                "Bank statement file is too large. Maximum size is %s MiB.",
                _MAX_IMPORT_BYTES // (1024 * 1024),
            ))
        file_hash = hashlib.sha256(content_bytes).hexdigest()

        # One transaction at a time per journal. Exact-file and per-line
        # duplicate checks below now execute against committed predecessor
        # state instead of racing two simultaneous uploads.
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            [_BANK_IMPORT_LOCK_NAMESPACE, self.journal_id.id],
        )

        # The import log is an append-only audit sink: ordinary accounting
        # users (group_eh_user) have read-only ACL on it, but the importer is
        # granted to them and writes a log on EVERY path (duplicate, error,
        # empty, success). Creating it in the user's own rights therefore
        # raised AccessError the instant a non-manager clicked Import, making
        # the feature unusable for the group it targets. Write the log via
        # sudo so recording an audit row never requires write rights on it
        # (and users still cannot tamper with existing rows).
        Log = self.env['eh.account.bank.statement.import.log'].sudo()
        existing_log = Log.search([
            ('file_hash', '=', file_hash),
            ('journal_id', '=', self.journal_id.id),
            ('state', '=', 'done'),
        ], limit=1)
        recover_deleted_statement = False
        if existing_log:
            existing_statements = (
                existing_log.statement_ids | existing_log.statement_id
            ).exists()
            if not existing_statements:
                if not self.reimport_deleted_statement:
                    self._eh_create_import_log({
                        'journal_id': self.journal_id.id,
                        'profile_id': self.profile_id.id,
                        'format_key': self.format_key,
                        'filename': self.filename,
                        'file_hash': file_hash,
                        'state': 'duplicate',
                        'statement_id': False,
                        'error_message': _(
                            "Prior successful statement was deleted. An EH "
                            "Accounting Manager must explicitly enable "
                            "Re-import deleted statement to restore it."
                        ),
                    })
                    return {
                        'type': 'ir.actions.client',
                        'tag': 'display_notification',
                        'params': {
                            'title': _("Manager recovery required"),
                            'message': _(
                                "This exact file was imported before, but "
                                "its statement was deleted. Ask an EH "
                                "Accounting Manager to approve recovery."
                            ),
                            'type': 'warning',
                            'sticky': True,
                            'next': self._goto_log(),
                        },
                    }
                if not self.env.user.has_group(
                    'eh_account_base.group_eh_manager'
                ):
                    raise UserError(_(
                        "Only an EH Accounting Manager can re-import a file "
                        "whose prior statement was deleted."
                    ))
                recover_deleted_statement = True
            else:
                self._eh_create_import_log({
                    'journal_id': self.journal_id.id,
                    'profile_id': self.profile_id.id,
                    'format_key': self.format_key,
                    'filename': self.filename,
                    'file_hash': file_hash,
                    'state': 'duplicate',
                    'statement_id': existing_statements[:1].id,
                    'statement_ids': [(6, 0, existing_statements.ids)],
                })
                return self._goto_log()

        try:
            parser = registry.get_parser(self.format_key)
            parsed = parser.parse(
                content_bytes,
                profile=self.profile_id if self.profile_id else None,
            )
        except Exception as exc:
            self._eh_create_import_log({
                'journal_id': self.journal_id.id,
                'profile_id': self.profile_id.id,
                'format_key': self.format_key,
                'filename': self.filename,
                'file_hash': file_hash,
                'state': 'error',
                'error_message': str(exc),
            }, durable=True)
            # Durable helper commits production evidence independently of
            # this request, which will roll back when UserError crosses RPC.
            self.env.flush_all()
            raise UserError(
                _("Import failed: %s") % str(exc),
            ) from exc

        try:
            # Run the create inside a savepoint so a database-level error
            # (e.g. a CHECK constraint) rolls back cleanly to here rather
            # than poisoning the whole transaction; otherwise the error
            # Error logging below would otherwise fail with
            # InFailedSqlTransaction.
            with self.env.cr.savepoint():
                materialiser = self
                if recover_deleted_statement:
                    materialiser = self.with_context(**{
                        _DELETED_STATEMENT_RECOVERY_CONTEXT:
                            _DELETED_STATEMENT_RECOVERY_CAPABILITY,
                    })
                parsed_statements = parsed.get('statements') or [parsed]
                if not all(
                        isinstance(item, dict) for item in parsed_statements):
                    raise UserError(_(
                        "Statement parser returned an invalid multi-statement "
                        "payload."
                    ))
                # Validate every declared account/currency before creating the
                # first row. The savepoint still guarantees all-or-nothing if
                # a later database constraint fails.
                for parsed_statement in parsed_statements:
                    materialiser._validate_currency(parsed_statement)
                    materialiser._validate_account_identity(parsed_statement)
                statements = self.env['account.bank.statement']
                line_count = 0
                skipped = 0
                for parsed_statement in parsed_statements:
                    statement, created_count, skipped_count = (
                        materialiser._materialise(parsed_statement)
                    )
                    statements |= statement
                    line_count += created_count
                    skipped += skipped_count
                source_attachments = materialiser._eh_attach_source_file(
                    statements, content_bytes,
                )
        except Exception as exc:
            self._eh_create_import_log({
                'journal_id': self.journal_id.id,
                'profile_id': self.profile_id.id,
                'format_key': self.format_key,
                'filename': self.filename,
                'file_hash': file_hash,
                'state': 'error',
                'error_message': str(exc),
            }, durable=True)
            self.env.flush_all()
            raise UserError(
                _("Import failed while creating the statement: %s")
                % str(exc),
            ) from exc

        # Every line was already imported (or the file had no lines): there
        # is nothing to create. A bank statement with no lines is invalid in
        # Odoo 18+ (the chk_bank_statement_valid constraint requires at least
        # one line), so we record the outcome and surface a notice instead of
        # creating an empty, illegal statement.
        if not statements:
            self._eh_create_import_log({
                'journal_id': self.journal_id.id,
                'profile_id': self.profile_id.id,
                'format_key': self.format_key,
                'filename': self.filename,
                'file_hash': file_hash,
                'state': 'duplicate' if skipped else 'done',
                'statement_id': False,
                'line_count': 0,
                'skipped_count': skipped,
            })
            message = (
                _("All %s line(s) in this file were already imported; no "
                  "statement was created.") % skipped
                if skipped else
                _("The file contained no statement lines to import.")
            )
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _("Nothing to import"),
                    'message': message,
                    'type': 'warning',
                    'sticky': False,
                    'next': self._goto_log(),
                },
            }

        self._eh_create_import_log({
            'journal_id': self.journal_id.id,
            'profile_id': self.profile_id.id,
            'format_key': self.format_key,
            'filename': self.filename,
            'file_hash': file_hash,
            'state': 'done',
            'statement_id': statements[:1].id,
            'statement_ids': [(6, 0, statements.ids)],
            'source_attachment_ids': [(6, 0, source_attachments.ids)],
            'line_count': line_count,
            'skipped_count': skipped,
        })
        return self._goto_statement(statements)

    def _eh_create_import_log(self, vals, durable=False):
        """Create importer-owned evidence with real actor/company stamps."""
        self.ensure_one()
        payload = dict(vals or {})
        payload.update({
            'imported_by_id': self.env.user.id,
            'company_id': self.journal_id.company_id.id,
        })
        Log = self.env['eh.account.bank.statement.import.log']
        if durable:
            durable_id = Log.record_failure_durable(payload)
            if durable_id:
                return Log.browse(durable_id)
        return Log.sudo().create(payload)

    def _validate_currency(self, parsed):
        """Guard against importing a file into a journal of the wrong
        currency.

        Every parser populates currency_code when the source declares one
        (CSV via the profile, OFX/CAMT/MT940 from the file). A bank
        journal's effective currency is its own currency_id, or the
        company currency when the journal leaves currency_id blank. If the
        file states a currency that is neither, importing would book the
        lines at face value in the wrong currency and silently corrupt the
        balance, so we stop with a precise error instead.
        """
        code = (parsed.get('currency_code') or '').strip().upper()
        if not code:
            return
        journal_currency = (
            self.journal_id.currency_id
            or self.journal_id.company_id.currency_id
        )
        if not journal_currency or not journal_currency.name:
            return
        if journal_currency.name.upper() == code:
            return
        raise UserError(_(
            "The statement file is in %(file_currency)s but the selected "
            "journal '%(journal)s' is in %(journal_currency)s. Choose a "
            "%(file_currency)s journal, or import a %(journal_currency)s "
            "file.",
            file_currency=code,
            journal=self.journal_id.display_name,
            journal_currency=journal_currency.name,
        ))

    @staticmethod
    def _eh_normalise_account_number(value):
        return ''.join(
            character for character in str(value or '').upper()
            if character.isalnum()
        )

    def _validate_account_identity(self, parsed):
        """Prove a source-declared account belongs to selected journal."""
        source = self._eh_normalise_account_number(
            parsed.get('account_id') or parsed.get('account_number'),
        )
        if not source:
            return
        bank_account = self.journal_id.bank_account_id
        candidates = set()
        if bank_account:
            for field_name in ('sanitized_acc_number', 'acc_number'):
                if field_name in bank_account._fields:
                    candidate = self._eh_normalise_account_number(
                        bank_account[field_name],
                    )
                    if candidate:
                        candidates.add(candidate)
        if not candidates:
            raise UserError(_(
                "The statement declares account %(source)s, but journal "
                "'%(journal)s' has no bank account number configured. "
                "Configure the journal account before importing.",
                source=parsed.get('account_id')
                or parsed.get('account_number'),
                journal=self.journal_id.display_name,
            ))
        matches = any(
            source == candidate
            or (
                min(len(source), len(candidate)) >= 6
                and (
                    source.endswith(candidate)
                    or candidate.endswith(source)
                )
            )
            for candidate in candidates
        )
        if not matches:
            raise UserError(_(
                "The statement declares account %(source)s, which does not "
                "match bank account %(journal_account)s on journal "
                "'%(journal)s'. Choose the correct journal.",
                source=parsed.get('account_id')
                or parsed.get('account_number'),
                journal_account=bank_account.acc_number,
                journal=self.journal_id.display_name,
            ))

    def _materialise(self, parsed):
        """Create account.bank.statement and lines from the parser dict.

        Returns (statement, line_count, skipped_count).
        """
        self._validate_currency(parsed)
        self._validate_account_identity(parsed)
        self._eh_stabilise_duplicate_source_refs(parsed.get('lines', []))
        StatementLine = self.env['account.bank.statement.line']
        Statement = self.env['account.bank.statement']
        prior_max_line = StatementLine.search([
            ('journal_id', '=', self.journal_id.id),
        ], order='id desc', limit=1)
        recovery = (
            self.env.context.get(_DELETED_STATEMENT_RECOVERY_CONTEXT)
            is _DELETED_STATEMENT_RECOVERY_CAPABILITY
            and self.reimport_deleted_statement
            and self.env.user.has_group('eh_account_base.group_eh_manager')
        )

        # Reimport guard at the line level: skip lines whose
        # unique_import_ref already exists on the journal. Stored refs are
        # prefixed with the journal id ("J<id>:<sha1>") so the single-column
        # unique constraint stays journal-scoped on every Odoo series
        # (journal_id has no SQL column on the line in 16/17).
        ref_field = 'unique_import_ref' if 'unique_import_ref' in StatementLine._fields else None
        existing_refs = set()
        existing_lines = StatementLine
        if ref_field:
            source_refs = {
                candidate
                for line in parsed.get('lines', [])
                for candidate in line.get(
                    '_eh_unique_import_ref_candidates',
                    [line.get('unique_import_ref')],
                )
                if candidate
            }
            refs = [
                StatementLine._eh_canonical_import_ref(
                    self.journal_id.id, candidate,
                )
                for candidate in source_refs
            ]
            if refs:
                existing_lines = StatementLine.search([
                    ('journal_id', '=', self.journal_id.id),
                    '|',
                    (ref_field, 'in', refs),
                    ('eh_provider_reference', 'in', sorted(source_refs)),
                ])
                existing_refs = set(existing_lines.mapped(ref_field))
                # Older live-feed rows stored only eh_provider_reference.
                # Project that exact bank reference into the canonical key in
                # memory so a later file import skips it without rewriting
                # historical rows or guessing from fuzzy text/amount data.
                existing_refs.update(
                    StatementLine._eh_canonical_import_ref(
                        self.journal_id.id, provider_ref,
                    )
                    for provider_ref in existing_lines.mapped(
                        'eh_provider_reference',
                    )
                    if provider_ref
                )

        statement_vals = {
            'journal_id': self.journal_id.id,
            'name': (
                self.statement_name
                or parsed.get('source_statement_id')
                or self.filename
                or _("Imported statement")
            ),
            'date': parsed.get('statement_date') or fields.Date.context_today(self),
        }
        if parsed.get('opening_balance') is not None:
            statement_vals['balance_start'] = parsed['opening_balance']

        # Pre-resolve partner names in one pass so a 5,000-line CSV
        # does not fire 5,000 res.partner lookups. Real statements
        # repeat the same payer dozens of times; we cache the lookup
        # per unique name.
        unique_partner_names = {
            line['partner_name']
            for line in parsed.get('lines', [])
            if line.get('partner_name')
        }
        partner_id_by_name = {}
        if unique_partner_names:
            Partner = self.env['res.partner']
            company_id = self.journal_id.company_id.id
            for name in unique_partner_names:
                partner = Partner.search([
                    ('name', '=ilike', name),
                    '|', ('company_id', '=', False),
                    ('company_id', '=', company_id),
                ], limit=1)
                if partner:
                    partner_id_by_name[name] = partner.id

        foreign_currency_by_code = {}
        foreign_codes = {
            str(line.get('foreign_currency_code') or '').upper()
            for line in parsed.get('lines', [])
            if line.get('foreign_currency_code')
        }
        if foreign_codes:
            currencies = self.env['res.currency'].search([
                ('name', 'in', sorted(foreign_codes)),
            ])
            foreign_currency_by_code = {
                currency.name.upper(): currency for currency in currencies
            }
            missing_codes = foreign_codes.difference(
                foreign_currency_by_code)
            if missing_codes:
                raise UserError(_(
                    "Statement references unknown transaction currencies: "
                    "%s.", ', '.join(sorted(missing_codes)),
                ))

        skipped = 0
        line_vals_list = []
        recovered_lines = StatementLine
        for line in parsed.get('lines', []):
            unique_ref = line.get('unique_import_ref')
            if unique_ref:
                unique_ref = StatementLine._eh_canonical_import_ref(
                    self.journal_id.id, unique_ref,
                )
            candidate_refs = {
                StatementLine._eh_canonical_import_ref(
                    self.journal_id.id, candidate,
                )
                for candidate in line.get(
                    '_eh_unique_import_ref_candidates',
                    [line.get('unique_import_ref')],
                )
                if candidate
            }
            matching_refs = candidate_refs.intersection(existing_refs)
            if matching_refs and recovery:
                orphan_matches = existing_lines.filtered(lambda existing: (
                    existing.unique_import_ref in matching_refs
                    and existing.journal_id == self.journal_id
                    and not existing.statement_id
                    and existing.date == fields.Date.to_date(line['date'])
                    and (
                        self.journal_id.currency_id
                        or self.journal_id.company_id.currency_id
                    ).compare_amounts(existing.amount, line['amount']) == 0
                ))
                if len(orphan_matches) == 1:
                    recovered_lines |= orphan_matches
                    continue
            if matching_refs:
                skipped += 1
                continue
            line_vals = {
                'date': line['date'],
                'amount': line['amount'],
                'payment_ref': (line.get('payment_ref') or '/')[:255],
                'narration': line.get('narration') or '',
                'journal_id': self.journal_id.id,
            }
            if ref_field and unique_ref:
                line_vals[ref_field] = unique_ref
            partner_name = line.get('partner_name')
            if partner_name:
                line_vals['partner_name'] = partner_name[:255]
            if partner_name and partner_name in partner_id_by_name:
                line_vals['partner_id'] = partner_id_by_name[partner_name]
            foreign_code = str(
                line.get('foreign_currency_code') or '').upper()
            if foreign_code:
                line_vals.update({
                    'foreign_currency_id':
                        foreign_currency_by_code[foreign_code].id,
                    'amount_currency': line.get('foreign_amount', 0.0),
                })
            line_vals_list.append(line_vals)

        # No new lines (all skipped as duplicates, or an empty file): do not
        # create a statement. An empty statement violates the core
        # chk_bank_statement_valid constraint in Odoo 18+. The caller treats
        # an empty recordset as "nothing imported".
        if not line_vals_list and not recovered_lines:
            return Statement, 0, skipped

        # End balance integrity on a partial-overlap re-import.
        #
        # The parsed closing_balance describes the file as a whole, i.e. the
        # balance after ALL of the file's lines. When some lines are skipped
        # as already-imported duplicates, stamping that full closing balance
        # onto balance_end_real would leave a statement whose end balance no
        # longer matches the lines it actually retained
        # (balance_start + sum(retained amounts) != closing_balance), i.e. an
        # inconsistent statement that reports a phantom reconciliation gap.
        #
        # Bank closing balance remains authoritative. When duplicate lines are
        # omitted from this incremental statement, derive its opening anchor
        # backwards from that closing balance and retained movements. This
        # preserves both bank evidence and the statement equation instead of
        # fabricating a different closing balance.
        closing_balance = parsed.get('closing_balance')
        if closing_balance is not None:
            if not skipped:
                statement_vals['balance_end_real'] = closing_balance
            elif line_vals_list or recovered_lines:
                currency = (
                    self.journal_id.currency_id
                    or self.journal_id.company_id.currency_id
                )
                retained_total = sum(
                    vals['amount'] for vals in line_vals_list
                ) + sum(recovered_lines.mapped('amount'))
                consistent_start = closing_balance - retained_total
                statement_vals['balance_start'] = (
                    currency.round(consistent_start)
                    if currency else consistent_start
                )
                statement_vals['balance_end_real'] = closing_balance

        statement_vals['line_ids'] = (
            ([(6, 0, recovered_lines.ids)] if recovered_lines else [])
            + [(0, 0, vals) for vals in line_vals_list]
        )
        statement = Statement.create(statement_vals)

        # Fuzzy duplicate scan. The unique_import_ref check above
        # catches re-imports of the exact same file. This second pass
        # catches the more interesting case: the same payment imported
        # from two different sources (CSV today, OFX tomorrow) with
        # different per-line references. Each new line is compared
        # against the journal's prior lines; matches are recorded
        # via eh_duplicate_of_id without deleting either side, so the
        # operator decides whether to drop the duplicate or keep it.
        flagged = 0
        new_lines = statement.line_ids - recovered_lines
        probable_duplicates = StatementLine._eh_find_probable_duplicates(
            new_lines, prior_max_id=prior_max_line.id or 0,
        )
        for new_line in new_lines:
            existing = probable_duplicates.get(new_line.id)
            if existing:
                new_line.eh_duplicate_of_id = existing[:1].id
                flagged += 1
        if flagged:
            statement.message_post(body=_(
                "Heritage import detected %(count)s probable duplicate "
                "line(s) against earlier transactions on this journal. "
                "Review the lines flagged with 'Duplicate of' before "
                "reconciling.",
                count=flagged,
            ))
        return statement, len(line_vals_list) + len(recovered_lines), skipped

    def _eh_attach_source_file(self, statements, content_bytes):
        """Pin exact uploaded bytes to every created statement."""
        self.ensure_one()
        if not statements:
            return self.env['ir.attachment']
        encoded = base64.b64encode(content_bytes)
        filename = self.filename or _("Imported bank statement")
        return self.env['ir.attachment'].create([
            {
                'name': filename,
                'type': 'binary',
                'datas': encoded,
                'res_model': 'account.bank.statement',
                'res_id': statement.id,
                'company_id': self.journal_id.company_id.id,
                'description': _(
                    "Exact source file for bank statement import. SHA-256: %s",
                    hashlib.sha256(content_bytes).hexdigest(),
                ),
            }
            for statement in statements
        ])

    @staticmethod
    def _eh_stabilise_duplicate_source_refs(lines):
        """Make repeated bank-supplied identifiers safe and idempotent.

        FITID, AcctSvcrRef and MT940 references are documented as unique,
        but real exports commonly repeat placeholders such as NONREF and
        NOTPROVIDED.  Absolute row numbers are not stable across re-exports,
        so disambiguate only within each (source reference, line content)
        group.  A partial later export can still match occurrence zero via
        the candidate list even when it contains only one of the formerly
        duplicated rows.
        """
        prepared = []
        count_by_source_ref = {}
        for line in lines:
            line_date = line.get('date')
            date_text = (
                line_date.isoformat()
                if hasattr(line_date, 'isoformat') else str(line_date or '')
            )
            content = '\x1f'.join((
                date_text,
                repr(float(line.get('amount') or 0.0)),
                str(line.get('payment_ref') or '').strip(),
                str(line.get('narration') or '').strip(),
                str(line.get('partner_name') or '').strip(),
            ))
            digest = hashlib.sha256(
                content.encode('utf-8'),
            ).hexdigest()[:20]
            source_ref = str(line.get('unique_import_ref') or '').strip()
            if not source_ref:
                # MT940 :61: can legitimately carry no bank reference.
                # Content-derived identity makes exact and reordered
                # re-imports idempotent without relying on row position.
                source_ref = 'ehcontent:%s' % digest
                line['unique_import_ref'] = source_ref
            prepared.append((line, source_ref, digest))
            count_by_source_ref[source_ref] = (
                count_by_source_ref.get(source_ref, 0) + 1
            )

        occurrence_by_content = {}
        for line, source_ref, digest in prepared:
            occurrence_key = (source_ref, digest)
            occurrence = occurrence_by_content.get(occurrence_key, 0)
            occurrence_by_content[occurrence_key] = occurrence + 1
            canonical_zero = '%s|ehdup:%s:0' % (source_ref, digest)
            if count_by_source_ref[source_ref] > 1:
                stable_ref = '%s|ehdup:%s:%s' % (
                    source_ref, digest, occurrence,
                )
                line['unique_import_ref'] = stable_ref
            else:
                stable_ref = source_ref
            line['_eh_unique_import_ref_candidates'] = sorted({
                source_ref,
                stable_ref,
                canonical_zero,
            })

    def _goto_log(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _("Import Log"),
            'res_model': 'eh.account.bank.statement.import.log',
            'view_mode': 'list,form',
            'domain': [('journal_id', '=', self.journal_id.id)],
        }

    def _goto_statement(self, statement):
        action = {
            'type': 'ir.actions.act_window',
            'name': _("Imported Statements"),
            'res_model': 'account.bank.statement',
            'view_mode': 'list,form',
            'domain': [('id', 'in', statement.ids)],
        }
        if len(statement) == 1:
            action.update({
                'name': _("Imported Statement"),
                'res_id': statement.id,
                'view_mode': 'form',
                'views': [(False, 'form')],
            })
        return action
