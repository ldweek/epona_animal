# -*- encoding: utf-8 -*-
"""Re-run pre-recompute anchor capture for 1.0.3 databases."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def migrate(cr, version):
    path = (
        Path(__file__).resolve().parents[1]
        / '19.0.1.0.3'
        / 'pre-migration.py'
    )
    spec = spec_from_file_location('eh_benefit_anchor_pre_migration', path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.migrate(cr, version)
