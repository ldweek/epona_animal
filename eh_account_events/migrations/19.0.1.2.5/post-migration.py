# -*- encoding: utf-8 -*-
"""Re-run the fail-closed stamped-evidence audit for 1.2.4 databases."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def migrate(cr, version):
    path = (
        Path(__file__).resolve().parents[1]
        / '19.0.1.2.3'
        / 'post-migration.py'
    )
    spec = spec_from_file_location(
        'eh_account_events_fail_closed_provenance', path,
    )
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.migrate(cr, version)
