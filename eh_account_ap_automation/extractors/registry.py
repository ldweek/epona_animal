# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Extractor registry: same shape as the bank-connector and parser
registries elsewhere in the suite. Concrete extractors register
themselves under a unique EXTRACTOR_KEY at import time.
"""

from .base import LineItemExtractor


_EXTRACTORS = {}


def register_extractor(cls):
    if not isinstance(cls, type) or not issubclass(cls, LineItemExtractor):
        raise TypeError(
            "register_extractor expects a LineItemExtractor subclass, got %r"
            % (cls,),
        )
    if not cls.EXTRACTOR_KEY:
        raise ValueError(
            "%s.EXTRACTOR_KEY must be set before registration"
            % cls.__name__,
        )
    _EXTRACTORS[cls.EXTRACTOR_KEY] = cls
    return cls


def get_extractor(key):
    cls = _EXTRACTORS.get(key)
    if cls is None:
        raise KeyError(
            "No line-item extractor registered for key %r. Available: %s"
            % (key, sorted(_EXTRACTORS)),
        )
    return cls()


def has_extractor(key):
    return key in _EXTRACTORS


def extractor_choices():
    """Return [(key, label), ...] for selection fields and UI menus."""
    return sorted(
        ((cls.EXTRACTOR_KEY, cls.EXTRACTOR_LABEL)
         for cls in _EXTRACTORS.values()),
        key=lambda x: x[0],
    )
