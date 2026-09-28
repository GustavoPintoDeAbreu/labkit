from labkit import scaffold
from labkit.cli import main


def test_init_writes_three_files_and_never_overwrites(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# mine\n")
    out = []
    written = scaffold.init(tmp_path, "My App", "http://hub:8095", out=out.append)
    assert sorted(p.name for p in written) == ["AGENTS.md", "lab.yaml"]
    assert (tmp_path / "CLAUDE.md").read_text() == "# mine\n"
    assert any("@AGENTS.md" in o for o in out)
    assert "## Lab" in (tmp_path / "AGENTS.md").read_text()
    assert "http://hub:8095/api/catalog?format=md" in (tmp_path / "AGENTS.md").read_text()
    assert "product: my-app" in (tmp_path / "lab.yaml").read_text()


def test_lab_yaml_skeleton_parses():
    import yaml
    doc = yaml.safe_load(scaffold.lab_yaml("x"))
    assert doc["product"] == "x" and doc["docs"][0]["path"] == "AGENTS.md"


def test_install_skills_refreshes_own_and_spares_others(tmp_path):
    (tmp_path / "send-whatsapp").mkdir()                      # someone else's skill of the same name
    done = scaffold.install_skills(tmp_path, out=lambda s: None)
    names = {p.name for p in done}
    assert "send-whatsapp" not in names and {"use-llm-broker", "deploy-to-pi", "new-lab-project"} <= names
    assert (tmp_path / "use-llm-broker" / "SKILL.md").read_text().startswith("---\nname: use-llm-broker")
    assert {p.name for p in scaffold.install_skills(tmp_path, out=lambda s: None)} == names   # rerun refreshes


def test_cli(tmp_path, capsys):
    assert main(["init", str(tmp_path), "--hub", "http://h:1"]) == 0
    assert (tmp_path / "CLAUDE.md").read_text() == "@AGENTS.md\n"
    assert main(["install-skills", "--dest", str(tmp_path / "sk")]) == 0
