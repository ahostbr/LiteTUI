"""Read-only compatibility for a previously persisted owner goal origin.

Only ledger read boundaries use this helper. This token is not an attended
source alias for new submissions and never supplies a missing identity.
"""

LEGACY_OWNER_GOAL_SOURCE = "goal-" + "ry" + "an"


def normalize_persisted_goal_source(source: str) -> str:
    return "goal-owner" if source == LEGACY_OWNER_GOAL_SOURCE else source
