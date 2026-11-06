from . import models
from . import wizards


def pre_init_hook(env):
    """Enable btree_gist before the model tables are created.

    eh.collections.case carries a GiST EXCLUDE constraint over the
    integer (partner_id, company_id) pair; stock PostgreSQL GiST has no
    operator class for integer equality, so the constraint creation
    aborts the install unless btree_gist is present.
    """
    env.cr.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
