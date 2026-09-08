"""
Install Chrome's shared libraries at run time, without root.

Streamlit Community Cloud gives no root, and its build image still lists
Debian 11 "bullseye", whose security suite expired when bullseye left LTS on
2026-08-31. That makes the platform's `apt-get update` exit non-zero, so a
packages.txt fails the build outright. Selenium Manager can still fetch Chrome
for Testing, but not the system libraries Chrome links against, so the browser
dies with exit code 127.

This module sidesteps both problems by building a private apt environment
under ~/.cache that points only at a live Debian suite, never at the expired
one. `apt-get download` and `dpkg-deb -x` both work unprivileged, so the
libraries can be unpacked into a prefix the caller puts on LD_LIBRARY_PATH.

Only libraries are fetched. Chromium itself is skipped, because Selenium
Manager has already supplied a browser and the package would add a few hundred
megabytes to a host with a 1 GB ceiling.
"""

import os
import shutil
import subprocess
from pathlib import Path

CACHE = Path(os.path.expanduser("~")) / ".cache" / "chrome-deps"
APT = CACHE / "apt"
DEBS = CACHE / "debs"
ROOT = CACHE / "root"
STAMP = CACHE / ".complete"

# Chromium's own payload. We want what it links against, not the browser.
SKIP = {
    "chromium", "chromium-common", "chromium-driver",
    "chromium-sandbox", "chromium-shell", "chromium-l10n",
}

ARCH_DIRS = ("usr/lib/x86_64-linux-gnu", "lib/x86_64-linux-gnu", "usr/lib")


def _run(cmd, cwd=None, timeout=900):
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )


def debian_suite() -> str:
    """Codename of the running Debian, falling back to a suite that is live."""
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("VERSION_CODENAME="):
                name = line.split("=", 1)[1].strip().strip('"')
                # bullseye is EOL; its security suite is what broke apt here.
                if name and name != "bullseye":
                    return name
    except Exception:
        pass
    return "trixie"


def _apt_flags() -> list:
    """Point every apt directory at our cache so nothing needs root."""
    return [
        "-o", f"Dir::State={APT}/state",
        "-o", f"Dir::State::Lists={APT}/state/lists",
        "-o", f"Dir::Cache={APT}/cache",
        "-o", f"Dir::Cache::Archives={APT}/cache/archives",
        "-o", f"Dir::Etc::SourceList={APT}/sources.list",
        "-o", "Dir::Etc::SourceParts=/dev/null",
        "-o", "Dir::Etc::Preferences=/dev/null",
        "-o", "Dir::Etc::PreferencesParts=/dev/null",
        "-o", "Dir::Log=/dev/null",
        "-o", "Debug::NoLocking=true",
    ]


def _write_sources() -> None:
    keyring = "/usr/share/keyrings/debian-archive-keyring.gpg"
    # Prefer signature checking; fall back only if the keyring is absent.
    marker = f"[signed-by={keyring}]" if os.path.exists(keyring) else "[trusted=yes]"
    suite = debian_suite()
    (APT / "sources.list").write_text(
        f"deb {marker} http://deb.debian.org/debian {suite} main\n"
    )


def _prepare(log) -> bool:
    for d in (
        APT / "state" / "lists" / "partial",
        APT / "cache" / "archives" / "partial",
        DEBS,
        ROOT,
    ):
        d.mkdir(parents=True, exist_ok=True)
    _write_sources()
    log(f"Fetching package lists for Debian {debian_suite()}...")
    res = _run(["apt-get", "update", *_apt_flags()])
    if res.returncode != 0:
        log(f"  apt-get update failed: {res.stderr.strip().splitlines()[-1:] or ''}")
        return False
    return True


def _installed(pkg: str) -> bool:
    res = _run(["dpkg-query", "-W", "-f=${Status}", pkg], timeout=30)
    return res.returncode == 0 and "install ok installed" in res.stdout


def _closure(log) -> list:
    """Packages chromium depends on, minus chromium itself and what is present."""
    res = _run([
        "apt-cache", "depends", "--recurse",
        "--no-recommends", "--no-suggests", "--no-conflicts",
        "--no-breaks", "--no-replaces", "--no-enhances",
        *_apt_flags(), "chromium",
    ])
    if res.returncode != 0 or not res.stdout.strip():
        log("  could not resolve chromium's dependencies")
        return []

    names = []
    for raw in res.stdout.splitlines():
        # Package names sit at column 0; dependency lines are indented.
        if raw and not raw[0].isspace() and not raw.startswith("<"):
            name = raw.strip()
            if name and name not in SKIP and name not in names:
                names.append(name)

    wanted = [p for p in names if not _installed(p)]
    log(f"  {len(names)} packages in closure, {len(wanted)} not already present")
    return wanted


def _download(pkgs: list, log) -> int:
    """Fetch .deb files. Batches, falling back to one-by-one on failure."""
    got = 0
    for i in range(0, len(pkgs), 30):
        batch = pkgs[i:i + 30]
        res = _run(["apt-get", "download", *_apt_flags(), *batch], cwd=DEBS)
        if res.returncode == 0:
            got += len(batch)
            continue
        # One bad name fails the whole batch, so retry individually.
        for pkg in batch:
            if _run(["apt-get", "download", *_apt_flags(), pkg], cwd=DEBS).returncode == 0:
                got += 1
        log(f"  downloaded {got}/{len(pkgs)} so far")
    return got


def _extract(log) -> int:
    count = 0
    for deb in sorted(DEBS.glob("*.deb")):
        if _run(["dpkg-deb", "-x", str(deb), str(ROOT)]).returncode == 0:
            count += 1
    return count


def library_path(root: Path = ROOT) -> list:
    """Directories under the unpacked prefix that hold shared objects."""
    return [str(root / d) for d in ARCH_DIRS if (root / d).is_dir()]


def ensure_libraries(log=lambda m: None):
    """Unpack Chrome's libraries if needed. Returns the prefix, or None."""
    if STAMP.exists() and library_path():
        return ROOT
    if not shutil.which("apt-get") or not shutil.which("dpkg-deb"):
        log("apt-get or dpkg-deb unavailable — cannot install libraries here.")
        return None

    log("Installing Chrome's system libraries (first run only, this is slow)...")
    try:
        if not _prepare(log):
            return None
        pkgs = _closure(log)
        if not pkgs:
            return None
        log(f"Downloading {len(pkgs)} packages...")
        if _download(pkgs, log) == 0:
            log("  nothing downloaded")
            return None
        log("Unpacking...")
        count = _extract(log)
        shutil.rmtree(DEBS, ignore_errors=True)   # reclaim disk immediately
        if not library_path():
            log("  no library directories produced")
            return None
        STAMP.touch()
        log(f"Installed libraries from {count} packages.")
        return ROOT
    except subprocess.TimeoutExpired:
        log("  timed out while installing libraries")
        return None
    except Exception as e:
        log(f"  library install failed: {e}")
        return None
