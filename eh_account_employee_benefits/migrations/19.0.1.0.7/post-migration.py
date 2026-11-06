# -*- encoding: utf-8 -*-
"""Preserve proven legacy direct-cash contribution routing."""


def migrate(cr, version):
    if not version:
        return
    # Before 1.0.7 contribution_account_id ambiguously meant either clearing
    # or bank.  Only an actual asset_cash account proves that a false routing
    # flag was a direct-cash configuration.  Copy that exact evidence into the
    # new bank field; leave every ambiguous setup blank so the next posting
    # fails closed and asks the operator to choose the real cash account.
    cr.execute(
        """
        UPDATE eh_benefit_plan AS plan
           SET contribution_cash_account_id = plan.contribution_account_id
          FROM account_account AS account
         WHERE plan.contribution_cash_account_id IS NULL
           AND NOT plan.contributions_posted_elsewhere
           AND account.id = plan.contribution_account_id
           AND account.account_type = 'asset_cash'
        """
    )
