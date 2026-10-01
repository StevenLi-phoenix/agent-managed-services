"""Platform layer: core mode, the runtime for the `api` core (Cordis).

Everything here sits *above* the supervisor core and is optional to it: no core
module imports this package at import time, and with it deleted ``ams`` is a
complete supervisor without the ``platform`` subcommand
(``tests/test_core_boundary.py``). See ``docs/platform-core.md``.
"""
