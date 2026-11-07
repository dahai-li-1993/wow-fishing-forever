"""`cedargrove gui`: the setup and fishing window."""

from __future__ import annotations

import typer

from cedargrove.commands._common import fail


def gui() -> None:
    """Open the cedargrove window (setup, calibration, bite check, and the bot itself)."""
    try:
        import tkinter  # noqa: F401

        import ttkbootstrap  # noqa: F401
    except ImportError as e:
        fail(f"the GUI needs Tk and ttkbootstrap ({e}); use `cedargrove run` or install them")
    from cedargrove.gui.app import run

    try:
        run()
    except Exception as e:  # e.g. no display
        fail(f"cannot open the window: {e}")


def register(app: typer.Typer) -> None:
    app.command("gui")(gui)
