# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
HTTP foreign-exchange rate sources.

Each submodule defines one provider class (subclassing
``tools.rate_providers.BaseHttpProvider``) and registers it with the
central registry. Importing this package triggers every registration,
so a new source is purely a new file listed below; no view or model
edits are needed because the Odoo selection is registry-driven.

Central-bank sources need no API key and work out of the box. Keyed
aggregator sources read the key from the FX rate configuration.
"""

# Central-bank and free sources (no API key required).
from . import frankfurter   # noqa: F401
from . import boc           # noqa: F401  Bank of Canada
from . import nbp           # noqa: F401  National Bank of Poland
from . import cnb           # noqa: F401  Czech National Bank
from . import cbr           # noqa: F401  Bank of Russia
from . import bnr           # noqa: F401  National Bank of Romania
from . import nbkz          # noqa: F401  National Bank of Kazakhstan
from . import tcmb          # noqa: F401  Central Bank of Turkey
from . import rba           # noqa: F401  Reserve Bank of Australia
from . import cbk           # noqa: F401  Central Bank of Kuwait
from . import bcb           # noqa: F401  Central Bank of Brazil
from . import banrepco      # noqa: F401  Bank of the Republic (Colombia TRM)
from . import bcrp          # noqa: F401  Central Reserve Bank of Peru
from . import cbb           # noqa: F401  Central Bank of Bahrain
from . import cbu           # noqa: F401  Central Bank of Uzbekistan
from . import bnm           # noqa: F401  Bank Negara Malaysia
from . import bnb           # noqa: F401  Bulgarian National Bank
from . import srb          # noqa: F401  Sveriges Riksbank
from . import bcu           # noqa: F401  Central Bank of Uruguay (SOAP)
from . import erapi         # noqa: F401  open ExchangeRate API aggregator
from . import hmrc          # noqa: F401  HMRC monthly customs rates
from . import gcc_peg       # noqa: F401  Gulf decree pegs (offline, no network)

# Keyed aggregator sources (operator supplies an API key).
from . import fixer         # noqa: F401
from . import oxr           # noqa: F401  Open Exchange Rates
from . import currencylayer  # noqa: F401
from . import banxico       # noqa: F401  Bank of Mexico (FIX, keyed)
