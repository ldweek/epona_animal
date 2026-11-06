# -*- encoding: utf-8 -*-
"""Run the stamped-evidence audit for databases already on 1.0.4."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def migrate(cr, version):
    path = (
        Path(__file__).resolve().parents[1]
        / '19.0.1.0.4'
        / 'post-migration.py'
    )
    spec = spec_from_file_location('eh_sbp_fail_closed_post_migration', path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.migrate(cr, version)
