# -*- encoding: utf-8 -*-
"""Upgrade regressions for the 19.0.1.1.9 builder data repair."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from odoo.tests import TransactionCase, tagged


class _SchemaCursor:
    """Minimal cursor proving optional-schema branches without PostgreSQL."""

    def __init__(self, columns=None):
        self.columns = columns or {}
        self.queries = []
        self._rows = []
        self.rowcount = 0

    def execute(self, query, params=None):
        text = str(query)
        self.queries.append((text, params))
        self.rowcount = 0
        if 'information_schema.columns' in text:
            if params:
                table = params[0]
            elif 'eh_report_forecast' in text:
                table = 'eh_report_forecast'
            else:
                table = ''
            self._rows = [
                (column,) for column in sorted(self.columns.get(table, ()))
            ]
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)


@tagged(
    'eh_account_dynamic_reports_pro', 'integration',
    'post_install', '-at_install',
)
class TestBuilderMigration119(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        migration_root = (
            Path(__file__).resolve().parents[1]
            / 'migrations' / '19.0.1.1.9'
        )
        pre_spec = spec_from_file_location(
            'eh_builder_pre_migration_119',
            migration_root / 'pre-migration.py',
        )
        cls.pre_migration = module_from_spec(pre_spec)
        pre_spec.loader.exec_module(cls.pre_migration)
        post_spec = spec_from_file_location(
            'eh_builder_post_migration_119',
            migration_root / 'post-migration.py',
        )
        cls.post_migration = module_from_spec(post_spec)
        post_spec.loader.exec_module(cls.post_migration)
        migration_117 = (
            Path(__file__).resolve().parents[1]
            / 'migrations' / '19.0.1.1.7' / 'pre-migration.py'
        )
        spec_117 = spec_from_file_location(
            'eh_pro_pre_migration_117', migration_117,
        )
        cls.pre_migration_117 = module_from_spec(spec_117)
        spec_117.loader.exec_module(cls.pre_migration_117)
        migration_1112 = (
            Path(__file__).resolve().parents[1]
            / 'migrations' / '19.0.1.1.12' / 'pre-migration.py'
        )
        spec_1112 = spec_from_file_location(
            'eh_pro_pre_migration_1112', migration_1112,
        )
        cls.pre_migration_1112 = module_from_spec(spec_1112)
        spec_1112.loader.exec_module(cls.pre_migration_1112)

    def _builder(self, code, company=None):
        return self.env['eh.report.builder'].sudo().create({
            'name': code.replace('_', ' ').title(),
            'code': code,
            'company_id': (company or self.env.company).id,
        })

    def _migration_report(self, code, handler=None, company=False):
        return self.env['eh.account.dynamic.report'].sudo().create({
            'name': code.replace('_', ' ').title(),
            'code': code,
            'handler_model': (
                handler
                or 'eh.account.dynamic.report.handler.builder'
            ),
            'company_id': company.id if company else False,
        })

    def test_pre_repairs_nulls_preserves_values_and_is_idempotent(self):
        creator_company = self.env['res.company'].sudo().create({
            'name': 'Builder Migration Creator Company',
        })
        creator = self.env['res.users'].sudo().create({
            'name': 'Builder Migration Creator',
            'login': 'builder_migration_creator_119',
            'company_id': creator_company.id,
            'company_ids': [(6, 0, [creator_company.id])],
        })
        by_creator = self._builder('migration_creator_company_119')
        by_fallback = self._builder('migration_fallback_company_119')
        preserved = self._builder('migration_preserved_company_119')
        legacy_line = self.env['eh.report.builder.line'].sudo().create({
            'name': 'Legacy defaults',
            'builder_id': by_creator.id,
        })
        preserved_line = self.env['eh.report.builder.line'].sudo().create({
            'name': 'Preserved choices',
            'builder_id': preserved.id,
            'account_scope': 'types',
            'sign': '-',
        })
        self.env.flush_all()
        self.env.cr.execute(
            "ALTER TABLE eh_report_builder "
            "ALTER COLUMN company_id DROP NOT NULL"
        )
        self.env.cr.execute(
            "ALTER TABLE eh_report_builder_line "
            "ALTER COLUMN account_scope DROP NOT NULL"
        )
        self.env.cr.execute(
            "ALTER TABLE eh_report_builder_line "
            "ALTER COLUMN sign DROP NOT NULL"
        )
        self.env.cr.execute(
            "UPDATE eh_report_builder SET company_id = NULL, "
            "create_uid = CASE WHEN id = %s THEN %s ELSE NULL END "
            "WHERE id = ANY(%s)",
            (by_creator.id, creator.id, [by_creator.id, by_fallback.id]),
        )
        self.env.cr.execute(
            "UPDATE eh_report_builder_line "
            "SET account_scope = NULL, sign = NULL WHERE id = %s",
            (legacy_line.id,),
        )
        self.env.cr.execute("SELECT MIN(id) FROM res_company")
        fallback_company_id = self.env.cr.fetchone()[0]

        self.pre_migration.migrate(self.env.cr, '19.0.1.1.8')
        self.env.cr.execute(
            "SELECT id, company_id FROM eh_report_builder "
            "WHERE id = ANY(%s) ORDER BY id",
            ((by_creator | by_fallback | preserved).ids,),
        )
        first_builders = self.env.cr.fetchall()
        self.env.cr.execute(
            "SELECT id, account_scope, sign FROM eh_report_builder_line "
            "WHERE id = ANY(%s) ORDER BY id",
            ((legacy_line | preserved_line).ids,),
        )
        first_lines = self.env.cr.fetchall()

        self.pre_migration.migrate(self.env.cr, '19.0.1.1.8')
        self.env.cr.execute(
            "SELECT id, company_id FROM eh_report_builder "
            "WHERE id = ANY(%s) ORDER BY id",
            ((by_creator | by_fallback | preserved).ids,),
        )
        self.assertEqual(self.env.cr.fetchall(), first_builders)
        self.env.cr.execute(
            "SELECT id, account_scope, sign FROM eh_report_builder_line "
            "WHERE id = ANY(%s) ORDER BY id",
            ((legacy_line | preserved_line).ids,),
        )
        self.assertEqual(self.env.cr.fetchall(), first_lines)

        companies = dict(first_builders)
        self.assertEqual(companies[by_creator.id], creator_company.id)
        self.assertEqual(companies[by_fallback.id], fallback_company_id)
        self.assertEqual(companies[preserved.id], self.env.company.id)
        lines = {row[0]: row[1:] for row in first_lines}
        self.assertEqual(lines[legacy_line.id], ('codes', '+'))
        self.assertEqual(lines[preserved_line.id], ('types', '-'))

    def test_post_links_scopes_orphans_and_is_idempotent(self):
        owner = self.env['res.company'].sudo().create({
            'name': 'Builder Migration Owner',
        })
        wrong_company = self.env['res.company'].sudo().create({
            'name': 'Builder Migration Wrong Company',
        })
        unlinked = self._builder('migration_unlinked_report_119', owner)
        unlinked_report = self._migration_report(unlinked.code)
        already_linked = self._builder(
            'migration_existing_link_119', owner,
        )
        already_linked.action_publish()
        linked_report = already_linked.published_report_id
        orphan = self._migration_report('migration_orphan_report_119')
        standard = self._migration_report(
            'migration_standard_report_119',
            handler='eh.account.dynamic.report.handler.trial_balance',
        )
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_report_builder SET is_published = TRUE, "
            "published_report_id = NULL WHERE id = %s",
            (unlinked.id,),
        )
        self.env.cr.execute(
            "UPDATE eh_account_dynamic_report SET company_id = %s "
            "WHERE id = %s",
            (wrong_company.id, linked_report.id),
        )

        self.post_migration.migrate(self.env.cr, '19.0.1.1.8')
        self.env.cr.execute(
            "SELECT published_report_id FROM eh_report_builder WHERE id = %s",
            (unlinked.id,),
        )
        self.assertEqual(self.env.cr.fetchone(), (unlinked_report.id,))
        self.env.cr.execute(
            "SELECT id, company_id, active "
            "FROM eh_account_dynamic_report "
            "WHERE id = ANY(%s) ORDER BY id",
            ((unlinked_report | linked_report | orphan | standard).ids,),
        )
        first = self.env.cr.fetchall()

        self.post_migration.migrate(self.env.cr, '19.0.1.1.8')
        self.env.cr.execute(
            "SELECT id, company_id, active "
            "FROM eh_account_dynamic_report "
            "WHERE id = ANY(%s) ORDER BY id",
            ((unlinked_report | linked_report | orphan | standard).ids,),
        )
        self.assertEqual(self.env.cr.fetchall(), first)
        reports = {row[0]: row[1:] for row in first}
        self.assertEqual(reports[unlinked_report.id][0], owner.id)
        self.assertEqual(reports[linked_report.id][0], owner.id)
        self.assertFalse(reports[orphan.id][1])
        self.assertTrue(reports[standard.id][1])

    def test_optional_schema_guards_do_not_gate_available_repairs(self):
        empty = _SchemaCursor()
        self.pre_migration.migrate(empty, '19.0.1.1.8')
        self.post_migration.migrate(empty, '19.0.1.1.8')
        self.assertTrue(empty.queries)
        self.assertTrue(all(
            'information_schema.columns' in query
            for query, _params in empty.queries
        ))

        # No legacy saved-view table is advertised. Builder repair must still
        # reach all three graph operations.
        baseline = _SchemaCursor({
            'eh_report_builder': {
                'id', 'code', 'company_id', 'published_report_id',
            },
            'eh_account_dynamic_report': {
                'id', 'code', 'handler_model', 'company_id', 'active',
            },
        })
        self.post_migration.migrate(baseline, '19.0.1.1.8')
        dml = [
            query for query, _params in baseline.queries
            if 'information_schema.columns' not in query
        ]
        self.assertEqual(len(dml), 3)
        self.assertTrue(any('published_report_id =' in query for query in dml))
        self.assertTrue(any('SET company_id =' in query for query in dml))
        self.assertTrue(any('SET active = FALSE' in query for query in dml))

        # A partial line schema is still independently normalised.
        partial = _SchemaCursor({
            'eh_report_builder_line': {'sign'},
        })
        self.pre_migration.migrate(partial, '19.0.1.1.8')
        updates = [
            query for query, _params in partial.queries
            if query.startswith('UPDATE eh_report_builder_line')
        ]
        self.assertEqual(len(updates), 1)
        self.assertIn("sign = COALESCE(sign, '+')", updates[0])
        self.assertNotIn('account_scope', updates[0])

    def test_117_migration_guards_missing_tables_and_columns(self):
        empty = _SchemaCursor()
        self.pre_migration_117.migrate(empty, '19.0.1.1.6')
        dml = [
            query for query, _params in empty.queries
            if 'information_schema.columns' not in query
        ]
        self.assertFalse(dml)

        timeout_only = _SchemaCursor({
            'eh_report_schedule': {'webhook_timeout'},
        })
        self.pre_migration_117.migrate(timeout_only, '19.0.1.1.6')
        dml = [
            query for query, _params in timeout_only.queries
            if query.startswith('UPDATE eh_report_schedule')
        ]
        self.assertEqual(len(dml), 2)
        self.assertNotIn('delivery_channel', dml[1])

    def test_117_migration_preserves_email_only_null_timeout(self):
        report = self.env.ref(
            'eh_account_dynamic_reports.report_trial_balance',
        )
        Schedule = self.env['eh.report.schedule'].sudo()
        base_vals = {
            'report_id': report.id,
            'options_json': '{}',
            'interval': 1,
            'interval_unit': 'month',
            'next_run': '2026-08-31 00:00:00',
            'subject': 'Migration timeout test',
        }
        email_null = Schedule.create(dict(
            base_vals, name='Email null', delivery_channel='email',
        ))
        email_invalid = Schedule.create(dict(
            base_vals, name='Email invalid', delivery_channel='email',
        ))
        webhook_invalid = Schedule.create(dict(
            base_vals, name='Webhook invalid', delivery_channel='webhook',
        ))
        self.env.flush_all()
        self.env.cr.execute(
            "ALTER TABLE eh_report_schedule DROP CONSTRAINT IF EXISTS "
            "eh_report_schedule_bounded_webhook_timeout"
        )
        self.env.cr.execute(
            "ALTER TABLE eh_report_schedule "
            "ALTER COLUMN webhook_timeout DROP NOT NULL"
        )
        self.env.cr.execute(
            "UPDATE eh_report_schedule SET webhook_timeout = CASE "
            "WHEN id = %s THEN NULL ELSE 99 END WHERE id = ANY(%s)",
            [email_null.id, (
                email_null | email_invalid | webhook_invalid
            ).ids],
        )

        self.pre_migration_117.migrate(self.env.cr, '19.0.1.1.6')
        schedules = email_null | email_invalid | webhook_invalid
        schedules.invalidate_recordset([
            'webhook_timeout', 'active', 'last_run_status', 'last_error',
        ])
        self.assertEqual(email_null.webhook_timeout, 15)
        self.assertTrue(email_null.active)
        self.assertEqual(email_invalid.webhook_timeout, 15)
        self.assertTrue(email_invalid.active)
        self.assertEqual(webhook_invalid.webhook_timeout, 15)
        self.assertFalse(webhook_invalid.active)
        self.assertEqual(webhook_invalid.last_run_status, 'error')

        self.env.cr.execute(
            "ALTER TABLE eh_report_schedule "
            "ALTER COLUMN webhook_timeout SET NOT NULL"
        )
        self.env.cr.execute(
            "ALTER TABLE eh_report_schedule ADD CONSTRAINT "
            "eh_report_schedule_bounded_webhook_timeout "
            "CHECK (webhook_timeout BETWEEN 1 AND 60)"
        )

    def test_1112_migration_renames_legacy_linear_growth(self):
        report = self.env.ref(
            'eh_account_dynamic_reports.report_profit_and_loss',
        )
        forecast = self.env['eh.report.forecast'].create({
            'name': 'Legacy linear label',
            'base_report_id': report.id,
            'base_date_from': '2026-01-01',
            'base_date_to': '2026-01-31',
            'horizon_months': 1,
            'growth_method': 'compound',
        })
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_report_forecast SET growth_method = 'linear' "
            "WHERE id = %s",
            [forecast.id],
        )
        self.pre_migration_1112.migrate(self.env.cr, '19.0.1.1.11')
        forecast.invalidate_recordset(['growth_method'])
        self.assertEqual(forecast.growth_method, 'compound')

    def test_pre_normalizes_legacy_forecast_growth_before_check(self):
        report = self.env.ref(
            'eh_account_dynamic_reports.report_profit_and_loss',
        )
        forecasts = self.env['eh.report.forecast'].sudo().create([
            {
                'name': 'Legacy growth high',
                'base_report_id': report.id,
                'base_date_from': '2026-01-01',
                'base_date_to': '2026-01-31',
                'horizon_months': 1,
                'monthly_growth_pct': 5.0,
            },
            {
                'name': 'Legacy growth NaN',
                'base_report_id': report.id,
                'base_date_from': '2026-01-01',
                'base_date_to': '2026-01-31',
                'horizon_months': 1,
                'monthly_growth_pct': 5.0,
            },
            {
                'name': 'Legacy growth negative infinity',
                'base_report_id': report.id,
                'base_date_from': '2026-01-01',
                'base_date_to': '2026-01-31',
                'horizon_months': 1,
                'monthly_growth_pct': 5.0,
            },
        ])
        self.env.flush_all()
        self.env.cr.execute(
            "ALTER TABLE eh_report_forecast DROP CONSTRAINT IF EXISTS "
            "eh_report_forecast_bounded_monthly_growth"
        )
        self.env.cr.execute(
            "UPDATE eh_report_forecast SET monthly_growth_pct = CASE id "
            "WHEN %s THEN 1001.0 "
            "WHEN %s THEN 'NaN'::double precision "
            "WHEN %s THEN '-Infinity'::double precision END "
            "WHERE id = ANY(%s)",
            (*forecasts.ids, forecasts.ids),
        )

        self.pre_migration.migrate(self.env.cr, '19.0.1.1.8')
        self.env.cr.execute(
            "SELECT monthly_growth_pct FROM eh_report_forecast "
            "WHERE id = ANY(%s) ORDER BY id",
            (forecasts.ids,),
        )
        first = [row[0] for row in self.env.cr.fetchall()]
        self.pre_migration.migrate(self.env.cr, '19.0.1.1.8')
        self.env.cr.execute(
            "SELECT monthly_growth_pct FROM eh_report_forecast "
            "WHERE id = ANY(%s) ORDER BY id",
            (forecasts.ids,),
        )
        self.assertEqual([row[0] for row in self.env.cr.fetchall()], first)
        self.assertEqual(first, [1000.0, 0.0, -100.0])
        self.env.cr.execute(
            "ALTER TABLE eh_report_forecast ADD CONSTRAINT "
            "eh_report_forecast_bounded_monthly_growth "
            "CHECK (monthly_growth_pct BETWEEN -100.0 AND 1000.0)"
        )
