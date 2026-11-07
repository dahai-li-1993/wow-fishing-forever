"""cedargrove: a vision-based fishing bot for World of Warcraft: Forever."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("cedargrove")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0+unknown"
