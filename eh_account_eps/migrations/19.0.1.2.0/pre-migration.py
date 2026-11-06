def migrate(cr, version):
    """Refuse ambiguous same-day balances before adding uniqueness.

    Each row is a total share balance, not a delta. Choosing one of two
    same-day rows would fabricate accounting evidence, so upgrade stops with
    exact run/date identities for controlled remediation.
    """
    cr.execute(
        """
        SELECT run_id, effective_date, array_agg(id ORDER BY id)
          FROM eh_eps_share_movement
      GROUP BY run_id, effective_date
        HAVING count(*) > 1
      ORDER BY run_id, effective_date
        """
    )
    duplicates = cr.fetchall()
    if duplicates:
        detail = '; '.join(
            'run=%s date=%s rows=%s' % (run_id, date, ids)
            for run_id, date, ids in duplicates
        )
        raise RuntimeError(
            "EPS upgrade refused ambiguous same-date share balances: %s. "
            "Review the evidence and retain exactly one authoritative total "
            "balance for each run/date before retrying." % detail
        )
