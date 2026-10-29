"""Launch-local holds for recovered input; never rewrite delivery evidence."""


def held(item):
    return item.get("_restored_input_held") is True


def mark(app, item):
    if getattr(app, "_hold_restored_input", False):
        item["_restored_input_held"] = True


def status(app):
    return {
        "policy": "hold" if getattr(app, "_hold_restored_input", False) else "automatic",
        "held": sum(held(item) for item in getattr(app, "_pending_input", ())),
    }
