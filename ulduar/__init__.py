"""Project Ulduar: a vault and observatory for data projects.

The components (Freya, Hodir, Thorim, Algalon, Mimiron) arrive phase by phase;
see ROADMAP.md. Until then the package only states its version.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("project-ulduar")
except PackageNotFoundError:  # a bare checkout that was never installed
    __version__ = "0+unknown"
