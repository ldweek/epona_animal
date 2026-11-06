from . import models


def post_init_hook(env):
    """Seed the IAS 7 cash flow account tags from the suite's own account
    configuration (asset depreciation expense, impairment loss, provision
    expense, FX revaluation gain/loss, fair value gain/loss). Additive and
    idempotent; every probe is defensive, so a partial suite install never
    fails here. Operators can re-run the same pass any time via the
    "Apply IAS 7 Cash Flow Tags" server action.
    """
    env['eh.noncash.transaction'].action_eh_ias7_auto_tag()
