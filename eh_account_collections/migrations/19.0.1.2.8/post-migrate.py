# -*- encoding: utf-8 -*-
"""Wire shipped branded templates on existing noupdate installations."""


def migrate(cr, version):
    del version
    cr.execute("""
        WITH wiring(level_xmlid, legacy_template_xmlid, template_xmlid) AS (
            VALUES
                ('followup_level_1',
                 'mail_template_followup_reminder_1',
                 'email_template_eh_dunning_friendly'),
                ('followup_level_2',
                 'mail_template_followup_reminder_2',
                 'email_template_eh_dunning_firm'),
                ('followup_level_3',
                 'mail_template_followup_final',
                 'email_template_eh_dunning_final')
        ), resolved AS (
            SELECT level_data.res_id AS level_id,
                   legacy_data.res_id AS legacy_template_id,
                   template_data.res_id AS template_id
              FROM wiring
              JOIN ir_model_data AS level_data
                ON level_data.module = 'eh_account_collections'
               AND level_data.name = wiring.level_xmlid
               AND level_data.model = 'eh.collections.followup.level'
              JOIN ir_model_data AS legacy_data
                ON legacy_data.module = 'eh_account_collections'
               AND legacy_data.name = wiring.legacy_template_xmlid
               AND legacy_data.model = 'mail.template'
              JOIN ir_model_data AS template_data
                ON template_data.module = 'eh_account_collections'
               AND template_data.name = wiring.template_xmlid
               AND template_data.model = 'mail.template'
        )
        UPDATE eh_collections_followup_level AS level
           SET mail_template_id = resolved.template_id,
               write_date = NOW()
          FROM resolved
         WHERE level.id = resolved.level_id
           AND level.mail_template_id IS DISTINCT FROM resolved.template_id
           AND (
               level.mail_template_id IS NULL
               OR level.mail_template_id = resolved.legacy_template_id
           )
    """)
    template_where = """
        FROM ir_model_data AS data
       WHERE data.module = 'eh_account_collections'
         AND data.name IN (
             'email_template_eh_dunning_friendly',
             'email_template_eh_dunning_firm',
             'email_template_eh_dunning_final'
         )
         AND data.model = 'mail.template'
         AND template.id = data.res_id
    """
    # Only repair the exact expression shipped by the previous release.
    # A customer-authored sender expression on this noupdate record is data,
    # not module metadata, and must survive an upgrade.
    cr.execute("""
        UPDATE mail_template AS template
           SET email_from =
                   '{{ object.collector_id.email_formatted or '
                   '(object.company_id.partner_id and '
                   'object.company_id.partner_id.email_formatted) }}',
               write_date = NOW()
    """ + template_where + """
         AND template.email_from =
             '{{ (object.company_id.email or user.email) | safe }}'
    """)
    # Translated fields are JSONB on newer series and varchar/text on older
    # ones. Restrict rewrites to the exact phrases previously shipped so
    # customer-authored noupdate template prose remains untouched.
    for column, old, new in (
        ('name', 'EH Dunning — ', 'EH Dunning: '),
        ('subject', ' balance — {{ object.partner_id.name }}',
         ' balance - {{ object.partner_id.name }}'),
        ('subject', ' referral — {{ object.partner_id.name }}',
         ' referral - {{ object.partner_id.name }}'),
        ('body_html', 'please ignore this message — our records may',
         'please ignore this message; our records may'),
    ):
        cr.execute("""
            SELECT data_type
              FROM information_schema.columns
             WHERE table_schema = current_schema()
               AND table_name = 'mail_template'
               AND column_name = %s
        """, [column])
        is_jsonb = cr.fetchone()[0] == 'jsonb'
        value_sql = (
            "REPLACE(template.%s::text, %%s, %%s)::jsonb" % column
            if is_jsonb
            else "REPLACE(template.%s, %%s, %%s)" % column
        )
        cr.execute(
            "UPDATE mail_template AS template SET %s = %s, "
            "write_date = NOW() %s AND POSITION(%%s IN "
            "template.%s::text) > 0" % (
                column, value_sql, template_where, column,
            ),
            [old, new, old],
        )
