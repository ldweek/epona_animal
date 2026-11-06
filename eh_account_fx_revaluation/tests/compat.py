# -*- encoding: utf-8 -*-
"""Small test-only compatibility helpers for Odoo 16 through 19."""


def user_groups_field(env):
    """Return the res.users group field used by the running Odoo series."""
    return (
        'group_ids'
        if 'group_ids' in env['res.users']._fields
        else 'groups_id'
    )


def add_user_groups(user, *groups):
    """Grant groups without embedding a series-specific field name."""
    commands = []
    for groups_arg in groups:
        commands.extend((4, group_id) for group_id in groups_arg.ids)
    user.write({user_groups_field(user.env): commands})
    return user


def user_group_values(env, *groups):
    """Return portable values suitable for res.users.create()."""
    ids = []
    for groups_arg in groups:
        ids.extend(groups_arg.ids)
    return {user_groups_field(env): [(6, 0, ids)]}
