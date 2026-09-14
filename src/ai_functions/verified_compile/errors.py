"""Errors raised by the Lean-verified compilation pipeline."""

from __future__ import annotations


class VerifiedCompileError(RuntimeError):
    """Base error for ``@ai_verified_compile``."""


class VerifiedCompileConfigurationError(VerifiedCompileError):
    """The decorated function or compiler configuration is unsupported."""


class PythonContractError(VerifiedCompileConfigurationError):
    """A Python contract uses syntax or semantics outside the verified subset."""


class PythonContractViolation(VerifiedCompileError):
    """The native result failed its original Python contract at runtime."""


class LeanSetupError(VerifiedCompileError):
    """Lean or its Lake environment could not be prepared."""


class LeanVerificationError(VerifiedCompileError):
    """Generated Lean source did not pass verification."""


class LeanCompilationError(VerifiedCompileError):
    """Verified Lean source could not be compiled to a shared library."""


class LeanFFIError(VerifiedCompileError):
    """Python could not load or call the compiled Lean function."""
