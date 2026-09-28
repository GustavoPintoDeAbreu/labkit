import subprocess

import pytest

from labkit.deploy_pi import Deployer, rsync_args


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    (tmp_path / "deploy" / "pi").mkdir(parents=True)
    (tmp_path / "app.py").write_text("x")
    (tmp_path / ".gitignore").write_text("deploy/pi/VERSION\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_rsync_filter_never_ships_pi_data_or_env():
    a = rsync_args(["myapp/***", "config.yaml"])
    assert a[:8] == ["--exclude", "__pycache__", "--exclude", "*.pyc", "--exclude", "deploy/pi/.env",
                     "--exclude", "deploy/pi/data/"]
    assert a[-2:] == ["--exclude", "*"] and "myapp/***" in a and "deploy/pi/***" in a


def test_dry_run_plan(repo):
    out = []
    d = Deployer("myapp", ["myapp/***"], host="u@pi", waha_env_var="MYAPP_WAHA_URL", init_env=True,
                 dry_run=True, repo=repo, out=out.append)
    d.deploy()
    flat = [" ".join(c) for c in d.commands]
    assert flat[0].startswith("rsync -a --delete") and flat[0].endswith("./ u@pi:myapp/")
    assert any("docker compose up -d --build" in c for c in flat)
    assert any("umask 077 && cat > myapp/deploy/pi/.env" in c for c in flat)
    assert out[-1].startswith("✅ myapp deployed")


def test_dirty_tree_is_refused_unless_forced(repo):
    (repo / "app.py").write_text("changed")
    with pytest.raises(SystemExit, match="uncommitted"):
        Deployer("myapp", [], host="u@pi", dry_run=True, repo=repo, out=lambda s: None).deploy()
    Deployer("myapp", [], host="u@pi", dry_run=True, force=True, repo=repo, out=lambda s: None).deploy()


def test_init_env_rewrites_only_the_waha_line(repo, monkeypatch):
    (repo / ".env").write_text("A=1\nMYAPP_WAHA_URL=http://old:3000\nB=2\n")
    sent = {}
    d = Deployer("myapp", [], host="u@pi", waha_env_var="MYAPP_WAHA_URL", init_env=True, repo=repo,
                 out=lambda s: None)

    def fake_run(cmd, check=True, capture=False, stdin=None):
        d.commands.append(cmd)
        if stdin is not None:
            sent["env"] = stdin
        missing = cmd[-1].startswith("test -f") and "env" not in sent
        return subprocess.CompletedProcess(cmd, 1 if missing else 0, "", "")

    monkeypatch.setattr(d, "_run", fake_run)
    d.env()
    assert sent["env"] == "A=1\nMYAPP_WAHA_URL=http://kaya-waha:3000\nB=2\n"
