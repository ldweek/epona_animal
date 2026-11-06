# -*- encoding: utf-8 -*-
from . import test_fx_revaluation
from . import test_fx_report
from . import test_performance
from . import test_medium_low_metadata
from . import test_rate_providers
from . import test_hedge
from . import test_golden_ias21_cta
from . import test_workflow_guard
from . import test_post_lock
from . import test_line_freeze
from . import test_migration_163
from . import test_migration_165
from . import test_migration_166
from . import test_migration_169
from . import test_migration_170

# Per-source rate provider tests (hermetic, no network, no DB writes).
from . import test_fx_frankfurter
from . import test_fx_erapi
from . import test_fx_boc
from . import test_fx_nbp
from . import test_fx_cnb
from . import test_fx_cbr
from . import test_fx_bnr
from . import test_fx_bnb
from . import test_fx_srb
from . import test_fx_bcu
from . import test_fx_nbkz
from . import test_fx_cbu
from . import test_fx_tcmb
from . import test_fx_rba
from . import test_fx_cbk
from . import test_fx_cbb
from . import test_fx_bcb
from . import test_fx_bnm
from . import test_fx_banrepco
from . import test_fx_bcrp
from . import test_fx_hmrc
from . import test_fx_gcc_peg
from . import test_fx_fixer
from . import test_fx_oxr
from . import test_fx_currencylayer
from . import test_fx_banxico

# Browser tour (eh_tour tag, selected by the matrix runner with --tours).
from . import test_tour
