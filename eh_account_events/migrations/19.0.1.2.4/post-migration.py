# -*- encoding: utf-8 -*-
"""Re-run the corrected 1.2.3 proof-first provenance audit.

Databases that already installed the intermediate 1.2.3 build still need the
expanded non-primary-link audit.  Keep one implementation of the historical
proof predicates and execute it for the 1.2.4 upgrade as well.
"""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def migrate(cr, version):
    path = (
        Path(__file__).resolve().parents[1]
        / '19.0.1.2.3'
        / 'post-migration.py'
    )
    spec = spec_from_file_location(
        'eh_account_events_provenance_migration_123', path,
    )
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.migrate(cr, version)
