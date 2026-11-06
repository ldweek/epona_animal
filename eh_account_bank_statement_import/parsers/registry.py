# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Parser registry: maps a format key to a parser class.

Subclasses can register their own parsers by importing this module and
appending to PARSERS, keyed by FORMAT_KEY. Format keys must be unique;
registering an existing key replaces the prior parser, which lets a
localization swap the default behaviour without touching the wizard.
"""

from .csv_parser import CsvStatementParser
from .ofx_parser import OfxStatementParser
from .qif_parser import QifStatementParser
from .camt_parser import Camt053StatementParser
from .mt940_parser import Mt940StatementParser


PARSERS = {
    CsvStatementParser.FORMAT_KEY: CsvStatementParser,
    OfxStatementParser.FORMAT_KEY: OfxStatementParser,
    QifStatementParser.FORMAT_KEY: QifStatementParser,
    Camt053StatementParser.FORMAT_KEY: Camt053StatementParser,
    Mt940StatementParser.FORMAT_KEY: Mt940StatementParser,
}


def get_parser(format_key):
    cls = PARSERS.get(format_key)
    if cls is None:
        raise KeyError(
            "No parser registered for format %r. Available: %s"
            % (format_key, sorted(PARSERS)),
        )
    return cls()


def format_choices():
    """Return [(key, label), ...] for selection fields and UI menus."""
    return sorted(
        ((cls.FORMAT_KEY, cls.FORMAT_LABEL) for cls in PARSERS.values()),
        key=lambda x: x[0],
    )
