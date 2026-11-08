"""Six checks that protect what the bot actually relies on: the shipped assets, the default
config, logging in the console-less GUI executable, and that the two detectors say yes to the
real thing and no to nothing.

Everything here runs on bundled files and synthetic signals. No screen, audio device, window
system, or Tk is touched, so a failure means the code or the assets changed, not the machine.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
from loguru import logger

from cedargrove.assets import CLIP_NAMES, ICON_SIZES
from cedargrove.audio import AudioChunk
from cedargrove.audio.clips import build_fingerprints, load_reference_clips
from cedargrove.audio.detector import BiteDetector, BiteEvent, detector_for
from cedargrove.audio.features import DEFAULT_MEL
from cedargrove.cli import _setup_logging
from cedargrove.config import (
    AudioConfig,
    Config,
    GameConfig,
    VisionConfig,
    ensure_config_file,
    load_config,
    save_config,
)
from cedargrove.engine import events as ev
from cedargrove.vision.matcher import IconMatcher
from cedargrove.vision.template import load_icon_templates, template_for_size

REPO_ROOT = Path(__file__).resolve().parents[1]
RATE = DEFAULT_MEL.sample_rate


def test_bundled_assets_load():
    """Every sprite and clip the code expects is shipped and readable; the release check agrees."""
    check = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_release.py")],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stderr
    assert tuple(t.native_size for t in load_icon_templates()) == ICON_SIZES
    assert tuple(fp.name for fp in build_fingerprints()) == CLIP_NAMES


def test_default_config_matches_code_defaults(tmp_path):
    """The shipped default_config file and the dataclass defaults describe the same settings,
    and a config survives a save/load round trip unchanged."""
    path = tmp_path / "config.toml"
    assert ensure_config_file(path) is True
    assert ensure_config_file(path) is False  # never overwrites an existing file

    from_file, warnings = load_config(path)
    assert warnings == []
    assert from_file == Config(game=GameConfig(), vision=VisionConfig(), audio=AudioConfig())

    save_config(from_file, path)
    assert load_config(path) == (from_file, [])


def test_logging_works_without_stderr(tmp_path, monkeypatch):
    """cedargrove-gui.exe is built without a console, so on Windows Python sets sys.stderr to None.
    Logging setup must not crash there, and the log file must still be written."""
    monkeypatch.setenv("CEDARGROVE_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "stderr", None)
    try:
        log_path = _setup_logging()
        logger.info("started without a console")
    finally:
        logger.remove()  # closes the file sink so tmp_path can be deleted on Windows
    assert "started without a console" in log_path.read_text()


def _hear(stream: np.ndarray) -> tuple[list[BiteEvent], BiteDetector]:
    """Run a stream through the detector the bot builds from the default config, in modest
    chunks like the live loop does."""
    det = detector_for(AudioConfig())
    events: list[BiteEvent] = []
    chunk = 2048
    for i in range(0, len(stream), chunk):
        part = stream[i : i + chunk]
        events += det.feed(AudioChunk(part, RATE, (i + len(part)) / RATE))
    return events, det


def test_bite_detector_hears_each_clip_once():
    """Each of the four bundled splashes, padded with silence, yields exactly one bite naming
    that clip, above the shipped threshold, timed inside the clip, and the "best match" the
    status line reports and the peak the Scores graph plots are that same peak. Two splashes
    well apart yield two."""
    threshold = AudioConfig().bite_threshold
    gap = np.zeros(RATE, dtype=np.float32)  # 1 s
    for name, clip in load_reference_clips(RATE).items():
        events, det = _hear(np.concatenate([gap, clip, gap]))
        assert [e.clip for e in events] == [name], name
        assert events[0].score >= threshold, (name, events[0].score)
        assert 1.0 - 0.1 <= events[0].t <= 1.0 + len(clip) / RATE, (name, events[0].t)
        best = det.best_heard
        assert (best.name, best.score) == (name, events[0].score), (name, best)
        assert det.take_peaks()[name] == events[0].score, name  # what the Scores graph plots
        assert det.take_peaks()[name] == -1.0, name  # taking the peaks clears them

    clip = load_reference_clips(RATE)[CLIP_NAMES[0]]
    apart = np.zeros(4 * RATE, dtype=np.float32)  # longer than the default refractory period
    assert len(_hear(np.concatenate([gap, clip, apart, clip, gap]))[0]) == 2


def test_bite_detector_stays_quiet_on_silence_and_noise():
    """Nothing fires on silence or broadband noise, and the scores stay well under threshold."""
    threshold = AudioConfig().bite_threshold
    rng = np.random.default_rng(0)
    silence = np.zeros(3 * RATE, dtype=np.float32)
    noise = (rng.standard_normal(3 * RATE) * 0.1).astype(np.float32)
    for stream in (silence, noise):
        det = detector_for(AudioConfig())
        events: list[BiteEvent] = []
        for i in range(0, len(stream), 2048):
            part = stream[i : i + 2048]
            events += det.feed(AudioChunk(part, RATE, (i + len(part)) / RATE))
        assert events == []
        assert max(s.score for s in det.last_scores) < threshold - 0.2


def test_icon_matcher_finds_sprite_and_rejects_water():
    """Every sprite pasted onto noisy blue water is found at the right spot, at 1080p and on a
    4K-wide frame that the matcher downscales. The same water without a sprite is rejected."""
    threshold = VisionConfig().icon_threshold
    templates = load_icon_templates()
    rng = np.random.default_rng(0)
    for frame_w, frame_h in ((1920, 1080), (3840, 2160)):
        water = np.full((frame_h, frame_w, 3), (90, 60, 30), np.uint8)  # BGR: bluish
        water += rng.integers(0, 20, water.shape, dtype=np.uint8)
        for size in ICON_SIZES:
            t = template_for_size(templates, size)
            x, y = frame_w // 3, frame_h // 3
            frame = water.copy()
            frame[y : y + size, x : x + size][t.mask > 0] = t.bgr[t.mask > 0]

            hit = IconMatcher(t).match(frame)
            assert hit.accepted(threshold), (frame_w, size, hit)
            assert abs(hit.x - (x + size / 2)) <= 2 and abs(hit.y - (y + size / 2)) <= 2

            miss = IconMatcher(t).match(water)
            assert not miss.accepted(threshold), (frame_w, size, miss)


class _World:
    """A fake game on a fake clock. Times are seconds after the latest cast. The splash clips
    play at `sounds_at` (the bot must ignore one while the bobber is still landing); the gear
    icon is on screen while `icon(t_since_cast, interacted)` says so."""

    def __init__(self, *, sounds_at=(), icon=None, focused=None):
        self.t = 0.0
        self.cast_at = -1e9
        self.interacted = False
        self.sounds_at = list(sounds_at)
        self.icon = icon or (lambda t, interacted: False)
        self.focused = focused or (lambda t: True)
        self.presses: list[tuple[float, str]] = []

    def since_cast(self) -> float:
        return self.t - self.cast_at


def _engine(world: _World, *, with_matcher: bool = True):
    from cedargrove.capture import WindowRef
    from cedargrove.engine.engine import FishingEngine
    from cedargrove.vision.matcher import IconMatch

    def sleep(s: float) -> None:
        world.t += s

    class Capturer:
        def open(self, w): ...
        def close(self): ...

        def grab(self, box=None):
            on = world.icon(world.since_cast(), world.interacted)
            shape = (1440, 2560, 3) if box is None else (box[3], box[2], 3)
            return np.full(shape, 1 if on else 0, np.uint8)

    class Matcher:
        def match(self, frame, region=None, *, full_width=None):
            world.t += 0.03  # a look takes time
            if frame[0, 0, 0]:
                return IconMatch(0.9, 50, 50, 42, 0.3)
            return IconMatch(0.6, 50, 50, 42, 0.0)

    class Input:
        def is_game_focused(self, w):
            return world.focused(world.t)

        def press_key(self, key, hold):
            world.presses.append((world.t, key))
            if key == "1":
                world.cast_at, world.interacted = world.t, False
            else:
                world.interacted = True

    class Audio:
        sample_rate = 16000

        def read(self, n):
            return np.zeros(n, np.float32)

    class Detector:
        """Hears a clip if it was fed audio covering a moment in `sounds_at`."""

        def __init__(self):
            self.heard_to = -1e9

        def reset(self):
            self.heard_to = world.since_cast()

        def feed(self, chunk):
            now = world.since_cast()
            hits = [s for s in world.sounds_at if self.heard_to < s <= now]
            self.heard_to = now
            return [BiteEvent("fishingbobber_1", 0.9, s) for s in hits]

    cfg = Config(
        game=GameConfig(cast_key="1", interact_key="f"), vision=VisionConfig(), audio=AudioConfig()
    )
    return FishingEngine(
        cfg,
        WindowRef("World of Warcraft", 1, 0, 0, 2560, 1440, 1),
        capturer=Capturer(),
        input_device=Input(),
        audio_source=Audio(),
        detector=Detector(),
        matcher=Matcher() if with_matcher else None,
        clock=lambda: world.t,
        sleep=sleep,
    )


def _run_until(engine, done, limit=20000) -> list:
    seen: list = []
    for _ in range(limit):
        engine.step()
        while not engine.events.empty():
            seen.append(engine.events.get())
        if done(seen):
            return seen
    raise AssertionError([type(e).__name__ for e in seen][-30:])


def test_fishes_by_sound_alone_and_ignores_the_landing_splash():
    # No calibrated icon at all. The bobber's landing at 0.5 s sounds like a splash; the bite is
    # at 6 s. Exactly one Interact, after the bite, then the next cast.
    world = _World(sounds_at=[0.5, 6.0])
    engine = _engine(world, with_matcher=False)
    seen = _run_until(engine, lambda s: sum(isinstance(e, ev.Caught) for e in s) >= 1)
    bites = [e for e in seen if isinstance(e, ev.BiteHeard)]
    interacts = [p for p in world.presses if p[1] == "f"]
    assert len(bites) == 1 and len(interacts) == 1
    assert interacts[0][0] - world.presses[0][0] >= 6.0
    caught = next(e for e in seen if isinstance(e, ev.Caught))
    assert caught.confirmed is False
    assert not any(isinstance(e, ev.Timeout) for e in seen)


def test_an_icon_never_matched_does_not_end_the_cast():
    # This is what lost half the casts: the gear never matched, so the bot recast after 4 s
    # even though the fish would bite at 12 s.
    world = _World(sounds_at=[12.0], icon=lambda t, interacted: False)
    engine = _engine(world)
    seen = _run_until(engine, lambda s: any(isinstance(e, ev.Caught) for e in s))
    assert [p[1] for p in world.presses] == ["1", "f"]
    assert not any(isinstance(e, ev.Timeout) for e in seen)


def test_the_icon_vanishing_confirms_the_catch_early():
    world = _World(sounds_at=[5.0], icon=lambda t, interacted: t > 1.0 and not interacted)
    engine = _engine(world)
    seen = _run_until(engine, lambda s: any(isinstance(e, ev.Caught) for e in s))
    assert any(isinstance(e, ev.IconFound) for e in seen)
    caught = next(e for e in seen if isinstance(e, ev.Caught))
    bite = next(e for e in seen if isinstance(e, ev.BiteHeard))
    assert caught.confirmed is True
    assert caught.t - bite.t < engine.cfg.run.loot_timeout_s


def test_no_splash_three_casts_in_a_row_says_what_to_check():
    world = _World(sounds_at=[])
    engine = _engine(world)
    seen = _run_until(engine, lambda s: sum(isinstance(e, ev.Timeout) for e in s) >= 4)
    warnings = [e for e in seen if isinstance(e, ev.Warning)]
    assert len(warnings) == 1 and "3 casts" in warnings[0].message
    assert "gear icon was not seen" in warnings[0].message


def test_pauses_while_the_game_is_not_focused():
    world = _World(sounds_at=[6.0], focused=lambda t: not (2.0 < t < 4.0))
    engine = _engine(world, with_matcher=False)
    seen = _run_until(engine, lambda s: any(isinstance(e, ev.Caught) for e in s))
    names = [type(e).__name__ for e in seen]
    assert "Paused" in names and "Resumed" in names
    assert names.index("Paused") < names.index("Resumed") < names.index("BiteHeard")


def test_waits_a_moment_after_resuming_before_casting():
    # Start with the terminal focused; the player clicks into the game at 2 s. A key sent in
    # that instant can be lost, so the first cast waits a second.
    world = _World(focused=lambda t: t >= 2.0)
    engine = _engine(world, with_matcher=False)
    _run_until(engine, lambda s: bool(world.presses))
    assert world.presses[0][0] >= 3.0


def test_waits_for_a_bobber_still_out_before_casting():
    # A bobber cast by hand before Start is still out until 3 s. With cast = Interact (as in
    # Dahai's setup) pressing now would only reel it in, so the bot waits for it to go.
    world = _World(sounds_at=[6.0])
    world.cast_at = 0.0
    world.icon = lambda t, interacted: world.t < 3.0 or (t > 1.0 and not interacted)
    engine = _engine(world)
    seen = _run_until(engine, lambda s: bool(world.presses))
    assert world.presses[0][0] >= 3.0
    assert any(isinstance(e, ev.StateChanged) and "still in the water" in e.did for e in seen)
