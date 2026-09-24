"""Generation-isolated module loading for the WS7 handler-reload full scope.

The swap (``plugin_reload_swap``) and the eligibility contract
(``plugin_reload_eligibility``) are done. This module closes the last remaining
in-gate piece of a handler-generation swap: getting the NEW generation's
handlers WITHOUT mutating the live module.

Why a fresh module object, not ``importlib.reload``:

  * ``import_module`` on an already-imported module returns the CACHED module and
    does NOT re-execute its body, so it cannot produce a new generation at all.
  * ``importlib.reload`` re-runs the body IN PLACE on the existing module object.
    It is not transactional: if the body raises halfway, the module's globals are
    left PARTIALLY updated -- a live generation corrupted by a failed reload.

Generation isolation instead compiles the (re)written source into a brand-new
``types.ModuleType`` and execs it there. The new module is NEVER registered in
``sys.modules`` and the live module object is NEVER touched, so:

  * on SUCCESS the new generation is a distinct object; dispatch sees it only
    once the swap installs the candidate registry that references its handlers;
  * on ANY failure the live generation is byte-for-byte intact, so the caller
    reports restart-required / "reload failed, live generation preserved" with
    NO rollback to claim. This is the spec's "Do not advertise rollback if
    imported replacement code already mutated live module state" -- by loading a
    fresh module we never mutate the live state, so there is nothing to roll back.

Trust model: exec'ing the source is the SAME trust ``import_module`` already
assumes for a local plugin module; no new surface is added. The eligibility gate
(d8e9030) is what keeps only stateless, side-effect-free plugins in this path.
"""
from __future__ import annotations

import types
import uuid
from dataclasses import dataclass
from typing import Any

#: Name the fresh module's ``__file__`` points at. A sentinel (not a real path)
#: so the fresh generation can be told apart from the live one in diagnostics.
_FRESH_FILE = "<{name}:fresh-generation>"


@dataclass(frozen=True)
class Generation:
    """One isolated generation of a plugin module.

    ``token`` is a unique identity for THIS generation so an in-flight reference
    or a diagnostic can name the generation it belongs to (spec: "Retain a
    generation identity for in-flight references until they are released").
    ``module`` is the fresh ``ModuleType``; ``manifest`` is its ``.PLUGIN``.
    """

    module_name: str
    module: types.ModuleType
    manifest: Any
    token: str


def load_fresh_generation(module_name: str, source: str) -> Generation:
    """Compile ``source`` into a FRESH module object and exec it there.

    The live ``sys.modules[module_name]`` (if any) is never touched or replaced,
    and the new module is never registered in ``sys.modules``. Raises on any
    failure (compile error, exception during exec, or a module without a
    ``PLUGIN`` manifest) -- in which case the live generation is untouched and
    the caller reports the failure with no rollback claim.

    ``source`` is the module body as a string (the caller reads it from disk via
    ``read_live_module_source`` in production). Keeping it a parameter is what
    makes this unit testable without a disk read and keeps the disk-read path
    separable from the (trust-critical) exec.
    """
    code = compile(source, _FRESH_FILE.format(name=module_name), "exec")
    module = types.ModuleType(module_name)
    module.__file__ = _FRESH_FILE.format(name=module_name)
    # Deliberately NOT `sys.modules[module_name] = module`: the live generation
    # must survive an in-flight exec and a failed one alike.
    exec(code, module.__dict__)  # noqa: S102 -- trusted local plugin source
    if not hasattr(module, "PLUGIN"):
        # The half-built module is discarded (never registered); the live one is
        # untouched. This is a clean "invalid replacement" refusal, not a crash.
        raise ValueError(f"{module_name}: fresh generation has no PLUGIN manifest")
    return Generation(module_name=module_name, module=module,
                      manifest=module.PLUGIN, token=uuid.uuid4().hex)


def read_live_module_source(module_name: str) -> str:
    """Read the CURRENT on-disk source of an already-imported module.

    Uses the live module's ``__file__`` (the module is imported, so this does not
    re-execute anything) and returns the file's present contents -- which differ
    from the loaded generation exactly when the file was edited on disk, the
    whole reason a reload exists. A file that has vanished or is unreadable
    raises, which the caller reports as a failed reload (live generation intact).
    """
    import importlib
    live = importlib.import_module(module_name)
    path = getattr(live, "__file__", None)
    if not path:
        raise ValueError(f"{module_name}: no source path on the live module")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
