"""Platform layer: the pieces that turn ams into the runtime for the `api` fleet.

Everything here sits *above* the supervisor core and is optional to it: the
supervisor never imports this package. See `.claude/state/PLAN-allin.md`.
"""
