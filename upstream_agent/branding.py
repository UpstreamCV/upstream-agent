"""UpStream branding: logo + the brand palette.

Two consumers:

* ``maybe_print_welcome`` — plain logo shown once on the first
  *subcommand* invocation, gated by a sentinel file at
  ``~/.upstream/.welcomed``. Failure to create the sentinel is silently
  swallowed so a read-only ``~/.upstream`` cannot break the CLI.
* The interactive shell — prints :func:`logo_text` (the same logo in
  brand green) on **every** launch and styles its whole UI with the
  palette below.

Palette (truecolor hex; Rich renders these exactly in any modern
terminal and degrades gracefully elsewhere):

* ``BRAND_GREEN`` — the main color: logo, borders, headings, prompt
  accents, healthy states.
* ``BRAND_BLUE``  — informational text, labels, secondary values.
* ``BRAND_GOLD``  — highlights, hints, in-between states (starting /
  reconnecting / unassigned). Errors stay red — semantic, not brand.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.text import Text

BRAND_GREEN = "#344C2C"
BRAND_BLUE = "#4B816C"
BRAND_GOLD = "#81764B"

LOGO = r"""
       $$$$$$$$$$$$$$$$$$$$$$$$$$
      $                         $$
     $$                   $$$$   $
     $$              $$$$$$$$$   $
     $$          $$$$$$$$$$$$$   $
     $$     $$$$$$$$$$$$$$$$     $
     $$    $$$$$$$$$$$$$$$$      $
     $$    $$$$$$$$$$$$$$        $
     $$   $$$$$$$$$$$$$          $
     $$   $$$$$$$$$$             $
     $$  $$$$$$$$$$              $
     $$  $$$$$$$                 $
     $$  $$$$$                   $
     $$  $$$$$                   $
     $$  $$$$$ $$$$$             $
     $$     $$$$$$               $
     $$     $$$$$                $
     $$      $$$                 $
     $$       $$                 $
      $$                        $
        $$$$$$$$$$$$$$$$$$$$$$$

  _    _           _
 | |  | |         | |
 | |  | |_ __  ___| |_ _ __ ___  __ _ _ __ ___
 | |  | | '_ \/ __| __| '__/ _ \/ _` | '_ ` _ \
 | |__| | |_) \__ \ |_| | |  __/ (_| | | | | | |
  \____/| .__/|___/\__|_|  \___|\__,_|_| |_| |_|
        | |
        |_|
"""


def logo_text() -> Text:
    """The logo as a Rich renderable in brand green.

    The wordmark block at the bottom picks up bold for a little extra
    weight; the mark itself stays plain green so it reads as one shape.
    """

    text = Text()
    mark, _, wordmark = LOGO.partition("\n\n  _")
    text.append(mark, style=BRAND_GREEN)
    if wordmark:
        text.append("\n\n  _", style=f"bold {BRAND_GREEN}")
        text.append(wordmark, style=f"bold {BRAND_GREEN}")
    return text


def _welcome_sentinel_path() -> Path:
    return Path.home() / ".upstream" / ".welcomed"


def maybe_print_welcome() -> None:
    sentinel = _welcome_sentinel_path()
    if sentinel.exists():
        return
    typer.echo(LOGO)
    try:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
    except OSError:
        pass
