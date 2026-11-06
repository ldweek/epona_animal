def migrate(cr, version):
    # Preserve true manual aliases only where no dated evidence owns them.
    cr.execute(
        """
        UPDATE eh_eps_run run
           SET manual_restatement_factor = run.restatement_factor
         WHERE NOT EXISTS (
                   SELECT 1
                     FROM eh_eps_restatement_event event
                    WHERE event.run_id = run.id
               )
           AND run.restatement_factor > 0
        """
    )
    cr.execute(
        """
        UPDATE eh_eps_potential potential
           SET manual_average_market_price = potential.average_market_price
          FROM eh_eps_run run
         WHERE run.id = potential.run_id
           AND NOT EXISTS (
                   SELECT 1
                     FROM eh_eps_price_observation observation
                    WHERE observation.potential_id = potential.id
                      AND observation.date BETWEEN
                          run.period_start AND run.period_end
               )
        """
    )
    # Cancelled disclosures remain queryable through archived-record filters,
    # but do not enter default list/graph/pivot populations.
    cr.execute(
        "UPDATE eh_eps_run SET active = FALSE WHERE state = 'cancelled'"
    )
    cr.execute(
        "UPDATE eh_eps_run SET ever_computed = TRUE "
        "WHERE state IN ('computed', 'cancelled')"
    )
