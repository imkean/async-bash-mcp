"""deploy/install.sh: the tracked unit is a template, so the installer must
render it and never install a hardcoded checkout path.

A fake ``systemctl`` on PATH records what the installer asks for, so these tests
never touch the real user manager.
"""

import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "deploy" / "install.sh"
UNIT_NAME = "async-bash-mcp.service"


def make_fake_systemctl(tmp_path: Path):
    """Return (bin dir, log file) for a systemctl that only records its args."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "systemctl.log"
    script = bin_dir / "systemctl"
    script.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "{log}"\nexit 0\n')
    script.chmod(0o755)
    return bin_dir, log


def run_installer(installer: Path, tmp_path: Path, *args: str, bin_dir: Path):
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        [str(installer), *args], env=env, capture_output=True, text=True, check=False
    )


def unit_path(tmp_path: Path) -> Path:
    return tmp_path / "config" / "systemd" / "user" / UNIT_NAME


def test_renders_the_checkout_path(tmp_path):
    bin_dir, log = make_fake_systemctl(tmp_path)

    result = run_installer(INSTALLER, tmp_path, bin_dir=bin_dir)

    assert result.returncode == 0, result.stderr
    text = unit_path(tmp_path).read_text()
    assert "@REPO@" not in text
    assert f'ExecStart="{REPO}/deploy/serve.sh"' in text
    assert "Documentation=https://" in text
    assert "--user daemon-reload" in log.read_text()


def test_quotes_a_checkout_path_with_spaces_and_ampersands(tmp_path):
    """A path with a space needs quoting in ExecStart; `&` must survive sed."""
    awkward = tmp_path / "space & repo"
    shutil.copytree(REPO / "deploy", awkward / "deploy")
    bin_dir, _ = make_fake_systemctl(tmp_path)

    result = run_installer(awkward / "deploy" / "install.sh", tmp_path, bin_dir=bin_dir)

    assert result.returncode == 0, result.stderr
    text = unit_path(tmp_path).read_text()
    assert f'ExecStart="{awkward}/deploy/serve.sh"' in text


def test_replaces_a_stale_symlink(tmp_path):
    unit = unit_path(tmp_path)
    unit.parent.mkdir(parents=True)
    unit.symlink_to("/nonexistent/old-checkout/deploy/async-bash-mcp.service")
    bin_dir, _ = make_fake_systemctl(tmp_path)

    result = run_installer(INSTALLER, tmp_path, bin_dir=bin_dir)

    assert result.returncode == 0, result.stderr
    assert not unit.is_symlink()
    assert f'ExecStart="{REPO}/deploy/serve.sh"' in unit.read_text()


def test_now_enables_and_starts(tmp_path):
    bin_dir, log = make_fake_systemctl(tmp_path)

    assert run_installer(INSTALLER, tmp_path, "--now", bin_dir=bin_dir).returncode == 0

    assert "--user enable --now async-bash-mcp.service" in log.read_text()


def test_restart_flag_restarts(tmp_path):
    bin_dir, log = make_fake_systemctl(tmp_path)

    assert (
        run_installer(INSTALLER, tmp_path, "--restart", bin_dir=bin_dir).returncode == 0
    )

    assert "--user restart async-bash-mcp.service" in log.read_text()


def test_unknown_flag_exits_two(tmp_path):
    bin_dir, log = make_fake_systemctl(tmp_path)

    result = run_installer(INSTALLER, tmp_path, "--bogus", bin_dir=bin_dir)

    assert result.returncode == 2
    assert "unknown argument" in result.stderr
    assert not log.exists(), "nothing should be installed for a bad invocation"


def test_help_exits_zero(tmp_path):
    bin_dir, _ = make_fake_systemctl(tmp_path)

    result = run_installer(INSTALLER, tmp_path, "--help", bin_dir=bin_dir)

    assert result.returncode == 0
    assert "usage:" in result.stderr
