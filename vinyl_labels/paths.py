"""Stable filesystem locations shared across package modules and commands."""

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"


def project_path(path):
    """Resolve a configured path relative to the project, unless absolute."""
    configured = Path(path).expanduser()
    return configured if configured.is_absolute() else PROJECT_ROOT / configured
