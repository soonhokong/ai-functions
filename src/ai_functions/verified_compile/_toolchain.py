"""Automatic, isolated Lean toolchain provisioning."""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ._locking import exclusive_file_lock
from .config import VerifiedCompileConfig
from .errors import LeanSetupError

logger = logging.getLogger(__name__)

_ELAN_VERSION = "4.2.3"
_ELAN_RELEASE_BASE = f"https://github.com/leanprover/elan/releases/download/v{_ELAN_VERSION}"
_ELAN_ASSETS = {
    ("darwin", "aarch64"): (
        "elan-aarch64-apple-darwin.tar.gz",
        "7cae4c03b2f0de4053fb04a91359d5804551e6e37a6ddd1b2e0097dc561ae4a9",
    ),
    ("darwin", "x86_64"): (
        "elan-x86_64-apple-darwin.tar.gz",
        "10d037a69731c0593723e018130c5f54afde175796b4af8ba1317e561e55598c",
    ),
    ("linux", "aarch64"): (
        "elan-aarch64-unknown-linux-gnu.tar.gz",
        "cb69af0803b04157bc30201c29c12fca882bb3ad8b43476b8d2d3064810bc3ac",
    ),
    ("linux", "x86_64"): (
        "elan-x86_64-unknown-linux-gnu.tar.gz",
        "df0b2b3a439961ffcbb3985214365ffe40f49bc871df04dff268c7d8e21ca8b2",
    ),
}


@dataclass(frozen=True)
class LeanCommandEnvironment:
    """The resolved Lake executable and environment for subprocesses."""

    lake: Path
    environment: dict[str, str]


def _normalized_machine() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "aarch64"
    if machine in {"amd64", "x86_64"}:
        return "x86_64"
    return machine


def _elan_asset() -> tuple[str, str]:
    if sys.platform == "darwin":
        platform_name = "darwin"
    elif sys.platform.startswith("linux"):
        platform_name = "linux"
    else:
        platform_name = sys.platform
    key = (platform_name, _normalized_machine())
    try:
        return _ELAN_ASSETS[key]
    except KeyError as exc:
        raise LeanSetupError(
            f"Automatic Lean installation does not support {sys.platform!r} on {platform.machine()!r}",
        ) from exc


def _download(url: str, destination: Path, *, timeout: float) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "strands-ai-functions"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output)
    except (OSError, urllib.error.URLError) as exc:
        raise LeanSetupError(
            f"Could not download the managed Lean installer from {url}. "
            "Check network access, or use VerifiedCompileConfig(toolchain_mode='system').",
        ) from exc


def _verify_sha256(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        raise LeanSetupError(f"Managed Lean installer checksum mismatch: expected {expected}, got {actual}")


def _install_elan(archive_path: Path, destination: Path, *, timeout: float) -> None:
    with tempfile.TemporaryDirectory(prefix="elan-install-", dir=destination.parent) as temporary:
        temporary_path = Path(temporary)
        with tarfile.open(archive_path, "r:gz") as archive:
            try:
                installer_member = archive.getmember("elan-init")
            except KeyError as exc:
                raise LeanSetupError("Managed Lean installer archive does not contain `elan-init`") from exc
            archive.extract(installer_member, temporary_path, filter="data")

        installer = temporary_path / "elan-init"
        installer.chmod(installer.stat().st_mode | stat.S_IXUSR)
        staging_home = temporary_path / "elan-home"
        environment = {**os.environ, "ELAN_HOME": str(staging_home)}
        try:
            result = subprocess.run(
                [
                    str(installer),
                    "-y",
                    "--no-modify-path",
                    "--default-toolchain",
                    "none",
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LeanSetupError("Could not install the managed Lean toolchain bootstrap") from exc
        if result.returncode != 0:
            output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
            raise LeanSetupError(f"Managed Lean installer failed:\n{output or '(no diagnostic output)'}")

        lake = staging_home / "bin" / "lake"
        if not lake.exists():
            raise LeanSetupError("Managed Lean installer completed without creating the `lake` shim")
        (staging_home / ".bootstrap-complete").write_text(f"elan {_ELAN_VERSION}\n")
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(staging_home, destination)


class LeanToolchain:
    """Resolve either an isolated managed toolchain or an explicit system one."""

    def __init__(self, config: VerifiedCompileConfig) -> None:
        self.config = config
        self.cache_root = Path(config.cache_dir).expanduser().resolve() / "toolchains"

    def _system(self) -> LeanCommandEnvironment:
        lake = shutil.which("lake")
        if lake is None:
            raise LeanSetupError(
                "`lake` was not found on PATH while toolchain_mode='system'. "
                "Use the default managed mode to install Lean automatically.",
            )
        return LeanCommandEnvironment(lake=Path(lake), environment={})

    def _managed(self) -> LeanCommandEnvironment:
        asset_name, expected_sha256 = _elan_asset()
        destination = self.cache_root / f"elan-{_ELAN_VERSION}-{asset_name.removesuffix('.tar.gz')}"
        lake = destination / "bin" / "lake"
        marker = destination / ".bootstrap-complete"
        if marker.exists() and lake.exists():
            return self._managed_environment(destination, lake)

        lock_path = self.cache_root / ".locks" / f"{destination.name}.lock"
        with exclusive_file_lock(lock_path):
            if not marker.exists() or not lake.exists():
                self.cache_root.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix="elan-download-", dir=self.cache_root) as temporary:
                    archive_path = Path(temporary) / asset_name
                    url = f"{_ELAN_RELEASE_BASE}/{asset_name}"
                    logger.info("Downloading managed Lean bootstrap from %s", url)
                    _download(url, archive_path, timeout=self.config.setup_timeout_seconds)
                    _verify_sha256(archive_path, expected_sha256)
                    _install_elan(
                        archive_path,
                        destination,
                        timeout=self.config.setup_timeout_seconds,
                    )
        return self._managed_environment(destination, lake)

    @staticmethod
    def _managed_environment(destination: Path, lake: Path) -> LeanCommandEnvironment:
        path = str(destination / "bin")
        inherited_path = os.environ.get("PATH")
        if inherited_path:
            path = os.pathsep.join((path, inherited_path))
        return LeanCommandEnvironment(
            lake=lake,
            environment={
                "ELAN_HOME": str(destination),
                "PATH": path,
            },
        )

    def ensure(self) -> LeanCommandEnvironment:
        """Return a usable Lake command, provisioning it when necessary."""
        if self.config.toolchain_mode == "system":
            return self._system()
        return self._managed()
