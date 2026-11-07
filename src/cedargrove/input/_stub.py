from __future__ import annotations

from cedargrove.capture import WindowRef
from cedargrove.errors import InputUnavailable


class StubInputDevice:
    def press_key(self, key: str, hold_s: float = 0.06) -> None:
        raise InputUnavailable("input injection on this platform")

    def is_game_focused(self, window: WindowRef) -> bool | None:
        return None
