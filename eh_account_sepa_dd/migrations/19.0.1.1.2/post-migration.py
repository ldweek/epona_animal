# -*- encoding: utf-8 -*-
"""Recover exact local-instrument identity from legacy PAIN.008 bytes."""

import hashlib
import logging
from xml.etree import ElementTree

from odoo import SUPERUSER_ID, api


_logger = logging.getLogger(__name__)
_ALLOWED = {'CORE', 'B2B', 'COR1'}


def _instrument_from_export(export):
    attachment = export.attachment_id.sudo().with_context(
        eh_sepa_dd_export_migration=True,
    )
    raw = attachment.raw or b''
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    if not raw or not export.file_hash:
        return False
    if hashlib.sha256(raw).hexdigest() != export.file_hash:
        return False
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return False
    values = {
        node.text.strip()
        for node in root.findall('.//{*}LclInstrm/{*}Cd')
        if node.text and node.text.strip() in _ALLOWED
    }
    return values.pop() if len(values) == 1 else False


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    exports = env['eh.sepa.dd.export'].sudo().search([
        ('local_instrument', '=', False),
    ], order='id')
    recovered = quarantined = 0
    for export in exports:
        instrument = _instrument_from_export(export)
        if instrument:
            export.write({'local_instrument': instrument})
            recovered += 1
            continue
        if export.state in ('generated', 'downloaded'):
            export.write({'state': 'superseded'})
            quarantined += 1
    _logger.info(
        "eh_account_sepa_dd 19.0.1.1.2: recovered local-instrument "
        "identity for %d legacy exports; retired %d ambiguous live exports.",
        recovered, quarantined,
    )
