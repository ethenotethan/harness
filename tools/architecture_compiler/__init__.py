"""The architecture compiler: one core, one language pack per language, one contract.

    python3 -m tools.architecture_compiler <service-root> [--check]

Reads ``<root>/architecture/config.json`` (the same shape the Python ``ast``
compiler uses, plus ``external_systems``, ``external_groups``, ``rules`` and
``languages``), runs every source file through the pack for its extension
(Swift, TypeScript/JavaScript, Go, Rust, Python), and writes a document that
conforms to the ``hermes.architecture`` contract or refuses to write at all.
"""
from .core import CHECK_COMMAND, COMPILER_ID, compile_service, main, serialized  # noqa: F401

__all__ = ["CHECK_COMMAND", "COMPILER_ID", "compile_service", "main", "serialized"]
