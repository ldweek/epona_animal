# Phase 1 test plan:
#
# Functional unit tests:
from . import test_cheque_book
from . import test_cheque_lifecycle
from . import test_bounce_replace
from . import test_cheque_print
from . import test_cheque_register_report
from . import test_cheque_invoice_reconcile
from . import test_fiscal_lock
from . import test_settlement_accounting
from . import test_partner_statistics
from . import test_workflow_guard
#
# Golden IFRS 9 worked examples (IFRS 10/10 program, Phase 1):
from . import test_golden_ifrs9
