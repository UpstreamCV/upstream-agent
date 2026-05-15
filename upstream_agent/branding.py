"""Welcome banner shown once on first CLI invocation.

The banner is gated by a sentinel file at ``~/.upstream/.welcomed``. The
first time any ``upstream`` subcommand runs, the logo prints and the
sentinel is created; subsequent invocations skip it.

Failure to create the sentinel is silently swallowed so a read-only or
otherwise broken ``~/.upstream`` directory cannot break the CLI.
"""

from __future__ import annotations

from pathlib import Path

import typer

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
