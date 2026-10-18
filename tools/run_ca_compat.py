"""Build an isolated compatibility workspace from this fork and a CA checkout.

Usage: python tools/run_ca_compat.py /absolute/path/to/ClimateAdvisor
Requires the HA/pytest dependencies described in tests_ca_compat/README.md.
The workspace is temporary; neither checkout is modified.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    ca = Path(sys.argv[1]).resolve() / "custom_components" / "climate_advisor"
    if not (ca / "manifest.json").is_file():
        raise SystemExit(f"Climate Advisor checkout not found: {ca}")
    with tempfile.TemporaryDirectory(prefix="mxz-ca-compat-") as scratch:
        work = Path(scratch)
        components = work / "custom_components"
        components.mkdir()
        (components / "__init__.py").touch()
        shutil.copytree(root / "custom_components/mxz_coordinator", components / "mxz_coordinator")
        shutil.copytree(ca, components / "climate_advisor")
        shutil.copytree(root / "tests", work / "tests")
        for source in (root / "tests_ca_compat").glob("*.py"):
            if source.name != "conftest_compat_extra.py":
                shutil.copy2(source, work / "tests" / source.name)
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--tb=long",
             "tests/test_ca_scenarios.py", "tests/test_ca_full_override.py"],
            cwd=work, check=False,
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
