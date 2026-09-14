"""Lean/Lake environment management and native compilation."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ._ffi import generate_c_shim
from ._locking import exclusive_file_lock
from ._source import RenderedLeanSource
from ._types import FunctionShape
from .config import VerifiedCompileConfig
from .errors import LeanCompilationError, LeanSetupError, LeanVerificationError

logger = logging.getLogger(__name__)


def shared_library_suffix() -> str:
    """Return the native shared-library suffix for supported hosts."""
    if sys.platform == "darwin":
        return ".dylib"
    if sys.platform.startswith("linux"):
        return ".so"
    raise LeanSetupError(f"@ai_verified_compile currently supports macOS and Linux, not {sys.platform!r}")


def _environment_key(config: VerifiedCompileConfig) -> str:
    payload = f"{config.lean_toolchain}\0{config.mathlib_revision or 'core-only'}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class LeanProject:
    """A cached Lake project used to check and compile generated modules."""

    def __init__(self, config: VerifiedCompileConfig) -> None:
        self.config = config
        self.root = Path(config.cache_dir).expanduser().resolve() / "environments" / _environment_key(config)

    def _run(
        self,
        command: list[str],
        *,
        cwd: Path,
        timeout: float,
        error_type: type[LeanSetupError | LeanVerificationError | LeanCompilationError],
        action: str,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        logger.debug("verified compile: %s", " ".join(command))
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env={**os.environ, **(extra_env or {})},
            )
        except FileNotFoundError as exc:
            raise error_type(
                f"{command[0]!r} was not found while {action}. "
                "Install Lean with elan and ensure its shims are on PATH.",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise error_type(f"Timed out after {timeout:g}s while {action}") from exc

        if result.returncode != 0:
            output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
            raise error_type(f"Failed while {action}:\n{output or '(no diagnostic output)'}")
        return result

    def ensure(self) -> Path:
        """Create the pinned Lake project and install Mathlib once."""
        marker = self.root / ".setup-complete"
        if marker.exists():
            return self.root

        lock_path = self.root.parent / ".locks" / f"{self.root.name}.lock"
        with exclusive_file_lock(lock_path):
            if marker.exists():
                return self.root

            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / "lean-toolchain").write_text(f"{self.config.lean_toolchain}\n")
            if self.config.mathlib_revision is None:
                lakefile = """\
import Lake
open Lake DSL

package aiVerifiedCompile where
"""
            else:
                lakefile = f"""\
import Lake
open Lake DSL

package aiVerifiedCompile where

require mathlib from git
  "https://github.com/leanprover-community/mathlib4" @ "{self.config.mathlib_revision}"
"""
            (self.root / "lakefile.lean").write_text(lakefile)

            self._run(
                ["lake", "--version"],
                cwd=self.root,
                timeout=self.config.setup_timeout_seconds,
                error_type=LeanSetupError,
                action="checking the Lean toolchain",
            )
            if self.config.mathlib_revision is not None:
                self._run(
                    ["lake", "update"],
                    cwd=self.root,
                    timeout=self.config.setup_timeout_seconds,
                    error_type=LeanSetupError,
                    action="resolving Mathlib",
                )
                # Lake may adopt the dependency's toolchain file. Keep the
                # explicitly configured compiler authoritative.
                (self.root / "lean-toolchain").write_text(f"{self.config.lean_toolchain}\n")
                self._run(
                    ["lake", "exe", "cache", "get"],
                    cwd=self.root,
                    timeout=self.config.setup_timeout_seconds,
                    error_type=LeanSetupError,
                    action="downloading the Mathlib build cache",
                )
            marker.write_text("ok\n")
        return self.root

    def _write_modules(self, directory: Path, source: RenderedLeanSource) -> tuple[Path, Path]:
        implementation_path = directory / f"{source.module_name}.lean"
        proof_path = directory / f"{source.proof_module_name}.lean"
        implementation_path.write_text(source.implementation)
        proof_path.write_text(source.verification)
        return implementation_path, proof_path

    def _module_environment(self, directory: Path) -> dict[str, str]:
        lean_path = str(directory)
        inherited = os.environ.get("LEAN_PATH")
        if inherited:
            lean_path = os.pathsep.join((lean_path, inherited))
        return {"LEAN_PATH": lean_path}

    def _check_and_codegen(
        self,
        work: Path,
        source: RenderedLeanSource,
    ) -> tuple[Path, Path, Path, Path, Path]:
        implementation, proof = self._write_modules(work, source)
        olean_path = work / f"{source.module_name}.olean"
        proof_olean_path = work / f"{source.proof_module_name}.olean"
        c_path = work / "implementation.c"
        module_env = self._module_environment(work)
        self._run(
            [
                "lake",
                "env",
                "lean",
                "-o",
                olean_path.name,
                f"--c={c_path}",
                implementation.name,
            ],
            cwd=work,
            timeout=self.config.command_timeout_seconds,
            error_type=LeanVerificationError,
            action="checking and compiling the generated Lean implementation",
            extra_env=module_env,
        )
        self._run(
            ["lake", "env", "lean", "-o", proof_olean_path.name, proof.name],
            cwd=work,
            timeout=self.config.command_timeout_seconds,
            error_type=LeanVerificationError,
            action="checking the proof and axiom audit",
            extra_env=module_env,
        )
        return implementation, proof, olean_path, proof_olean_path, c_path

    def verify(self, source: RenderedLeanSource) -> None:
        """Kernel-check the exact compiled implementation, theorem, and audit."""
        root = self.ensure()
        work_parent = root / "work"
        work_parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="verify-", dir=work_parent) as temporary:
            work = Path(temporary)
            self._check_and_codegen(work, source)

    def compile(
        self,
        source: RenderedLeanSource,
        *,
        shape: FunctionShape,
        artifact_dir: Path,
    ) -> Path:
        """Compile an already-verified implementation into a native library."""
        root = self.ensure()
        work_parent = root / "work"
        work_parent.mkdir(exist_ok=True)
        artifact_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="compile-", dir=work_parent) as temporary:
            work = Path(temporary)
            implementation, proof, implementation_object, proof_object, c_path = self._check_and_codegen(work, source)
            shim_path = work / "ffi_shim.c"
            library_path = work / f"compiled{shared_library_suffix()}"
            shim_path.write_text(
                generate_c_shim(
                    shape,
                    export_name=source.export_name,
                    module_name=source.module_name,
                ),
            )

            prefix_result = self._run(
                ["lake", "env", "lean", "--print-prefix"],
                cwd=work,
                timeout=self.config.command_timeout_seconds,
                error_type=LeanCompilationError,
                action="locating the Lean runtime",
            )
            lean_prefix = Path(prefix_result.stdout.strip())
            lean_library_dir = lean_prefix / "lib" / "lean"
            if not lean_library_dir.is_dir():
                raise LeanCompilationError(f"Lean runtime directory does not exist: {lean_library_dir}")

            libraries = ["-lleanshared"]
            init_shared = lean_library_dir / f"libInit_shared{shared_library_suffix()}"
            if init_shared.exists():
                libraries.append("-lInit_shared")
            if any(lean_library_dir.glob("libleanrt.*")):
                libraries.append("-lleanrt")

            command = [
                "lake",
                "env",
                "leanc",
                "-shared",
                "-fPIC",
                "-O2",
                "-DLEAN_EXPORTING",
                str(c_path),
                str(shim_path),
                f"-L{lean_library_dir}",
                *libraries,
                f"-Wl,-rpath,{lean_library_dir}",
                "-o",
                str(library_path),
            ]
            self._run(
                command,
                cwd=work,
                timeout=self.config.command_timeout_seconds,
                error_type=LeanCompilationError,
                action="linking the Lean shared library",
            )

            destinations = {
                implementation: artifact_dir / implementation.name,
                proof: artifact_dir / proof.name,
                implementation_object: artifact_dir / implementation_object.name,
                proof_object: artifact_dir / proof_object.name,
                c_path: artifact_dir / c_path.name,
                shim_path: artifact_dir / shim_path.name,
                library_path: artifact_dir / f"compiled{shared_library_suffix()}",
            }
            for source_path, destination in destinations.items():
                shutil.copy2(source_path, destination)
            return destinations[library_path]
