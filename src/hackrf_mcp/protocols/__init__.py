"""Protocols: one module per decoder family.

Each module registers what it provides when it is imported:

- an offline decoder for hackrf_decode, with decoders.register(Decoder(...)),
  whose run(job) turns a capture into a decoders.Result;
- a live profile for hackrf_live_start, with
  live_profiles.register(Profile(...)), whose pipeline class (a
  live_profiles.Pipeline) turns the HackRF's stream into log rows;

or both. Importing this package imports every module in it: the built-in
ones below in a fixed order (so the tools list them consistently), then any
other module dropped into this directory, alphabetically. Adding a decoder
is adding a module here; the tools' parameter choices and descriptions,
hackrf_server_status and hackrf_live_summary pick it up from the
registries. See the README's "Adding a decoder".
"""

from __future__ import annotations

import importlib
import pkgutil

BUILTIN = ("adsb", "uat", "ais", "acars", "vdl2", "multimon", "rds")


def _load() -> None:
    for name in BUILTIN:
        importlib.import_module(f"{__name__}.{name}")
    for module in pkgutil.iter_modules(__path__):
        if module.name not in BUILTIN and not module.name.startswith("_"):
            importlib.import_module(f"{__name__}.{module.name}")


_load()
