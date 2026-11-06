"""Executable aggregate contract for the accounting-suite meta module."""

import ast
import importlib.util
from pathlib import Path

from odoo.tests import TransactionCase, tagged


@tagged('eh_account_suite', 'post_install', '-at_install')
class TestSuiteContract(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.module_root = Path(__file__).resolve().parents[1]
        cls.repo_root = cls.module_root.parent
        cls.manifest = ast.literal_eval(
            (cls.module_root / '__manifest__.py').read_text(encoding='utf-8')
        )
        cls.readme = (cls.module_root / 'README.md').read_text(
            encoding='utf-8'
        )
        cls.listing = (
            cls.module_root / 'static' / 'description' / 'index.html'
        ).read_text(encoding='utf-8')

    def test_exact_51_module_closure_is_installed_together(self):
        dependencies = self.manifest['depends']
        expected = (
            self.module_root / 'tests' / 'expected_suite_modules.txt'
        ).read_text(encoding='utf-8').splitlines()
        self.assertEqual(dependencies, expected)
        self.assertEqual(len(dependencies), 51)
        installed = set(self.env['ir.module.module'].sudo().search([
            ('name', 'in', dependencies),
            ('state', '=', 'installed'),
        ]).mapped('name'))
        self.assertEqual(installed, set(dependencies))
        core_apps = self.env['ir.module.module'].sudo().search([
            ('name', 'in', ['sale', 'purchase', 'stock']),
            ('state', '=', 'installed'),
        ]).mapped('name')
        self.assertEqual(set(core_apps), {'sale', 'purchase', 'stock'})

    def test_bundle_version_is_not_below_any_member(self):
        suite_version = tuple(map(int, self.manifest['version'].split('.')))
        member_versions = {}
        for module_name in self.manifest['depends']:
            member_manifest = ast.literal_eval(
                (self.repo_root / module_name / '__manifest__.py').read_text(
                    encoding='utf-8'
                )
            )
            member_versions[module_name] = tuple(
                map(int, member_manifest['version'].split('.'))
            )
        highest_name = max(member_versions, key=member_versions.get)
        self.assertGreaterEqual(
            suite_version, member_versions[highest_name],
            msg='suite version is below %s' % highest_name,
        )

    def test_manifest_readme_and_listing_are_truthful(self):
        series = self.manifest['version'].split('.', 1)[0]
        expected_name = 'Odoo %s Community Accounting Pro' % series
        self.assertEqual(self.manifest['name'], expected_name)
        self.assertEqual(len(self.manifest['name']), 32)
        self.assertLessEqual(len(self.manifest['summary']), 160)
        self.assertEqual(self.listing.count(self.manifest['version']), 2)
        self.assertIn('<title>%s | ERP Heritage</title>' % expected_name,
                      self.listing)
        self.assertIn('>%s<' % expected_name, self.listing)
        self.assertTrue(self.readme.startswith('# %s\n' % expected_name))
        self.assertNotIn('Auto-installs', self.listing)
        self.assertIn('51 direct accounting modules', self.readme)
        for dependency in self.manifest['depends']:
            self.assertIn('`%s`' % dependency, self.readme)
        for corpus in (
            self.manifest['description'], self.readme, self.listing,
        ):
            self.assertIn('QIF', corpus)
            self.assertIn('PAIN.001.001.03', corpus)
            self.assertIn('.09', corpus)
        from odoo.addons.eh_account_bank_statement_import.parsers.registry \
            import PARSERS
        self.assertEqual(
            set(PARSERS), {'csv', 'ofx', 'qif', 'camt053', 'mt940'}
        )
        pain_selection = self.env['eh.sepa.originator']._fields[
            'pain_001_version'
        ].selection
        self.assertEqual({key for key, _label in pain_selection}, {'03', '09'})
        self.assertIn('hard dependency', self.readme)
        self.assertIn('Sales, Purchase and Inventory', self.readme)
        self.assertIn('one idempotent ERP', self.readme)
        self.assertIn('one idempotent ERP Heritage support contact', self.listing)
        closure_python_dependencies = set()
        pending = list(self.manifest['depends'])
        seen = set()
        while pending:
            module_name = pending.pop()
            if module_name in seen:
                continue
            seen.add(module_name)
            manifest_path = self.repo_root / module_name / '__manifest__.py'
            if not manifest_path.exists():
                continue
            dependency_manifest = ast.literal_eval(
                manifest_path.read_text(encoding='utf-8')
            )
            closure_python_dependencies.update(
                dependency_manifest.get('external_dependencies', {}).get(
                    'python', []
                )
            )
            pending.extend(dependency_manifest.get('depends', []))
        self.assertIn('cryptography', closure_python_dependencies)
        self.assertEqual(
            closure_python_dependencies,
            set(self.manifest['external_dependencies']['python']),
        )
        footer = self.listing.split('<footer', 1)[1]
        self.assertNotIn('<a ', footer)
        self.assertNotIn('Melbourne', footer)
        self.assertNotIn('LinkedIn', footer)
        self.assertFalse(
            (self.module_root / '__manifest__.py').read_text(
                encoding='utf-8'
            ).startswith('# -*- encoding:'),
        )

    def test_country_aware_bas_onboarding(self):
        task = self.env.ref('eh_account_suite.task_au_bas_onboarding')
        lines = self.env['eh.account.setup.task.line'].sudo().search([
            ('task_id', '=', task.id),
        ])
        self.assertEqual(
            set(lines.mapped('company_id').ids),
            set(self.env['res.company'].sudo().with_context(
                active_test=False
            ).search([]).ids),
        )
        australia = self.env.ref('base.au')
        for line in lines:
            company = line.company_id.with_company(line.company_id)
            fiscal_field = company._fields.get(
                'account_fiscal_country_id'
            )
            fiscal_country = (
                company.account_fiscal_country_id
                if fiscal_field
                and company.account_fiscal_country_id
                else company.country_id
            )
            if not fiscal_country or fiscal_country == australia:
                self.assertEqual(line.state, 'todo')
            else:
                self.assertEqual(line.state, 'skipped')
                self.assertIn('outside Australia', line.notes)

    def test_later_company_country_changes_and_progress_are_safe(self):
        Company = self.env['res.company'].sudo()
        australia = self.env.ref('base.au')
        new_zealand = self.env.ref('base.nz')
        au_company = Company.create({
            'name': 'EH Suite Contract AU',
            'country_id': australia.id,
            'parent_id': False,
        })
        nz_company = Company.create({
            'name': 'EH Suite Contract NZ',
            'country_id': new_zealand.id,
            'parent_id': False,
        })
        unknown_company = Company.create({
            'name': 'EH Suite Contract Unknown',
            'country_id': False,
            'parent_id': False,
        })
        archived_company = Company.create({
            'name': 'EH Suite Contract Archived NZ',
            'country_id': new_zealand.id,
            'parent_id': False,
        })
        archived_company.write({'active': False})
        annotated_company = Company.create({
            'name': 'EH Suite Contract Annotated',
            'country_id': False,
            'parent_id': False,
        })
        task = self.env.ref('eh_account_suite.task_au_bas_onboarding')
        Line = self.env['eh.account.setup.task.line'].sudo()

        def _line(company):
            return Line.search([
                ('task_id', '=', task.id),
                ('company_id', '=', company.id),
            ])

        self.assertEqual(_line(au_company).state, 'todo')
        self.assertEqual(_line(nz_company).state, 'skipped')
        self.assertEqual(_line(unknown_company).state, 'todo')
        self.assertEqual(_line(archived_company).state, 'skipped')
        self.assertFalse(_line(archived_company).completed_by_id)
        archived_company.write({'active': True})
        self.assertEqual(_line(archived_company).state, 'skipped')

        annotated_line = _line(annotated_company)
        annotated_line.write({'notes': 'Draft registration evidence.'})
        annotated_company.write({
            'country_id': new_zealand.id,
            'account_fiscal_country_id': new_zealand.id,
        })
        self.assertEqual(annotated_line.state, 'todo')
        self.assertEqual(annotated_line.notes, 'Draft registration evidence.')

        nz_line = _line(nz_company)
        nz_line.write({
            'state': 'done',
            'notes': 'Operator verified an applicable registration.',
        })
        nz_company.write({
            'country_id': australia.id,
            'account_fiscal_country_id': australia.id,
        })
        self.assertEqual(nz_line.state, 'done')
        self.assertEqual(
            nz_line.notes,
            'Operator verified an applicable registration.',
        )

        au_line = _line(au_company)
        au_line.write({
            'state': 'skipped',
            'completed_by_id': self.env.user.id,
            'notes': _line(archived_company).notes,
        })
        au_company.write({
            'country_id': new_zealand.id,
            'account_fiscal_country_id': new_zealand.id,
        })
        self.assertEqual(au_line.state, 'skipped')
        self.assertEqual(
            au_line.notes, _line(archived_company).notes
        )

        unknown_company.write({
            'country_id': new_zealand.id,
            'account_fiscal_country_id': new_zealand.id,
        })
        self.assertEqual(_line(unknown_company).state, 'skipped')
        unknown_company.write({
            'country_id': australia.id,
            'account_fiscal_country_id': australia.id,
        })
        self.assertEqual(_line(unknown_company).state, 'todo')

    def test_upgrade_entrypoint_applies_onboarding_finalizer(self):
        new_zealand = self.env.ref('base.nz')
        company = self.env['res.company'].sudo().create({
            'name': 'EH Suite Upgrade NZ',
            'country_id': new_zealand.id,
            'parent_id': False,
        })
        company.write({'active': False})
        task = self.env.ref('eh_account_suite.task_au_bas_onboarding')
        line = self.env['eh.account.setup.task.line'].sudo().search([
            ('task_id', '=', task.id),
            ('company_id', '=', company.id),
        ])
        line.unlink()
        migration_path = (
            self.module_root / 'migrations' / self.manifest['version']
            / 'post-migration.py'
        )
        spec = importlib.util.spec_from_file_location(
            'eh_account_suite_upgrade_contract', migration_path
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, 'prior-version')
        self.env.invalidate_all()
        migrated_line = self.env['eh.account.setup.task.line'].sudo().search([
            ('task_id', '=', task.id),
            ('company_id', '=', company.id),
        ])
        self.assertEqual(migrated_line.state, 'skipped')

    def test_generated_banner_contract_matches_listing(self):
        from PIL import Image
        for filename in ('banner.png', 'banner.gif'):
            with Image.open(
                self.module_root / 'static' / 'description' / filename
            ) as image:
                metadata = image.info.get('Title') or image.info.get(
                    'comment', b''
                )
                if isinstance(metadata, bytes):
                    metadata = metadata.decode('utf-8')
                self.assertIn('Community Accounting Pro', metadata)
                self.assertIn('51 modules', metadata)
                self.assertNotIn('ODOO 19', metadata.upper())
