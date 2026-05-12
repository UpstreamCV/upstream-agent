"""Local diagnostic routines for upstream-agent.

Diagnostics live here so the CLI ``check`` command can stay a thin
translator from result objects to stdout/stderr. Each diagnostic is a
pure function that accepts the relevant config slice and returns a
structured result; none of them call the API or touch the network.
"""
