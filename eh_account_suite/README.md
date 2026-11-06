# Odoo 19 Community Accounting Pro

One-click install for 51 direct accounting modules on Odoo Community.
This aggregate contains no accounting engine or transaction model; its only
model extension keeps country-aware onboarding current over company lifecycle.
It pins, installs, and tests one explicit dependency closure.

## Included closure

| Capability | Modules |
|---|---|
| Foundation | `eh_account_base`, `eh_account_setup` |
| Reporting | `eh_account_dynamic_reports`, `eh_account_dynamic_reports_pro`, `eh_account_dashboard`, `eh_account_dynamic_reports_budget` |
| Receivables | `eh_account_reconcile_pro`, `eh_account_bank_statement_import`, `eh_account_batch_payment`, `eh_account_collections`, `eh_account_credit_limit`, `eh_account_recurring_invoices`, `eh_account_pdc`, `eh_account_portal_extra` |
| Payables | `eh_account_ap_automation`, `eh_account_approval` |
| Assets and leases | `eh_account_assets_pro` |
| Period close | `eh_account_close_workflow`, `eh_account_year_end`, `eh_account_fx_revaluation` |
| Group accounting | `eh_account_intercompany`, `eh_account_consolidation`, `eh_account_intercompany_so_po` |
| Budgeting | `eh_account_budget_pro` |
| Compliance and payments | `eh_account_einvoice_peppol`, `eh_account_l10n_au_bas`, `eh_account_sepa_ct`, `eh_account_sepa_dd` |
| AI augmentation | `eh_account_ai_agent`, `eh_account_ai_budget`, `eh_account_ai_collections` |
| IFRS 9, IAS 12 and IAS 37 | `eh_account_ecl`, `eh_account_fair_value`, `eh_account_deferred_tax`, `eh_account_provisions` |
| Revenue and recurring bridge | `eh_account_revenue`, `eh_account_revenue_recurring` |
| Specialist standards | `eh_account_borrowing_costs`, `eh_account_grants`, `eh_account_inventory_nrv`, `eh_account_investment_property`, `eh_account_held_for_sale`, `eh_account_business_combination`, `eh_account_events`, `eh_account_eps`, `eh_account_share_based_payment`, `eh_account_employee_benefits`, `eh_account_costing` |
| Presentation, disclosure and assurance | `eh_account_statements`, `eh_account_disclosures`, `eh_account_audit_pack` |

`eh_account_l10n_au_bas` is a hard dependency of this fixed bundle. Suite
onboarding creates its setup task for every company, leaves it pending for
Australian fiscal countries, and pre-skips it for other countries. This does
not uninstall or silently disable module code.

Bank Statement Import ships five parsers: CSV, OFX, QIF, CAMT.053 and MT940.
SEPA Credit Transfer supports PAIN.001.001.03 and .09.

## Install effects

Order mirroring, AP matching, and asset flows transitively install Odoo Sales,
Purchase and Inventory. Transitive EH EDI Core encrypted credential-vault
runtime requires Python package `cryptography`; aggregate manifest declares
this dependency explicitly. EH EDI Core also creates one idempotent ERP
Heritage support contact; an archived contact stays archived and is never
duplicated.

## Tested-together contract

`tests/test_suite_contract.py` executes after install and verifies:

- all 51 direct dependencies are installed;
- transitive Sales, Purchase and Inventory applications are installed;
- bundle version is not below any member version;
- manifest, listing, README and parser/schema claims agree;
- country-aware Australian BAS onboarding was seeded correctly.

Full Odoo 16, 17, 18 and 19 release matrices run this aggregate test with all
suite modules installed together. Individual-module success is not used as a
substitute for aggregate closure.

## Licensing

LGPL-3 across every constituent module in this release.
