# -*- encoding: utf-8 -*-
"""Re-run child-anchor checks for databases already on 1.0.4."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def migrate(cr, version):
    path = (
        Path(__file__).resolve().parents[1]
        / '19.0.1.0.4'
        / 'pre-migration.py'
    )
    spec = spec_from_file_location('eh_sbp_anchor_pre_migration', path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.migrate(cr, version)
