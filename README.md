<div align="center">

<img src="/assets/cover-photo.jpg" alt="AI-generated photo of a bot fishing" width="360" />
<h1>Cedargrove</h1>

<p><strong>A fishing bot for WoW</strong></p>


<p>
  <a href="https://github.com/rsamf/wow-fishing-forever/actions/workflows/ci.yml"><img src="https://github.com/rsamf/wow-fishing-forever/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="https://github.com/rsamf/wow-fishing-forever/blob/main/LICENSE"><img src="https://img.shields.io/github/license/rsamf/wow-fishing-forever.svg" alt="License: MIT" /></a>
  <img src="https://img.shields.io/endpoint?url=https://gittokens.rsamf.com/badge/rsamf/wow-fishing-forever" alt="tokens" />
</p>

<p>
  <a href="#install"><b>Install</b></a>
  &nbsp;·&nbsp;
  <a href="#go-fish"><b>Go Fish</b></a>
  &nbsp;·&nbsp;
  <a href="#commands"><b>Commands</b></a>
  &nbsp;·&nbsp;
  <a href="#how-it-works"><b>How It Works</b></a>
  &nbsp;·&nbsp;
  <a href="#license"><b>License</b></a>
</p>
</div>

Cedargrove is a local rename of [WoW Fishing Forever](https://github.com/rsamf/wow-fishing-forever),
a fishing bot for **World of Warcraft: Forever**, successor to the 2019
[wow-fishing-bot](https://github.com/rsamf/wow-fishing-forever/tree/60d0710), now with many improvements, so you can now fish *forever*. It listens to
the game's own splash sound for the bite and presses your Interact key. Watching the window for
the built-in interact icon over your bobber is optional and never ends a cast.

> [!NOTE]
> 🎉 **Announcement** This was my very first Python project but, surprisingly, my most popular repo on GitHub. 
> When I originally released this for WoW Classic, I had no idea it would get this much attention. I left this
> project unmaintained for 7 years, but I felt it was time for a return when I saw WoW Forever coming. I have 
> come back to make many major improvements to the bot and its accessibility. The original bot was only suited 
> for programmers, but now, I've designed it to be accessible to anyone by releasing GUI executables. Enjoy but 
> understand the risk you're taking!


## Install

### Build executables

Build the renamed app from this checkout:

    uv sync --group dev
    uv run pyinstaller --clean --noconfirm packaging/cedargrove.spec

On Windows this creates `dist/cedargrove.exe` (terminal) and
`dist/cedargrove-gui.exe` (GUI). On macOS and Linux the same names have no `.exe` suffix.
These executables bundle Python and the app's dependencies.

The [upstream releases](https://github.com/rsamf/wow-fishing-forever/releases/latest)
use the original name. Build this checkout to get the renamed executables.

### Run from source

From this renamed checkout:

    uv sync --group dev
    uv run cedargrove doctor

The GUI also runs directly from Python:

    uv run python -m cedargrove gui

Running from source and running a bundled executable use the same fishing logic.
Renaming the app or running it from Python does not establish protection against bans.

### Additional step for Mac users

Mac users need to install a virtual loopback device such as [BlackHole](https://existential.audio/blackhole/), route the game's audio through it, and pick it on the Setup tab (this sets `audio.capture = "device"`).

## Go Fish

> [!CAUTION]
> **Use at your own risk.** Automating gameplay violates Blizzard's terms of service and can
> get your account banned. This project exists for educational purposes only.

In the game:

1. *Options > Gameplay > Controls*: enable **Auto Loot**.
2. *Options > Keybindings > Targeting*: bind **Interact With Target**. cedargrove assumes `f`.
3. Put **Fishing** on an action bar key. cedargrove assumes `1`.
4. Type `/console SoftTargetIconGameObject 1` once. This shows the gold gear icon over your
   bobber whenever it is close enough to interact with.
5. *Options > Sound*: set **Ambience** and **Music** to 0 and keep **Sound Effects** up.


**Note to Linux users**: The first synthetic key press opens a "Remote Desktop" dialog. Switch on *Allow Remote Interaction* and click *Share*. Until you do, key presses are silently dropped.

**Note to Windows users**: You may need to run the program in admin mode.

Stand at the water and run the GUI:

    cedargrove gui          # or double-click cedargrove-gui

Or run it from terminal:

    cedargrove doctor                      # checks the compatibility with this machine
    cedargrove config                      # creates the config file; edit cast_key / interact_key
    cedargrove run                         # fishes by sound; Ctrl-C stops

While the bot is running, keep the water in view and the game client in focus.

## Commands

| command | what it does |
|---|---|
| `cedargrove gui` | The setup and fishing window |
| `cedargrove run [--minutes N] [--record-audio WAV]` | Fish from the terminal until Ctrl-C or the session limit; optionally save everything heard |
| `cedargrove doctor` | Check OS, display, OpenCV, audio devices, game window, input, calibration, config |
| `cedargrove calibrate SHOT.png [--save]` | Measure the interact icon's on-screen size from a screenshot |
| `cedargrove watch --live` | Show what the matcher sees: phase, score, position, milliseconds per frame |
| `cedargrove watch --from DIR` | Same, over saved frames; `--save-frames` writes annotated thumbnails |
| `cedargrove listen [--list] [--device D] [--record WAV]` | Meter the game's audio (or loopback device D), print bites as they are heard |
| `cedargrove verify-bite WAV [--save]` | Score a recording against every splash clip and pick the bite |
| `cedargrove config` | Create the config file if needed and print the resolved settings |
| `cedargrove config --path` | Print only the config file location |
| `cedargrove --version` | Print the version |

Add `--verbose` before any command for debug logging. `doctor` prints the log file path.

## How It Works

This bot looks at pixels and listens to sound, and it presses the keys to cast and loot. It does not read memory, inject code, inject network traffic, or rely on an addon.

1. **Cast**: It casts using your specified cast key. Each key press is simulated with a `key_hold_ms` timer.
2. **Detect bite**: 1.5 seconds after the cast (once the bobber has landed) the bot starts listening. WoW plays one of its splash clips when a fish bites. The bot fingerprints the first 0.8 seconds of each as a log-mel spectrogram with the gain removed. If there's a match, the bot will simulate the interact key, and auto-loot should pick up your fish. After all that, the bot recasts. If no splash comes within `cast_timeout_s` it recasts, and after 3 such casts in a row it says what to check.
3. **Watch the bobber (optional)**: WoW draws a gold gear sprite over the bobber when it is in interact range. If the icon is calibrated, the bot looks for it with masked OpenCV template matching to show the bobber in the GUI and to see it vanish after Interact, so it can cast again sooner. Vision never ends a cast: sound decides. (Earlier versions recast whenever the icon was not matched, which threw away about half the casts in real sessions while every splash heard became a catch.)

## License

MIT. See [LICENSE](LICENSE).
