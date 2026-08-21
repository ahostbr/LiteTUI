"""Model selection and reconnection — /model, /models, /reconnect.

Handler bodies moved verbatim from the chain (self -> app). The connection
machinery (_connect, _fetch_ctx_window, _apply_context_length) stays
app-owned; these are its command surfaces.
"""
from picker import PickerScreen
from plugins import PluginManifest


def _cmd_model(app, name: str, arg: str) -> None:
    if arg:
        # Switch by number or name
        if arg.isdigit():
            idx = int(arg) - 1
            if 0 <= idx < len(app.available_models):
                app.model_id = app.available_models[idx]
                app._update_header()
                app._fetch_ctx_window()
                app._system(f"Switched to: {app.model_id}")
                # An explicit switch is an explicit act — the thing the
                # no-load-on-connect rule asks for. Boot still loads
                # nothing.
                app._apply_context_length()
            else:
                app._system(f"Invalid number. Use 1-{len(app.available_models)}")
        elif arg in app.available_models:
            app.model_id = arg
            app._update_header()
            app._fetch_ctx_window()
            app._system(f"Switched to: {app.model_id}")
            app._apply_context_length()
        else:
            app._system(f"Model not found: {arg}")
    elif not app.available_models:
        app._system("No models discovered — try /reconnect")
    else:
        # Clickable picker. `/model <n>` and `/model <name>` are handled
        # above and still work, so scripting and muscle memory survive.
        rows = [
            (m, ("▸ " if m == app.model_id else "  ") + m)
            for m in app.available_models
        ]
        app.push_screen(
            PickerScreen("Select a model", rows, current=app.model_id),
            app._on_model_picked,
        )


def _cmd_reconnect(app, name: str, arg: str) -> None:
    app._connect()


def _register(ctx) -> None:
    ctx.command(
        ("/model", "/models"), _cmd_model,
        palette="Switch model",
        help="Pick from the connected server's models (/model)",
    )
    ctx.command(
        ("/reconnect",), _cmd_reconnect,
        palette="Reconnect",
        help="Reconnect to the model server (/reconnect)",
    )


PLUGIN = PluginManifest(id="model-switch", register=_register)
