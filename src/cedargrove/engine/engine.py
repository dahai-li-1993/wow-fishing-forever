"""The fishing loop. One `step()` per iteration; every collaborator is injectable.

Cast, let the bobber land, listen for the splash, press Interact, give Auto Loot a moment, cast
again. The splash decides when to press Interact. The gold gear icon (when calibrated) checks the
cast: no icon within icon_timeout_s means the bobber landed out of range, so recast; a bobber
that vanishes without a heard splash means the fish got away, so recast now rather than at
cast_timeout_s. Because a bad match once threw away every cast, the icon check stands down after
ICON_GIVE_UP misses in a row and the bot fishes by sound until it sees the icon again.
"""

from __future__ import annotations

import math
import queue
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from cedargrove.audio import AudioChunk
from cedargrove.audio._threaded import ThreadedSource
from cedargrove.capture import Box, WindowRef, find_window, get_capturer, linux_x11
from cedargrove.config import Config
from cedargrove.engine import events as ev
from cedargrove.engine.state import State
from cedargrove.vision.calibrate import MAX_THRESHOLD
from cedargrove.vision.region import Region

UNFOCUSED = "World of Warcraft is not the focused window; click into the game"
THUMB_WIDTH = 480
AUDIO_BLOCK = 1024
AUDIO_MAX_S = 2.0  # never read more than this at once; the device buffer may drop older audio
STATS_EVERY_S = 10.0
LISTEN_EVERY_S = 2.0  # while waiting for the splash, say what is heard this often
SCORES_EVERY_S = 0.1  # ...and send each clip's peak score this often, for the GUI's graph
SLEEP_SLICE_S = 0.1
LOOP_SLEEP_S = 0.02  # between steps, so listening does not spin a core
LAND_S = 1.5  # after the cast the bobber flies and lands; nothing heard before this is a bite
LOOK_EVERY_S = 0.3  # full-frame looks for the icon, while it has not been seen this cast
ICON_GIVE_UP = 3  # icon timeouts in a row before fishing by sound until the icon is seen again
KEEP_MARGIN = 0.08  # once seen, the icon stays "there" down to threshold - this (hysteresis)
GONE_S = 3.0  # a tracked bobber unseen this long while listening is gone: recast
NO_SPLASH_WARN = 3  # casts in a row with no splash before saying what to check
# Clicking into the game to resume: a key sent in the same instant can be lost, so wait a moment.
RESUME_SETTLE_S = 1.0
# Before a cast: if a bobber is still out (a channel that outlived cast_timeout_s, or one cast by
# hand before Start), the cast key may only reel it in, which matters when it is also the
# Interact key. Wait up to this long for it to go; then cast anyway.
STALE_WAIT_S = 5.0


class FishingEngine:
    def __init__(
        self,
        cfg: Config,
        window: WindowRef,
        *,
        capturer: Any,
        input_device: Any,
        audio_source: Any,
        detector: Any,
        matcher: Any = None,
        find_window: Callable[..., WindowRef | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        session_s: float | None = None,
        box_interval_s: float = 0.25,
    ) -> None:
        self.cfg = cfg
        self.window = window
        self.capturer = capturer
        self.input = input_device
        self.audio = audio_source
        self.detector = detector
        self.matcher = matcher  # None: no usable calibrated icon; the bot fishes by ear alone
        self._find_window = find_window
        # Older calibrations saved best-score-minus-0.10, which can sit above real finds.
        self.threshold = min(cfg.vision.icon_threshold, MAX_THRESHOLD)
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self.session_s = cfg.game.session_minutes * 60.0 if session_s is None else session_s
        self.box_interval_s = box_interval_s

        self.events: queue.Queue[ev.Event] = queue.Queue()
        self.state = State.IDLE
        self.stats = ev.Stats(0.0)
        self.started: float | None = None
        self._stop = False
        self._resume_state = State.IDLE
        self._t_cast = 0.0
        self._t_interact = 0.0
        self._icon_xy: tuple[int, int] | None = None  # where the icon was last seen this cast
        self._icon_visible = False  # seen on the latest look
        self._unseen_since: float | None = None  # first look of the current run of misses
        self._icon_gate = True  # recast when no icon shows up; off after ICON_GIVE_UP misses
        self._icon_misses = 0  # icon timeouts in a row
        self._last_look = -math.inf
        self._no_splash = 0  # casts in a row that ended without a splash
        self._hold_until = -math.inf  # no cast before this (just resumed)
        self._stale_since: float | None = None  # when the pre-cast check began
        self._stale_said = False
        self._last_audio: float | None = None
        self._audio_frames = 0
        self._last_stats = 0.0
        self._last_listen = 0.0
        self._last_scores_sent = 0.0
        self._best_heard: tuple[str, float] = ("", -1.0)  # best clip score since listening began

    # -- public ------------------------------------------------------------------------------

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        self.capturer.open(self.window)
        try:
            with self.audio:
                while self.state is not State.STOPPED:
                    self.step()
        finally:
            self.capturer.close()

    def step(self) -> None:
        if self.state is State.STOPPED:
            return
        now = self._clock()
        if self.started is None:
            self.started = now
            self._last_audio = now
        if self._stop:
            self._finish("user")
            return
        if self.session_s != 0 and now - self.started >= self.session_s:
            self._finish("session limit")
            return
        try:
            self._step(now)
        except Exception as e:  # a collaborator broke; say so and stop rather than spin
            self._emit(ev.Error(self._t(), f"{type(e).__name__}: {e}"))
            self._finish("error")

    # -- the loop body -----------------------------------------------------------------------

    def _step(self, now: float) -> None:
        focused = self.input.is_game_focused(self.window)
        if focused is False:
            if self.state is not State.PAUSED:
                self._resume_state = self.state
                self.state = State.PAUSED
                self._emit(ev.Paused(self._t(), UNFOCUSED))
            self._drain_audio(now)
            self._sleep(SLEEP_SLICE_S)
            return
        if self.state is State.PAUSED:
            self.state = self._resume_state
            self._hold_until = now + RESUME_SETTLE_S
            self._emit(ev.Resumed(self._t(), self.state))

        chunks = self._drain_audio(now)  # drained every step, so a bite is never stale audio
        bites = []
        if self.state is State.WAIT_BITE:
            for chunk in chunks:
                bites.extend(self.detector.feed(chunk))
            self._note_scores()
            self._send_scores(now, flush=bool(bites))  # flush so the graph gets the bite's peak

        if self.state is State.IDLE:
            if self._ready_to_cast(now):
                self._cast()
            else:
                self._sleep(LOOP_SLEEP_S)
            return
        self._look(now)
        if self._icon_check(now):
            return
        if self.state is State.CASTING:
            self._land(now)
        elif self.state is State.WAIT_BITE:
            self._wait_bite(now, bites)
        elif self.state is State.INTERACT:
            self._after_interact(now)
        self._maybe_stats(now)
        self._sleep(LOOP_SLEEP_S)

    def _ready_to_cast(self, now: float) -> bool:
        if now < self._hold_until:
            return False
        if self.matcher is None:
            return True
        if self._stale_since is None:
            self._stale_since = now
        if now - self._stale_since >= STALE_WAIT_S:
            return True  # it may be something else that looks like the icon; cast anyway
        if now - self._last_look < LOOK_EVERY_S:
            return False
        self._last_look = now
        if self._icon_xy is not None:  # the last cast's bobber: look only where it was
            x, y = self._icon_xy
            w, h = self.window.width, self.window.height
            x0, y0, x1, y1 = Region.around(x, y, w, h, 0.05).to_box(w, h)
            m = self.matcher.match(self.capturer.grab((x0, y0, x1 - x0, y1 - y0)), full_width=w)
        else:
            m = self.matcher.match(self.capturer.grab())
        if not m.accepted(self.threshold):
            return True
        if not self._stale_said:
            self._stale_said = True
            self._change(
                State.IDLE,
                "A bobber is still in the water",
                f"Waiting up to {STALE_WAIT_S:g} s for it to go before casting",
            )
        return False

    def _cast(self) -> None:
        self._stale_since = None
        self._stale_said = False
        if self._find_window is not None:
            found = self._find_window("World of Warcraft")
            if found is not None:
                self.window = found
        key = self.cfg.game.cast_key
        self._press(key, "cast")
        self.stats = ev.Stats(
            self._t(), self.stats.casts + 1, self.stats.catches, self.stats.misses,
            self.stats.bites_heard, self._elapsed(),
        )  # fmt: skip
        self._t_cast = self._clock()
        self._icon_xy = None
        self._icon_visible = False
        self._unseen_since = None
        self._last_look = -math.inf
        self._change(State.CASTING, f"Pressed {key} to cast", "Waiting for the bobber to land")

    def _land(self, now: float) -> None:
        if now - self._t_cast < LAND_S:
            return
        self.detector.reset()  # nothing heard in flight counts
        self._best_heard = ("", -1.0)
        self._last_listen = self._last_scores_sent = now
        self._change(
            State.WAIT_BITE,
            "The bobber is in the water",
            f"Listening for a splash for up to {self.cfg.run.cast_timeout_s:g} s; "
            f"will press {self.cfg.game.interact_key} when I hear one",
        )

    def _wait_bite(self, now: float, bites: list[Any]) -> None:
        if bites:
            bite = bites[0]
            self._no_splash = 0
            self.stats = ev.Stats(
                self._t(), self.stats.casts, self.stats.catches, self.stats.misses,
                self.stats.bites_heard + 1, self._elapsed(),
            )  # fmt: skip
            self._emit(ev.BiteHeard(self._t(), bite.clip, float(bite.score)))
            if self.input.is_game_focused(self.window) is False:
                self._resume_state = State.WAIT_BITE
                self.state = State.PAUSED
                self._emit(ev.Paused(self._t(), UNFOCUSED))
                return
            key = self.cfg.game.interact_key
            self._press(key, "bite")
            self._t_interact = self._clock()
            self._last_look = -math.inf  # look again right away: is the bobber gone?
            self._change(
                State.INTERACT,
                f"Heard the splash ({bite.score:.2f}), pressed {key}",
                "Giving Auto Loot a moment",
            )
            return
        if now - self._t_cast > self.cfg.run.cast_timeout_s:
            self._emit(ev.Timeout(self._t(), "bite", self.cfg.run.cast_timeout_s, self._heard()))
            self._no_splash_miss("No splash heard")

    def _heard(self) -> str:
        clip, score = self._best_heard
        return f"best match {score:.2f} ({clip})" if clip else "nothing matched"

    def _no_splash_miss(self, did: str) -> None:
        self._no_splash += 1
        if self._no_splash == NO_SPLASH_WARN:
            self._emit(ev.Warning(self._t(), self._no_splash_hint()))
        self._miss(did)

    def _icon_check(self, now: float) -> bool:
        """End the cast on what the icon shows; True when it did."""
        if self.matcher is None or self.state not in (State.CASTING, State.WAIT_BITE):
            return False
        if self._icon_xy is None:
            if not self._icon_gate or now - self._t_cast <= self.cfg.run.icon_timeout_s:
                return False
            self._emit(ev.Timeout(self._t(), "icon", self.cfg.run.icon_timeout_s))
            self._icon_misses += 1
            if self._icon_misses == ICON_GIVE_UP:
                self._icon_gate = False
                self._emit(
                    ev.Warning(
                        self._t(),
                        f"No gear icon in {ICON_GIVE_UP} casts in a row, so I fish by sound "
                        "alone until I see it again; recalibrate on the Setup tab if this "
                        "keeps happening",
                    )
                )
            self._miss("No gear icon over the bobber")
            return True
        if (
            self.state is State.WAIT_BITE
            and self._unseen_since is not None
            and now - self._unseen_since >= GONE_S
        ):
            self._emit(ev.Timeout(self._t(), "gone", GONE_S, self._heard()))
            self._no_splash_miss("The bobber is gone without a splash I could hear")
            return True
        return False

    def _no_splash_hint(self) -> str:
        return (
            f"No splash heard in {self._no_splash} casts in a row: check that Sound Effects are "
            "on in the game, that nothing else covers the splash, and the bite threshold on the "
            "Setup tab (a missed bite scores just under it)"
        )

    def _after_interact(self, now: float) -> None:
        if self._icon_xy is not None and not self._icon_visible:  # it was there; now it is gone
            self._catch(confirmed=True)
            return
        if now - self._t_interact < self.cfg.run.loot_timeout_s:
            return
        if self._icon_xy is not None:  # still visible after the whole wait
            self._emit(ev.Timeout(self._t(), "loot", self.cfg.run.loot_timeout_s))
            self._miss("The bobber did not go away after Interact")
            return
        self._catch(confirmed=False)  # no icon to watch: trust the splash, as it has earned

    def _catch(self, *, confirmed: bool) -> None:
        self.stats = ev.Stats(
            self._t(), self.stats.casts, self.stats.catches + 1, self.stats.misses,
            self.stats.bites_heard, self._elapsed(),
        )  # fmt: skip
        self._emit(ev.Caught(self._t(), confirmed))
        did = (
            "Caught something; the bobber is gone"
            if confirmed
            else "Pressed Interact on the splash; Auto Loot should have it"
        )
        self._pause_then_idle(did)

    # -- vision (optional) -------------------------------------------------------------------

    def _look(self, now: float) -> None:
        """Keep `_icon_visible` and `_unseen_since` current; `_icon_check` acts on them.

        Until the icon is seen this cast: a full-frame match every LOOK_EVERY_S, up to
        icon_timeout_s after the cast. Once seen: a small box around it every box_interval_s,
        accepted down to KEEP_MARGIN below the threshold so a dip in score is not a loss.
        """
        if self.matcher is None:
            return
        if self._icon_xy is None:
            if self.state is State.INTERACT or now - self._t_cast > self.cfg.run.icon_timeout_s:
                return
            if now - self._last_look < LOOK_EVERY_S:
                return
            self._last_look = now
            frame = self.capturer.grab()
            m = self.matcher.match(frame)
            self._icon_visible = m.accepted(self.threshold)
            if self._icon_visible:
                self._icon_xy = (m.x, m.y)
                self._icon_misses = 0
                self._icon_gate = True
                thumb, box = _thumbnail(frame, m.x, m.y, m.size)
                where = (m.x, m.y)
                self._emit(ev.IconFound(self._t(), thumb, box, float(m.score), int(m.size), where))
            return
        if now - self._last_look < self.box_interval_s:
            return
        self._last_look = now
        x, y = self._icon_xy
        w, h = self.window.width, self.window.height
        x0, y0, x1, y1 = Region.around(x, y, w, h, 0.10).to_box(w, h)
        box: Box = (x0, y0, x1 - x0, y1 - y0)
        m = self.matcher.match(self.capturer.grab(box), full_width=w)
        self._icon_visible = m.accepted(self.threshold - KEEP_MARGIN)
        if self._icon_visible:
            self._icon_xy = (x0 + m.x, y0 + m.y)
            self._unseen_since = None
        elif self._unseen_since is None:
            self._unseen_since = now

    # -- helpers -----------------------------------------------------------------------------

    def _press(self, key: str, why: str) -> None:
        self.input.press_key(key, self.cfg.run.key_hold_ms / 1000.0)
        self._emit(ev.KeyPressed(self._t(), key, why))

    def _miss(self, did: str) -> None:
        self.stats = ev.Stats(
            self._t(), self.stats.casts, self.stats.catches, self.stats.misses + 1,
            self.stats.bites_heard, self._elapsed(),
        )  # fmt: skip
        self._pause_then_idle(did)

    def _pause_then_idle(self, did: str) -> None:
        pause = self._rng.uniform(self.cfg.run.pause_min_s, self.cfg.run.pause_max_s)
        self._change(State.IDLE, did, f"Casting again in {pause:.1f} s")
        remaining = pause
        while remaining > 0 and not self._stop:
            step = min(SLEEP_SLICE_S, remaining)
            self._sleep(step)
            remaining -= step

    def _drain_audio(self, now: float) -> list[AudioChunk]:
        rate = self.audio.sample_rate
        last = self._last_audio if self._last_audio is not None else now
        n = int((now - last) * rate)
        n = max(AUDIO_BLOCK, min(n, int(AUDIO_MAX_S * rate)))
        self._last_audio = now
        samples = self.audio.read(n)
        self._audio_frames += len(samples)
        return [AudioChunk(samples, rate, self._audio_frames / rate)]

    def _change(self, state: State, did: str, nxt: str) -> None:
        self.state = state
        self._emit(ev.StateChanged(self._t(), state, did, nxt))
        self._emit_stats()

    def _note_scores(self) -> None:
        best = getattr(self.detector, "best_heard", None)
        if best is not None and best.score > self._best_heard[1]:
            self._best_heard = (best.name, float(best.score))

    def _bite_threshold(self) -> float:
        return float(getattr(self.detector, "threshold", self.cfg.audio.bite_threshold))

    def _send_scores(self, now: float, *, flush: bool) -> None:
        take = getattr(self.detector, "take_peaks", None)
        if take is None or not (flush or now - self._last_scores_sent >= SCORES_EVERY_S):
            return
        self._last_scores_sent = now
        self._emit(ev.Scores(self._t(), take(), self._bite_threshold()))

    def _maybe_stats(self, now: float) -> None:
        if self.state is not State.WAIT_BITE:
            return
        if now - self._last_listen >= LISTEN_EVERY_S:
            self._last_listen = now
            clip, score = self._best_heard
            self._emit(ev.Listening(self._t(), clip, score, self._bite_threshold()))
        if now - self._last_stats >= STATS_EVERY_S:
            self._emit_stats()

    def _emit_stats(self) -> None:
        self._last_stats = self._clock()
        self.stats = ev.Stats(
            self._t(), self.stats.casts, self.stats.catches, self.stats.misses,
            self.stats.bites_heard, self._elapsed(),
        )  # fmt: skip
        self._emit(self.stats)

    def _finish(self, reason: str) -> None:
        if self.state is State.STOPPED:
            return
        self._emit_stats()
        self.state = State.STOPPED
        self._emit(ev.Stopped(self._t(), reason))

    def _emit(self, event: ev.Event) -> None:
        self.events.put(event)

    def _elapsed(self) -> float:
        return 0.0 if self.started is None else self._clock() - self.started

    def _t(self) -> float:
        return self._elapsed()


def _thumbnail(frame: np.ndarray, x: int, y: int, size: int) -> tuple[np.ndarray, Box]:
    h, w = frame.shape[:2]
    scale = min(1.0, THUMB_WIDTH / w)
    if scale < 1.0:
        thumb = cv2.resize(
            frame, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA
        )
    else:
        thumb = frame.copy()
    side = max(2, round(size * scale))
    bx = max(0, round(x * scale - side / 2))
    by = max(0, round(y * scale - side / 2))
    return thumb, (bx, by, side, side)


def unsupported_platform() -> str:
    """Why the bot cannot run here, or "" when it can (X11/Xwayland on Linux, or Windows)."""
    import os
    import sys

    if sys.platform == "win32" or linux_x11():
        return ""
    if sys.platform == "linux" and not os.environ.get("DISPLAY"):
        return "DISPLAY is not set; the bot needs X11 or Xwayland (log into a session with one)"
    return "the bot is not supported yet on this platform (Linux X11/Xwayland and Windows only)"


def build_engine(
    cfg: Config,
    *,
    session_s: float | None = None,
    title: str = "World of Warcraft",
    record_audio: Path | None = None,
) -> FishingEngine:
    """Wire the real capturer, input device, audio source, detector, and matcher for `cfg`.

    `record_audio` saves everything the engine hears to that WAV when it stops (for debugging
    missed splashes offline). Raises RuntimeError with a one-line hint when a prerequisite is
    missing.
    """
    from cedargrove.audio._recording import RecordingSource
    from cedargrove.audio.detector import detector_for
    from cedargrove.audio.devices import open_capture
    from cedargrove.input import get_input_device
    from cedargrove.vision.matcher import MIN_TEMPLATE_PX, IconMatcher
    from cedargrove.vision.template import load_icon_templates, template_for_size

    platform_problem = unsupported_platform()
    if platform_problem:
        raise RuntimeError(platform_problem)
    window = find_window(title)
    if window is None:
        raise RuntimeError(f"no window titled {title!r}; is the game running?")
    matcher = None  # no calibrated icon, or too small to match here: fish by ear alone
    size = cfg.vision.icon_size_px
    if size > 0:
        candidate = IconMatcher(
            template_for_size(load_icon_templates(), size), cfg.vision.match_width
        )
        if candidate.template_px(window.width) >= MIN_TEMPLATE_PX:
            matcher = candidate

    detector = detector_for(cfg.audio)
    audio: Any = ThreadedSource(open_capture(cfg.audio.selector, pid=window.pid, title=title))
    if record_audio is not None:
        audio = RecordingSource(audio, record_audio)
    return FishingEngine(
        cfg,
        window,
        capturer=get_capturer(),
        input_device=get_input_device(),
        audio_source=audio,
        detector=detector,
        matcher=matcher,
        find_window=lambda t=title: find_window(t),
        session_s=session_s,
    )
