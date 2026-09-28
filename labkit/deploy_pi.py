"""Deploy a project's compose service to the Raspberry Pi (``labkit deploy-pi``).

The lab's pattern (flight-deals, redditcast): the code is rsynced to
``~/<name>`` on the Pi and built there (no cross-building), from a compose
project in ``deploy/pi/``. The live data (``deploy/pi/data/``) and secrets
(``deploy/pi/.env``) live only on the Pi and a sync never touches them. The
compose project joins Kaya's ``pi_default`` network to reach ``kaya-waha``.

Only committed code is deployed: a dirty tree is refused unless ``--force``, and
the commit is stamped into ``deploy/pi/VERSION`` so the image can report it.

``--init-env`` writes the Pi's ``.env`` from this checkout's ``.env`` when it
does not exist yet, pointing ``--waha-env-var`` at ``http://kaya-waha:3000``. An
existing ``.env`` is never overwritten.

The host is ``--pi``, else ``$PI_HOST``, else ``<you>@pi5.local``.
"""
from __future__ import annotations

import argparse
import getpass
import os
import shlex
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence

WAHA_ON_PI = "http://kaya-waha:3000"


def default_host() -> str:
    return os.environ.get("PI_HOST") or f"{getpass.getuser()}@pi5.local"


def rsync_args(includes: Sequence[str]) -> List[str]:
    """The include/exclude filter: the given paths plus deploy/pi, never the Pi's data or .env."""
    args = ["--exclude", "__pycache__", "--exclude", "*.pyc",
            "--exclude", "deploy/pi/.env", "--exclude", "deploy/pi/data/"]
    for inc in includes:
        args += ["--include", inc]
    return args + ["--include", "deploy/", "--include", "deploy/pi/***", "--exclude", "*"]


class Deployer:
    def __init__(self, name: str, includes: Sequence[str], *, host: Optional[str] = None,
                 remote_dir: Optional[str] = None, waha_env_var: Optional[str] = None,
                 init_env: bool = False, force: bool = False, dry_run: bool = False,
                 repo: Optional[Path] = None, out=print):
        self.name, self.includes = name, list(includes)
        self.host = host or default_host()
        self.remote = remote_dir or name
        self.waha_env_var, self.init_env, self.force, self.dry = waha_env_var, init_env, force, dry_run
        self.repo = Path(repo or Path.cwd())
        self.out = out
        self.commands: List[List[str]] = []      # what ran (or would run), for tests and --dry-run

    # ---- process helpers
    def _run(self, cmd: List[str], *, check: bool = True, capture: bool = False, stdin: Optional[str] = None):
        self.commands.append(cmd)
        if self.dry:
            self.out("   $ " + " ".join(shlex.quote(c) for c in cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.run(cmd, cwd=self.repo, check=check, text=True, input=stdin,
                              capture_output=capture)

    def _ssh(self, remote_cmd: str, **kw):
        return self._run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", self.host, remote_cmd], **kw)

    def _git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.repo, check=True, text=True, capture_output=True).stdout.strip()

    # ---- steps
    def check_clean(self) -> None:
        dirty = self._git("status", "--porcelain")
        if not dirty:
            return
        if not self.force:
            raise SystemExit(f"❌ uncommitted changes; commit first, or pass --force\n{dirty}")
        self.out("⚠️  deploying uncommitted changes (--force)")

    def stamp(self) -> str:
        version = self._git("describe", "--always", "--dirty")
        if not self.dry:
            (self.repo / "deploy" / "pi" / "VERSION").write_text(version + "\n")
        self.out(f"🏷️  version {version}")
        return version

    def sync(self) -> None:
        self.out(f"📦 Syncing code to {self.host}:~/{self.remote} ...")
        self._run(["rsync", "-a", "--delete", *rsync_args(self.includes), "./", f"{self.host}:{self.remote}/"])
        self._ssh(f"mkdir -p {shlex.quote(self.remote)}/deploy/pi/data")

    def env(self) -> None:
        env_path = f"{self.remote}/deploy/pi/.env"
        if self.init_env:
            exists = self._ssh(f"test -f {shlex.quote(env_path)}", check=False).returncode == 0 and not self.dry
            if exists:
                self.out(f"ℹ️  {env_path} already exists; not touching it")
            else:
                local = (self.repo / ".env").read_text() if (self.repo / ".env").exists() else ""
                if self.waha_env_var:
                    lines = [f"{self.waha_env_var}={WAHA_ON_PI}" if ln.startswith(self.waha_env_var + "=") else ln
                             for ln in local.splitlines()]
                    local = "\n".join(lines) + ("\n" if lines else "")
                self._ssh(f"umask 077 && cat > {shlex.quote(env_path)}", stdin=local)
                self.out(f"🔐 wrote {env_path}" + (f" ({self.waha_env_var} -> {WAHA_ON_PI})" if self.waha_env_var else ""))
        if self._ssh(f"test -f {shlex.quote(env_path)}", check=False).returncode != 0:
            raise SystemExit(f"❌ {env_path} missing on the Pi: run with --init-env")

    def up(self) -> None:
        self.out("🔨 Building and (re)starting on the Pi ...")
        d = shlex.quote(f"{self.remote}/deploy/pi")
        self._ssh(f"cd {d} && docker compose up -d --build && docker image prune -f >/dev/null")
        self._ssh(f"cd {d} && docker compose ps --format '{{{{.Name}}}}  {{{{.Status}}}}'")

    def deploy(self) -> None:
        self.check_clean()
        self.stamp()
        if not self.dry and self._ssh("true", check=False).returncode != 0:
            raise SystemExit(f"❌ cannot reach {self.host}")
        self.sync()
        self.env()
        self.up()
        self.out(f"✅ {self.name} deployed. Logs: ssh {self.host} docker logs -f {self.name}")


def add_parser(sub) -> None:
    p = sub.add_parser("deploy-pi", help="deploy this repo's deploy/pi compose project to the Raspberry Pi",
                       description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", required=True, help="remote folder under ~ and the container name")
    p.add_argument("--include", action="append", default=[], metavar="PATTERN",
                   help="rsync include pattern for code to ship (repeat), e.g. 'myapp/***', config.yaml")
    p.add_argument("--waha-env-var", help="the .env key --init-env points at kaya-waha on the Pi")
    p.add_argument("--init-env", action="store_true")
    p.add_argument("--force", action="store_true", help="deploy uncommitted changes")
    p.add_argument("--pi", help="ssh target (default $PI_HOST or <you>@pi5.local)")
    p.add_argument("--dry-run", action="store_true", help="print the commands instead of running them")
    p.set_defaults(func=_main)


def _main(a: argparse.Namespace) -> int:
    Deployer(a.name, a.include, host=a.pi, waha_env_var=a.waha_env_var, init_env=a.init_env,
             force=a.force, dry_run=a.dry_run).deploy()
    return 0

