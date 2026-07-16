"""Built-in workflow library (IC-905).

Ships the shipped-with-IronCore workflow YAMLs (``review`` / ``migrate`` /
``explain-repo``) as package data so they are discoverable in an installed wheel,
not just from a source checkout. The files are declarative YAML — nothing here is
imported; :func:`ironcore.workflows.schema.discover_workflows` reads this directory
and keys each workflow by its filename stem.

This ``__init__`` exists solely to make ``builtin`` a real subpackage so hatchling
includes the ``*.yaml`` files in the wheel. Resolve the directory at runtime with
``importlib.resources.files("ironcore.workflows.builtin")`` (works frozen and from
source alike).
"""

from __future__ import annotations
