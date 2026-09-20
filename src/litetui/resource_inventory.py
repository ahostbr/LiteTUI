"""Strict residency interpretation; malformed catalogue is not absence evidence."""


def lmstudio_residency(rows, model):
    """Interpret a complete native model listing; callers own endpoint/freshness.

    This function alone is not a live probe. Never feed a failed request's
    empty-list fallback to it. Unknown states/duplicate IDs retain resources.
    """
    if not isinstance(rows, list) or not isinstance(model, str) or not model:
        return None
    states = {}
    for row in rows:
        if not isinstance(row, dict):
            return None
        key = row.get('id')
        if not isinstance(key, str) or not key or key in states:
            return None
        state = row.get('state')
        context = row.get('loaded_context_length')
        if state is not None:
            if state not in ('loaded', 'not-loaded'):
                return None
            resident = state == 'loaded'
            if context is not None and (type(context) is not int or context < 0 or (context > 0) != resident):
                return None
        elif type(context) is int and context > 0:
            resident = True
        else:
            # Missing/zero context alone does not prove unload completion.
            resident = None
        states[key] = resident
    return states.get(model, False)
