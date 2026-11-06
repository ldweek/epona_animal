# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
PDF render values and helpers for dynamic report PDFs.

Two pieces in this module:

1. An AbstractModel `report.eh_account_dynamic_reports.report_dynamic_pdf_template`
   that Odoo's reporting machinery picks up automatically when the Qweb
   template of the same name runs. Its `_get_report_values` returns the
   structure the template iterates over.

2. A model extension on `eh.account.dynamic.report` that adds `render_pdf`
   and `export_pdf_attachment`. These mirror the XLSX equivalents and let
   the OWL viewer (or any other caller) get a printable artifact.

The shared formatting helpers (_format_value, _line_css_class) are kept on
the AbstractModel so they are easy to override per locality if needed.
Concrete handlers do not need to know about PDF rendering at all; they
return the standard payload shape, and the template plus this helper do
the rest.
"""

import math

from odoo import api, models
from odoo.exceptions import UserError
from odoo.tools.misc import formatLang


_NUMERIC_FIGURE_TYPES = frozenset({
    'monetary', 'integer', 'float', 'percentage',
})

# A4 landscape remains readable with one hierarchy/label column and at most
# eight value columns.  Wider period x analytic (or Trial Balance) matrices
# are emitted as deterministic vertical chunks: every value expression appears
# once, while the label column is repeated so each physical page is usable in
# isolation.  The report axis itself may still use its full 48-column budget.
_PDF_VALUE_COLUMNS_PER_CHUNK = 8

# The precomputed payload may only travel from render_pdf() to QWeb inside the
# same Python process. An RPC caller can copy the context key names but cannot
# reproduce this object identity, so branded PDFs cannot be rendered from an
# unaudited caller-supplied payload.
_EH_PRECOMPUTED_PDF_CAPABILITY = object()

# Portal delivery needs a report execution/hash for traceability but must not
# leave statement source data in the attachment-backed shared cache.  Context
# text is forgeable over RPC; object identity is not.
_EH_EPHEMERAL_PDF_CONTEXT = 'eh_ephemeral_pdf_capability'
_EH_EPHEMERAL_PDF_CAPABILITY = object()


class EhDynamicReportPdf(models.AbstractModel):
    _name = 'report.eh_account_dynamic_reports.report_dynamic_pdf_template'
    _description = "Render values for the universal dynamic report PDF"

    @api.model
    def _get_report_values(self, docids, data=None):
        # ir.actions.report already gives the custom report model a private
        # rendering-context dict. Pop the in-process transport values so the
        # payload/capability never enters the eventual QWeb value namespace.
        data = data or {}
        precomputed_payload = data.pop('_eh_precomputed_payload', None)
        precomputed_report_id = data.pop('_eh_precomputed_report_id', None)
        precomputed_capability = data.pop(
            '_eh_precomputed_capability', None,
        )
        trusted_precomputed = bool(
            precomputed_capability is _EH_PRECOMPUTED_PDF_CAPABILITY
            and isinstance(precomputed_payload, dict)
            and precomputed_report_id
        )
        options = data.get('options') or {}
        # A PDF export must render the full detail, not the lazy on-demand
        # skeleton the OWL viewer requests (GL / Partner Ledger otherwise emit
        # headers and totals with zero transaction lines). Force eager.
        options = dict(options, eager_expand=True)
        options.pop('lazy_expand', None)
        DynamicReport = self.env['eh.account.dynamic.report']
        docs = DynamicReport.browse(docids)
        rendered = []
        for doc in docs:
            if trusted_precomputed and doc.id == int(precomputed_report_id):
                payload = precomputed_payload
            else:
                payload = doc.render(options)
            company, companies = self._resolve_pdf_company_scope(
                payload, options,
            )
            columns = payload.get('columns') or []
            lines = self._render_lines(payload)
            column_header_rows = self._normalise_column_header_rows(
                columns, payload.get('column_header_rows'),
            )
            rendered.append({
                'doc': doc,
                'payload': payload,
                'lines': lines,
                'column_header_rows': column_header_rows,
                'table_chunks': self._build_pdf_table_chunks(
                    columns, lines, column_header_rows,
                ),
                'company': company,
                'companies': companies,
                'company_scope_label': ', '.join(
                    companies.mapped('display_name')
                ),
            })
        return {
            'docs': docs,
            'doc_ids': docids,
            'doc_model': 'eh.account.dynamic.report',
            'data': data,
            'rendered': rendered,
        }

    @api.model
    def _resolve_pdf_company_scope(self, payload, options=None):
        """Resolve header branding from the audited report scope.

        Dynamic-report definitions are global records, so ``doc.env.company``
        says which company happened to be active in the web client, not which
        company owns the numbers.  Handler metadata is the authoritative,
        already-clamped scope.  A requested primary company may choose the
        branded header only when it is inside that scope.
        """
        payload = payload or {}
        options = options or {}
        raw_ids = (payload.get('meta') or {}).get('company_ids') or ()
        # The audited renderer clamps against user.company_ids, not merely the
        # companies currently ticked in the web-client context.  A wizard or
        # scheduled export may legitimately request another authorised company
        # without switching the active-company context first; do not brand
        # those numbers as the unrelated active company.
        allowed_ids = set(self.env.user.company_ids.ids)
        company_ids = []
        for raw_id in raw_ids:
            try:
                company_id = int(raw_id)
            except (TypeError, ValueError, OverflowError):
                continue
            if company_id in allowed_ids and company_id not in company_ids:
                company_ids.append(company_id)
        companies = self.env['res.company'].browse(company_ids).exists()
        if not companies:
            companies = self.env.company
            company_ids = companies.ids

        try:
            requested_primary = int(options.get('primary_company_id') or 0)
        except (TypeError, ValueError, OverflowError):
            requested_primary = 0
        if requested_primary not in company_ids:
            requested_primary = (
                self.env.company.id
                if self.env.company.id in company_ids
                else company_ids[0]
            )
        return (
            self.env['res.company'].browse(requested_primary).exists(),
            companies,
        )

    @api.model
    def _render_lines(self, payload):
        columns = payload.get('columns') or []
        currency = payload.get('currency') or {}
        rendered = []
        for line in payload.get('lines') or []:
            cells = []
            line_columns = line.get('columns') or []
            for i, line_col in enumerate(line_columns):
                col_def = columns[i + 1] if (i + 1) < len(columns) else {}
                figure_type = (
                    (line_col or {}).get('figure_type')
                    or col_def.get('figure_type', 'string')
                )
                cells.append({
                    'value': line_col.get('value') if line_col else None,
                    'display': self._format_value(
                        line_col.get('value') if line_col else None,
                        figure_type,
                        currency=currency,
                    ),
                    'align_right': figure_type in _NUMERIC_FIGURE_TYPES,
                })
            rendered.append({
                'id': line.get('id'),
                'name': line.get('name', ''),
                'level': int(line.get('level') or 0),
                'kind': (line.get('meta') or {}).get('kind', ''),
                'cells': cells,
                'css_class': self._line_css_class(line),
            })
        return rendered

    @api.model
    def _normalise_column_header_rows(self, columns, raw_rows):
        """Validate grouped PDF headings against flat payload columns.

        Malformed/stale optional metadata returns an empty list, making QWeb
        render the legacy flat header. This mirrors XLSX/viewer fail-closed
        behaviour and prevents a bad span from shifting accounting values.
        """
        if (
            not isinstance(columns, list) or not columns
            or not isinstance(raw_rows, list) or not raw_rows
        ):
            return []
        height = len(raw_rows)
        width = len(columns)
        occupied = [[False] * width for _unused in range(height)]
        normalised = []
        for row_index, raw_row in enumerate(raw_rows):
            if not isinstance(raw_row, list) or not raw_row:
                return []
            row = []
            cursor = 0
            for raw_cell in raw_row:
                while cursor < width and occupied[row_index][cursor]:
                    cursor += 1
                if not isinstance(raw_cell, dict):
                    return []
                colspan = raw_cell.get('colspan', 1)
                rowspan = raw_cell.get('rowspan', 1)
                if (
                    isinstance(colspan, bool) or isinstance(rowspan, bool)
                    or not isinstance(colspan, int)
                    or not isinstance(rowspan, int)
                    or colspan < 1 or rowspan < 1
                    or cursor + colspan > width
                    or row_index + rowspan > height
                ):
                    return []
                for y_pos in range(row_index, row_index + rowspan):
                    for x_pos in range(cursor, cursor + colspan):
                        if occupied[y_pos][x_pos]:
                            return []
                        occupied[y_pos][x_pos] = True
                name = raw_cell.get('name', '')
                if (
                    isinstance(name, bool)
                    or not isinstance(name, (str, int, float))
                    or (
                        isinstance(name, float)
                        and not math.isfinite(name)
                    )
                ):
                    return []
                row.append({
                    'name': str(name),
                    'colspan': colspan,
                    'rowspan': rowspan,
                    'is_label': cursor == 0,
                })
                cursor += colspan
            normalised.append(row)
        if any(not value for row in occupied for value in row):
            return []
        return normalised

    @api.model
    def _slice_pdf_header_rows(
        self, columns, header_rows, selected_indexes,
    ):
        """Clip a valid grouped header grid to a PDF column chunk.

        ``selected_indexes`` always contains the label column followed by one
        contiguous value-column range.  A period/analytic group crossing a
        chunk boundary is clipped to the cells actually printed.  The clipped
        grid is run through the normal validator again, so a future malformed
        caller still falls back to authoritative flat headings.
        """
        if not header_rows or not selected_indexes:
            return []
        width = len(columns)
        height = len(header_rows)
        occupied = [[False] * width for _unused in range(height)]
        positioned_rows = []
        for row_index, row in enumerate(header_rows):
            positioned = []
            cursor = 0
            for cell in row:
                while cursor < width and occupied[row_index][cursor]:
                    cursor += 1
                colspan = cell.get('colspan', 1)
                rowspan = cell.get('rowspan', 1)
                if (
                    cursor + colspan > width
                    or row_index + rowspan > height
                ):
                    return []
                for y_pos in range(row_index, row_index + rowspan):
                    for x_pos in range(cursor, cursor + colspan):
                        if occupied[y_pos][x_pos]:
                            return []
                        occupied[y_pos][x_pos] = True
                positioned.append((cursor, cell))
                cursor += colspan
            positioned_rows.append(positioned)
        if any(not value for row in occupied for value in row):
            return []

        clipped_rows = []
        for positioned in positioned_rows:
            clipped = []
            for start, cell in positioned:
                end = start + cell.get('colspan', 1)
                selected_span = sum(
                    1 for index in selected_indexes
                    if start <= index < end
                )
                if not selected_span:
                    continue
                clipped.append({
                    'name': cell.get('name', ''),
                    'colspan': selected_span,
                    'rowspan': cell.get('rowspan', 1),
                })
            if not clipped:
                return []
            clipped_rows.append(clipped)
        chunk_columns = [columns[index] for index in selected_indexes]
        return self._normalise_column_header_rows(
            chunk_columns, clipped_rows,
        )

    @api.model
    def _build_pdf_table_chunks(self, columns, lines, header_rows=None):
        """Return legible, deterministic PDF tables for a flat column axis.

        The first flat column is the row label and every rendered ``line``
        stores only the remaining value cells.  Chunks therefore repeat flat
        column zero and slice ``line['cells']`` with the matching zero-based
        value range.  No expression is duplicated or reordered.
        """
        if not isinstance(columns, list) or not columns:
            return []
        lines = lines if isinstance(lines, list) else []
        value_count = max(0, len(columns) - 1)
        starts = list(range(
            0, value_count, _PDF_VALUE_COLUMNS_PER_CHUNK,
        )) or [0]
        chunks = []
        chunk_count = len(starts)
        for chunk_index, value_start in enumerate(starts):
            value_end = min(
                value_count,
                value_start + _PDF_VALUE_COLUMNS_PER_CHUNK,
            )
            selected_indexes = [0] + list(range(
                value_start + 1, value_end + 1,
            ))
            chunk_columns = [columns[index] for index in selected_indexes]
            chunk_lines = []
            for line in lines:
                chunk_line = dict(line)
                cells = line.get('cells') or []
                chunk_line['cells'] = list(cells[value_start:value_end])
                chunk_lines.append(chunk_line)
            chunks.append({
                'index': chunk_index,
                'count': chunk_count,
                'value_from': value_start + 1 if value_count else 0,
                'value_to': value_end,
                'value_count': value_count,
                'columns': chunk_columns,
                'lines': chunk_lines,
                'column_header_rows': self._slice_pdf_header_rows(
                    columns, header_rows or [], selected_indexes,
                ),
            })
        return chunks

    @api.model
    def _format_value(self, value, figure_type, currency=None):
        """Return a display string for a value, applying accounting
        conventions: comma thousands, decimals from the currency block
        (default 2) for monetary, parentheses around negatives, currency
        symbol on monetary cells when the scope is single currency.
        Empty when value is None or empty string.
        """
        currency = currency or {}
        if value is None or value == '':
            return ''
        if figure_type == 'monetary':
            if isinstance(value, (int, float)):
                raw_decimals = currency.get('decimal_places')
                decimals = int(
                    2 if raw_decimals is None else raw_decimals
                )
                formatted = formatLang(
                    self.env, abs(value), digits=decimals, grouping=True,
                )
                symbol = currency.get('symbol') or ''
                multi = currency.get('multi_currency')
                if symbol and not multi:
                    if currency.get('position') == 'before':
                        body = "%s %s" % (symbol, formatted)
                    else:
                        body = "%s %s" % (formatted, symbol)
                else:
                    body = formatted
                return ("(%s)" % body) if value < 0 else body
            return str(value)
        if figure_type == 'integer':
            if isinstance(value, (int, float)):
                return formatLang(
                    self.env, int(value), digits=0, grouping=True,
                )
            return str(value)
        if figure_type == 'float':
            if isinstance(value, (int, float)):
                body = formatLang(
                    self.env, abs(value), digits=4, grouping=True,
                )
                return ("(%s)" % body) if value < 0 else body
            return str(value)
        if figure_type == 'percentage':
            if isinstance(value, (int, float)):
                body = formatLang(
                    self.env, abs(value) * 100.0,
                    digits=2, grouping=True,
                ) + '%'
                return ("(%s)" % body) if value < 0 else body
            return str(value)
        return str(value)

    @api.model
    def _line_css_class(self, line):
        """Return the space separated CSS class string for a row, based on
        line level and meta.kind. The Qweb template uses these classes to
        style headers, totals, computed lines, and balance checks.
        """
        classes = []
        level = int(line.get('level') or 0)
        if level == 0:
            classes.append('eh_pdf_section_row')
        else:
            classes.append('eh_pdf_data_row')
        kind = (line.get('meta') or {}).get('kind')
        if kind == 'section_header':
            classes.append('eh_pdf_section_header')
        elif kind == 'section_total':
            classes.append('eh_pdf_section_total')
        elif kind == 'account_header':
            classes.append('eh_pdf_account_header')
        elif kind == 'account_total':
            classes.append('eh_pdf_account_total')
        elif kind == 'partner_header':
            classes.append('eh_pdf_partner_header')
        elif kind == 'partner_total':
            classes.append('eh_pdf_partner_total')
        elif kind in ('net_profit', 'net_change',
                      'computed_total', 'current_year_earnings'):
            classes.append('eh_pdf_computed')
        elif kind == 'balance_check':
            classes.append('eh_pdf_balance_check')
        elif kind == 'opening_balance':
            classes.append('eh_pdf_opening')
        elif kind == 'cash_balance':
            classes.append('eh_pdf_cash_balance')
        return ' '.join(classes)


class EhAccountDynamicReportPdf(models.Model):
    _inherit = 'eh.account.dynamic.report'

    @api.private
    def _eh_render_pdf_ephemeral(self, options):
        """Render audited PDF bytes without persisting a cache payload.

        This is intentionally private and mints the process-local capability
        itself, so a JSON-RPC caller cannot turn ordinary back-office exports
        into unaudited or unexpectedly non-reproducible renders.
        """
        return self.with_context(**{
            _EH_EPHEMERAL_PDF_CONTEXT: _EH_EPHEMERAL_PDF_CAPABILITY,
        }).render_pdf(options, use_cache=False)

    def render_pdf(self, options, use_cache=True):
        """Render the report to PDF bytes.

        Resolves the universal PDF action, delegates to Odoo's standard
        Qweb to PDF pipeline, returning the bytes blob.
        """
        self.ensure_one()
        action = self.env.ref(
            'eh_account_dynamic_reports.action_report_dynamic_pdf',
            raise_if_not_found=False,
        )
        if not action:
            raise UserError(
                "PDF report action not found. Reinstall eh_account_dynamic_reports."
            )
        # As with XLSX, export the eager payload. The private render lifecycle
        # computes or loads it once; QWeb receives that exact payload below and
        # must not call render() a second time.
        options = dict(options or {}, eager_expand=True)
        options.pop('lazy_expand', None)
        report_ref = action.report_name

        def build_pdf(payload):
            data = {
                'options': options,
                '_eh_precomputed_payload': payload,
                '_eh_precomputed_report_id': self.id,
                '_eh_precomputed_capability': (
                    _EH_PRECOMPUTED_PDF_CAPABILITY
                ),
            }
            # Odoo's report rendering expects a list of docids; the abstract
            # model consumes the process-local payload above.
            rendering = self.env['ir.actions.report']._render_qweb_pdf(
                report_ref, [self.id], data=data,
            )
            # 17+ returns (bytes, content_type); legacy versions may return the
            # bytes directly. Test mode may return HTML instead of invoking
            # wkhtmltopdf, but the hash still covers the exact returned bytes.
            return rendering[0] if isinstance(rendering, tuple) else rendering

        ephemeral = (
            self.env.context.get(_EH_EPHEMERAL_PDF_CONTEXT)
            is _EH_EPHEMERAL_PDF_CAPABILITY
        )
        return self._eh_render_result(
            options,
            result_format='pdf',
            use_cache=use_cache and not ephemeral,
            result_builder=build_pdf,
            persist_payload=not ephemeral,
        )

    def export_pdf_attachment(self, options):
        """Render to PDF, persist as ir.attachment, return a download action.

        Mirrors export_xlsx_attachment so the OWL viewer can hand the user
        a downloadable file with one RPC call.
        """
        self.ensure_one()
        self._eh_check_access('read')
        content = self.render_pdf(options)
        date_block = options.get('date') or {}
        filename = "%s_%s_to_%s.pdf" % (
            self.code,
            date_block.get('date_from') or '',
            date_block.get('date_to') or '',
        )
        return self._eh_private_download_action(
            content=content,
            filename=filename,
            mimetype='application/pdf',
            options=options,
        )
