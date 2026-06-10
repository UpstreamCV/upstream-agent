"""Interactive terminal mode for the ``upstream`` command.

``upstream`` with no subcommand opens :class:`~upstream_agent.interactive.shell.InteractiveShell`,
a REPL that talks to the API with the configured token: account status,
limits, cameras (pulled from the server), stream start/stop (background
:class:`~upstream_agent.interactive.worker.StreamWorker` threads wrapping
the existing supervisor), and an on-demand log view backed by
:class:`~upstream_agent.logging_setup.RingBufferHandler`.
"""
