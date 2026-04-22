"""Public orchestration behavior surface backed by the legacy workflow engine."""

from .binding import build_orchestration_discord_binding
from .kernel_binding import build_orchestration_kernel_binding
from .policy import PolicyService
from .recovery import RecoveryService
from .service import SessionService
from .verification import VerificationService

BEHAVIOR_ID = "orchestration"

__all__ = [
    "BEHAVIOR_ID",
    "PolicyService",
    "RecoveryService",
    "SessionService",
    "VerificationService",
    "build_orchestration_discord_binding",
    "build_orchestration_kernel_binding",
]
