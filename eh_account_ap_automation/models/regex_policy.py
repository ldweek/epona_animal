# -*- encoding: utf-8 -*-
"""Fail-closed regular-expression policy for untrusted bill text."""

import re

try:  # Python 3.11+
    from re import _constants as sre_constants
    from re import _parser as sre_parse
except ImportError:  # pragma: no cover - retained for older series backports
    import sre_constants
    import sre_parse


MAX_PATTERN_LENGTH = 1000
MAX_SEARCH_CHARACTERS = 64 * 1024
MAX_FIXED_REPEAT = 1000

_REPEAT_OPS = {
    sre_constants.MAX_REPEAT,
    sre_constants.MIN_REPEAT,
}
if hasattr(sre_constants, 'POSSESSIVE_REPEAT'):
    _REPEAT_OPS.add(sre_constants.POSSESSIVE_REPEAT)

_ASCII = frozenset(range(128))
_DIGIT = frozenset(range(ord('0'), ord('9') + 1))
_SPACE = frozenset(ord(char) for char in ' \t\n\r\v\f')
_WORD = frozenset(
    list(range(ord('0'), ord('9') + 1))
    + list(range(ord('A'), ord('Z') + 1))
    + list(range(ord('a'), ord('z') + 1))
    + [ord('_')]
)


def _category_ascii(category):
    mapping = {
        sre_constants.CATEGORY_DIGIT: _DIGIT,
        sre_constants.CATEGORY_NOT_DIGIT: _ASCII - _DIGIT,
        sre_constants.CATEGORY_SPACE: _SPACE,
        sre_constants.CATEGORY_NOT_SPACE: _ASCII - _SPACE,
        sre_constants.CATEGORY_WORD: _WORD,
        sre_constants.CATEGORY_NOT_WORD: _ASCII - _WORD,
    }
    return mapping.get(category)


def _ascii_casefold(values):
    expanded = set(values)
    for value in values:
        char = chr(value)
        for variant in (char.lower(), char.upper()):
            if len(variant) == 1 and ord(variant) < 128:
                expanded.add(ord(variant))
    return frozenset(expanded)


def _atom_ascii(op, arg, ignore_case=False):
    if op == sre_constants.LITERAL:
        result = frozenset({arg}) if arg < 128 else frozenset()
        return _ascii_casefold(result) if ignore_case else result
    if op == sre_constants.NOT_LITERAL:
        result = _ASCII - frozenset({arg})
        return _ascii_casefold(result) if ignore_case else result
    if op == sre_constants.CATEGORY:
        result = _category_ascii(arg)
        return (
            _ascii_casefold(result)
            if ignore_case and result is not None else result
        )
    if op == sre_constants.ANY:
        return _ASCII - frozenset({ord('\n')})
    if op != sre_constants.IN:
        return None
    negate = False
    values = set()
    for item_op, item_arg in arg:
        if item_op == sre_constants.NEGATE:
            negate = True
        elif item_op == sre_constants.LITERAL:
            if item_arg < 128:
                values.add(item_arg)
        elif item_op == sre_constants.RANGE:
            start, stop = item_arg
            values.update(range(max(start, 0), min(stop, 127) + 1))
        elif item_op == sre_constants.CATEGORY:
            category = _category_ascii(item_arg)
            if category is None:
                return None
            values.update(category)
        else:
            return None
    result = frozenset(values)
    result = _ASCII - result if negate else result
    return _ascii_casefold(result) if ignore_case else result


def _simple_variable_repeat(token, ignore_case=False):
    op, arg = token
    if op == sre_constants.SUBPATTERN:
        _group, _add_flags, _del_flags, child = arg
        if len(child) != 1:
            return None
        return _simple_variable_repeat(child[0], ignore_case=ignore_case)
    if op not in _REPEAT_OPS:
        return None
    minimum, maximum, child = arg
    if minimum == maximum:
        return None
    if len(child) != 1:
        raise ValueError(
            "repetition may only apply to one literal or character class"
        )
    child_op, child_arg = child[0]
    charset = _atom_ascii(child_op, child_arg, ignore_case=ignore_case)
    if charset is None or child_op == sre_constants.ANY:
        raise ValueError(
            "wildcard, grouped, nested, or unsupported repetition is not allowed"
        )
    return charset


def _validate_sequence(sequence, variable_repeats, shape, ignore_case=False):
    for token in sequence:
        op, arg = token
        if op in _REPEAT_OPS:
            charset = _simple_variable_repeat(
                token, ignore_case=ignore_case,
            )
            if charset is None:
                # A fixed repeat is safe only over one simple atom.
                minimum, _maximum, child = arg
                atom = (
                    _atom_ascii(
                        *child[0], ignore_case=ignore_case,
                    )
                    if len(child) == 1 else None
                )
                if atom is None:
                    raise ValueError(
                        "fixed repetition may only apply to one literal or "
                        "character class"
                    )
                if minimum > MAX_FIXED_REPEAT:
                    raise ValueError(
                        "fixed repetition exceeds %s" % MAX_FIXED_REPEAT
                    )
                if minimum:
                    shape.append(('atom', atom))
                continue
            if not charset:
                raise ValueError(
                    "quantified atoms must have a supported ASCII character set"
                )
            if any(
                    charset.intersection(previous)
                    for previous in variable_repeats):
                raise ValueError(
                    "overlapping variable repetitions are not allowed"
                )
            minimum, maximum, _child = arg
            variable_repeats.append(charset)
            shape.append(('repeat', charset, minimum, maximum))
            continue
        if op == sre_constants.SUBPATTERN:
            _group, add_flags, del_flags, child = arg
            if add_flags or del_flags:
                raise ValueError("scoped regular-expression flags are not allowed")
            _validate_sequence(
                child, variable_repeats, shape,
                ignore_case=ignore_case,
            )
            continue
        if op == sre_constants.AT:
            shape.append(('anchor', arg))
            continue
        atom = _atom_ascii(op, arg, ignore_case=ignore_case)
        if atom is not None:
            shape.append(('atom', atom))
            continue
        # Alternation, backreferences, lookarounds and conditionals can all
        # reintroduce super-linear backtracking.  This parser is deliberately
        # a small safe subset; configuration can be rewritten with classes.
        raise ValueError(
            "alternation, lookaround, backreferences, and conditional "
            "expressions are not allowed"
        )


def _validate_search_shape(shape):
    """Exclude the unanchored retry shapes that make ``re.search`` quadratic.

    A leading literal followed by a mandatory, disjoint quantified class gives
    the engine an unambiguous boundary.  A single terminal repetition is also
    safe: it either returns the first run or scans the input once.  Everything
    else must be anchored at the beginning of the string (or each line).
    """
    beginning_anchors = {
        sre_constants.AT_BEGINNING,
        sre_constants.AT_BEGINNING_STRING,
    }
    if hasattr(sre_constants, 'AT_BEGINNING_LINE'):
        beginning_anchors.add(sre_constants.AT_BEGINNING_LINE)
    significant = [item for item in shape if item[0] != 'anchor']
    repeats = [item for item in significant if item[0] == 'repeat']
    if not repeats:
        return
    if shape and shape[0][0] == 'anchor' \
            and shape[0][1] in beginning_anchors:
        return
    first_repeat_index = next(
        index for index, item in enumerate(significant)
        if item[0] == 'repeat'
    )
    prefix = significant[:first_repeat_index]
    if not prefix:
        if len(shape) == 1 and len(significant) == 1 and len(repeats) == 1:
            return
        raise ValueError(
            "an unanchored variable repetition needs a mandatory literal prefix"
        )
    first_repeat = significant[first_repeat_index]
    if first_repeat[2] < 1 \
            or not prefix[-1][1] \
            or prefix[-1][1].intersection(first_repeat[1]):
        raise ValueError(
            "an unanchored repetition needs a disjoint mandatory prefix"
        )


def compile_safe_regex(pattern):
    if not pattern or len(pattern) > MAX_PATTERN_LENGTH:
        raise ValueError(
            "pattern is missing or exceeds %s characters"
            % MAX_PATTERN_LENGTH
        )
    try:
        parsed = sre_parse.parse(pattern, 0)
        compiled = re.compile(pattern)
    except (re.error, OverflowError) as exc:
        raise ValueError("pattern is invalid: %s" % exc) from exc
    variable_repeats = []
    shape = []
    _validate_sequence(
        parsed, variable_repeats, shape,
        ignore_case=bool(parsed.state.flags & re.IGNORECASE),
    )
    _validate_search_shape(shape)
    if compiled.groups < 1:
        raise ValueError("pattern needs one capture group")
    return compiled
