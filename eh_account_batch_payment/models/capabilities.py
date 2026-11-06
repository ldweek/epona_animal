# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Process-local capabilities shared by batch-payment model extensions."""


AUTHORIZED_BATCH_POST_CONTEXT = 'eh_authorized_batch_payment_post'
AUTHORIZED_BATCH_POST_CAPABILITY = object()
AUTHORIZED_BATCH_RECONCILE_CONTEXT = 'eh_authorized_batch_reconcile'
AUTHORIZED_BATCH_RECONCILE_CAPABILITY = object()
AUTHORIZED_BATCH_REVERSAL_CONTEXT = 'eh_authorized_batch_reversal'
AUTHORIZED_BATCH_REVERSAL_CAPABILITY = object()
AUTHORIZED_BATCH_EXPORT_CONTEXT = 'eh_authorized_batch_payment_export'
AUTHORIZED_BATCH_EXPORT_CAPABILITY = object()

AUTHORIZED_BATCH_WORKFLOW_CONTEXT = 'eh_authorized_batch_workflow_write'
AUTHORIZED_BATCH_WORKFLOW_CAPABILITY = object()
AUTHORIZED_DIRECT_PAYMENT_POST_CONTEXT = 'eh_authorized_direct_payment_post'
AUTHORIZED_DIRECT_PAYMENT_POST_CAPABILITY = object()
AUTHORIZED_BATCH_MEMBER_MUTATION_CONTEXT = (
    'eh_authorized_batch_member_mutation'
)
AUTHORIZED_BATCH_MEMBER_MUTATION_CAPABILITY = object()
AUTHORIZED_BATCH_BULK_CREATE_CONTEXT = 'eh_authorized_batch_bulk_create'
AUTHORIZED_BATCH_BULK_CREATE_CAPABILITY = object()
CORE_RECONCILE_CONTEXT = 'eh_core_reconcile_capability'
CORE_RECONCILE_CAPABILITY = object()
CORE_AML_CONTEXT = 'eh_core_aml_capability'
CORE_AML_CAPABILITY = object()


def has_authorized_batch_post_capability(env):
    """Return whether ``env`` carries the unforgeable batch-post token."""
    return (
        env.su
        and env.context.get(AUTHORIZED_BATCH_POST_CONTEXT)
        is AUTHORIZED_BATCH_POST_CAPABILITY
    )


def has_authorized_batch_reconcile_capability(env):
    """Return whether ``env`` carries the unforgeable reconciliation token."""
    return (
        env.context.get(AUTHORIZED_BATCH_RECONCILE_CONTEXT)
        is AUTHORIZED_BATCH_RECONCILE_CAPABILITY
    )


def has_authorized_batch_reversal_capability(env):
    """Return whether manager-owned batch reversal holds its exact token."""
    return (
        env.context.get(AUTHORIZED_BATCH_REVERSAL_CONTEXT)
        is AUTHORIZED_BATCH_REVERSAL_CAPABILITY
    )


def has_authorized_batch_export_capability(env):
    """Return whether ``env`` carries the unforgeable export-write token."""
    return (
        env.su
        and env.context.get(AUTHORIZED_BATCH_EXPORT_CONTEXT)
        is AUTHORIZED_BATCH_EXPORT_CAPABILITY
    )


def has_authorized_batch_workflow_capability(env):
    """Whether batch state/audit fields are in server workflow corridor."""
    return (
        env.su
        and env.context.get(AUTHORIZED_BATCH_WORKFLOW_CONTEXT)
        is AUTHORIZED_BATCH_WORKFLOW_CAPABILITY
    )


def has_authorized_direct_payment_post_capability(env):
    """Return whether ``env`` carries the locked direct-payment post token."""
    return (
        env.context.get(AUTHORIZED_DIRECT_PAYMENT_POST_CONTEXT)
        is AUTHORIZED_DIRECT_PAYMENT_POST_CAPABILITY
    )


def has_authorized_payment_post_capability(env):
    """Accept either locked batch-post or locked direct-payment corridor."""
    return (
        has_authorized_batch_post_capability(env)
        or has_authorized_direct_payment_post_capability(env)
    )


def has_authorized_batch_member_mutation_capability(env):
    """Return whether a locked payment edit owns draft move-line changes."""
    return (
        env.context.get(AUTHORIZED_BATCH_MEMBER_MUTATION_CONTEXT)
        is AUTHORIZED_BATCH_MEMBER_MUTATION_CAPABILITY
    )


def has_authorized_batch_bulk_create_capability(env):
    """Whether core is completing members before one final validation."""
    return (
        env.context.get(AUTHORIZED_BATCH_BULK_CREATE_CONTEXT)
        is AUTHORIZED_BATCH_BULK_CREATE_CAPABILITY
    )


def has_core_reconcile_capability(env):
    """Return whether locked core reconciliation owns nested evidence work."""
    return env.context.get(CORE_RECONCILE_CONTEXT) is CORE_RECONCILE_CAPABILITY


def has_core_aml_capability(env):
    """Return whether a preflighted move operation owns nested AML work."""
    return env.context.get(CORE_AML_CONTEXT) is CORE_AML_CAPABILITY
