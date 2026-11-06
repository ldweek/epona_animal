##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': "Odoo 19 Community Accounting Pro",
    'summary': "One-click 51-module accounting, IFRS, reporting, close, payments, automation, and audit bundle for Odoo Community.",
    'description': """
Odoo 19 Community Accounting Pro
================================

The complete ERP Heritage accounting suite for Odoo 19 Community Edition,
delivered as a single installable. Installing this module pulls every
production-grade accounting capability ERP Heritage publishes onto the
Apps Store, in a coherent, version-aligned bundle.

What is in the bundle
---------------------

**Foundation**

* Accounting Suite Base. Reproducible report audit log, in-process result
  cache, parameterised SQL builder. Installed as an explicit suite dependency.
* Accounting Setup Guide. First-install onboarding checklist with deep
  links to each configuration screen, per-company state.

**Reporting**

* Dynamic Account Reports. P&L, Balance Sheet, Trial Balance, General
  Ledger, Aged Partner Balance, Cash Flow, Partner Ledger. Drill down to
  journal entries, comparatives, XLSX and PDF export.
* Dynamic Reports Pro. Drag-and-drop custom report builder, scheduled
  email delivery, multi-period forecasting, budget vs actual scenarios.
* Financial Dashboard. Per-company KPI dashboard, cash position, AR / AP
  aging, period P&L, approvals, collections, budget variance tiles.

**Receivables**

* Bank Reconciliation Pro. Modern reconciliation widget. Smart
  suggestions, drag-and-drop matching, bulk operations, statement line
  search.
* Bank Statement Import. CSV, OFX, QIF, CAMT.053, MT940 parsers. Pluggable
  per-bank profiles, idempotent reimport.
* Batch Payment. Group inbound or outbound payments into a single posted
  batch. Per-partner aggregation, CSV export, manager-only post.
* Collections Workbench. Kanban-driven overdue receivables management.
  Auto-created cases, follow-up letter engine, escalation ladder,
  promise-to-pay tracking, call activities.
* Customer Credit Limit. Enforced credit limits on AR with manager
  override. Per-company policy, per-partner override, immutable
  override audit log.
* Recurring Invoices Pro. Template-based, daily / weekly / monthly /
  quarterly / yearly cadence, pause and resume, optional auto-post.
* Post-Dated Cheques. Cheque registers, issued and received tracking,
  bank presentation, clearance and bounce workflow.
* Customer Portal Extra. One-click statement download and a clean
  open-invoices summary on the customer portal.

**Payables**

* Vendor Bill Automation. Bill intake inbox, regex extraction,
  three-way match against PO and GRN, configurable tolerances.
* Approval Workflow. Multi-step approval for vendor bills and journal
  entries. Configurable policies per amount band, ordered approver
  groups, atomic state machine.

**Asset and revenue accounting**

* Fixed Assets and Leases. Fixed asset register, multiple depreciation
  methods, revaluation, disposal. IFRS 16 lease accounting (ROU,
  liability, modification, termination). Deferred revenue / expense.

**IFRS / IAS standards engine**

* IFRS 9 Expected Credit Loss. Three-stage impairment, POCI, forward
  looking scenarios, discounting and a loss-allowance reconciliation.
* IFRS 13 Fair Value. Fair-value hierarchy with the IFRS 9 SPPI and
  business-model classification and OCI recycling on derecognition.
* IAS 12 Deferred Tax. Balance-sheet liability method, enacted rate
  table, offsetting and the effective-tax-rate reconciliation.
* IAS 37 Provisions. Discounting and unwind, onerous-contract and
  restructuring engines, contingent and reimbursement paths.
* IFRS 15 Revenue. Five-step model with progress methods, the variable
  consideration constraint, financing and contract modifications, and a
  recurring-invoice bridge.
* IAS 23 Borrowing Costs, IAS 2 Inventory NRV, IAS 20 Grants, IAS 40
  Investment Property, IFRS 5 Held for Sale disposal groups.
* IFRS 3 Business Combinations, IAS 33 Earnings per Share, IAS 10 Events
  after the reporting period.
* IFRS 2 Share-based Payment and IAS 19 Employee Benefits.
* Standard Costing and CVP. Cost cards, the full variance decomposition,
  contribution margin and break-even.

**IFRS presentation and disclosure**

* Primary Statements. Statement of comprehensive income and changes in
  equity with structural OCI recycling and the IAS 1.60 completeness
  guard.
* Disclosure Notes. IFRS 7 credit and market risk, IFRS 8 segments,
  IAS 24 related parties and IFRS 12 interests.

**Audit and assurance**

* Audit Pack. Period sign-off with general-ledger integrity, hash-chain
  and segregation-of-duties checks.

**Period close**

* Period Close Workflow. Checklist-driven monthly and yearly close with
  sign-off chain, reusable templates, per-run task instances, audit
  trail.
* Year-End Closing. Computes net profit, posts the closing entry,
  optionally locks the fiscal year. Reverse and recompute supported.
* FX Period-End Revaluation. IAS 21 revaluation of monetary balances.
  Run records, computed adjustment lines, single audited journal entry,
  optional automatic reversal.

**Group accounting**

* Inter-Company Rules. Auto-mirror invoices and bills across sister
  companies. Posted sale invoice in A creates draft purchase bill in B.
* Group Consolidation. Multi-entity group consolidation. Parent +
  subsidiaries, IC elimination, IAS 21 currency translation, NCI split.

**Budgeting**

* Multi-Version Budget Pro. Multi-version budgets, batch actual
  computation, variance analysis. Versioned records, lifecycle.

**Compliance and e-invoicing**

* Peppol eInvoicing. Peppol BIS Billing 3.0 outbound and inbound.
  UBL 2.1 generator and parser, pluggable access-point adapter.
* AU BAS Reporting. Full G/W/T/F/FTC/WET/LCT label set, GST control
  reconciliation, ABN validator, Simpler BAS plus cash / accruals toggle.
  This is a hard suite dependency; country-aware onboarding pre-skips its
  setup task for non-Australian companies without uninstalling the module.

**Payment file generation**

* SEPA Credit Transfer. ISO 20022 PAIN.001.001.03 and .09 XML for vendor
  payment batches. IBAN and BIC validated locally.
* SEPA Direct Debit. ISO 20022 PAIN.008.001.02 XML. Mandate lifecycle
  (FRST / RCUR / FNAL / OOFF), creditor identifier per company.

**Augmentation**

* AI Agent Layer. JE anomaly detection, variance commentary, collections
  next-action suggestion. Deterministic defaults plus optional LLM
  provider hook.

Why install the bundle
----------------------

* **One version stream.** Every constituent module is on the same Odoo
  19 release line. The bundle's manifest version anchors the
  combination tested as a set.
* **One support contact.** Every module ships under the same publisher
  (ERP Heritage). Support and updates come from one source.
* **One install.** Customers stop hunting through 51 separate Apps Store
  listings. One click brings the entire suite into a Community database.
* **Engineering bar held across the suite.** Test coverage, isolation
  rules, audit logs, multi-company scoping, IFRS / IAS adherence are
  consistent across every module.
* **Core applications disclosed.** The fixed bundle installs Odoo Sales,
  Purchase and Inventory through its order, AP and asset dependencies.
* **Python dependency disclosed.** The transitive EH EDI Core encrypted
  credential-vault runtime requires the ``cryptography`` Python package;
  the aggregate manifest declares it.
* **Seed side effect disclosed.** EH EDI Core creates one idempotent ERP
  Heritage support contact. An archived contact stays archived and is not
  recreated.

Engineering principles
----------------------

* Multi-company aware throughout. Every model declares company scoping;
  every isolation rule is global.
* No silent fallbacks. Missing rates, missing accounts, missing
  configuration each surface explicit messages.
* Performance-gated tests. Render-time thresholds and bounded query
  counts are asserted on report, asset, FX and AP flows, alongside
  combination tests across multi-company, multi-currency, fiscal
  positions and lock dates.

Search keywords
---------------

Accounting, full featured accounting, Comprehensive Accounting, Odoo 19 Community
accounting, accounting suite, accounting pack, accounting bundle,
financial reporting, period close, year-end close, FX revaluation,
fixed assets, IFRS 16, lease accounting, consolidation, intercompany,
collections, credit limit, recurring invoices, batch payments,
post-dated cheques, vendor bill automation, approval workflow,
Peppol eInvoicing, SEPA credit transfer, SEPA direct debit, BAS,
multi-version budget, dashboard, customer portal.

    """,
    'author': "ERP Heritage",
    'website': "https://www.erpheritage.com.au/",
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    # Bundle version anchors the suite as a tested-together set. Bump the
    # minor when the member set or its generation changes; bump the patch
    # for a re-test of the same set. Kept at or above the highest member
    # version so the aggregate never reads as older than its parts.
    'version': '19.0.2.1.3',
    'depends': [
        'eh_account_base',
        'eh_account_setup',
        'eh_account_dynamic_reports',
        'eh_account_dynamic_reports_pro',
        'eh_account_dashboard',
        'eh_account_reconcile_pro',
        'eh_account_bank_statement_import',
        'eh_account_batch_payment',
        'eh_account_collections',
        'eh_account_credit_limit',
        'eh_account_recurring_invoices',
        'eh_account_pdc',
        'eh_account_portal_extra',
        'eh_account_ap_automation',
        'eh_account_approval',
        'eh_account_assets_pro',
        'eh_account_close_workflow',
        'eh_account_year_end',
        'eh_account_fx_revaluation',
        'eh_account_intercompany',
        'eh_account_consolidation',
        'eh_account_budget_pro',
        'eh_account_einvoice_peppol',
        'eh_account_l10n_au_bas',
        'eh_account_sepa_ct',
        'eh_account_sepa_dd',
        'eh_account_ai_agent',
        'eh_account_ai_budget',
        'eh_account_ai_collections',
        # IFRS / IAS engine (the standards layer of the suite).
        'eh_account_ecl',
        'eh_account_fair_value',
        'eh_account_deferred_tax',
        'eh_account_provisions',
        'eh_account_revenue',
        'eh_account_revenue_recurring',
        'eh_account_borrowing_costs',
        'eh_account_grants',
        'eh_account_inventory_nrv',
        'eh_account_investment_property',
        'eh_account_held_for_sale',
        'eh_account_business_combination',
        'eh_account_intercompany_so_po',
        'eh_account_events',
        'eh_account_eps',
        'eh_account_share_based_payment',
        'eh_account_employee_benefits',
        'eh_account_costing',
        # IFRS presentation and disclosure.
        'eh_account_statements',
        'eh_account_disclosures',
        'eh_account_dynamic_reports_budget',
        # Audit and assurance.
        'eh_account_audit_pack',
    ],
    'data': ['data/setup_tasks.xml'],
    'external_dependencies': {'python': ['cryptography']},
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': True,
    'auto_install': False,
    'post_init_hook': 'post_init_hook',
}
