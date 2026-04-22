"""Default workflow domain bundle backed by the sample project profile."""

from pathlib import Path

DOMAIN_ROOT = Path(__file__).resolve().parents[2] / "projects" / "sample"
PROJECT_FILE = DOMAIN_ROOT / "project.yaml"
PROMPTS_DIR = DOMAIN_ROOT / "prompts"

__all__ = ["DOMAIN_ROOT", "PROJECT_FILE", "PROMPTS_DIR"]
