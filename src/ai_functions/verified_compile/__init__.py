"""Lean-verified native compilation for AI Functions."""

from ._decorator import AIVerifiedFunction, ai_verified_compile
from ._source import LeanSpec
from .config import VerifiedCompileConfig, VerifiedCompileKwargs
from .errors import (
    LeanCompilationError,
    LeanFFIError,
    LeanSetupError,
    LeanVerificationError,
    VerifiedCompileConfigurationError,
    VerifiedCompileError,
)

__all__ = [
    "ai_verified_compile",
    "AIVerifiedFunction",
    "LeanCompilationError",
    "LeanFFIError",
    "LeanSetupError",
    "LeanSpec",
    "LeanVerificationError",
    "VerifiedCompileConfig",
    "VerifiedCompileConfigurationError",
    "VerifiedCompileError",
    "VerifiedCompileKwargs",
]
