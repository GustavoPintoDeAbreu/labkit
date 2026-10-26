import json

import pytest

from labkit import gpu_claims


def test_claim_counts_until_released_or_expired(tmp_path):
    gpu_claims.claim("redditcast", reason="podcast", ttl_s=60, directory=tmp_path, now=lambda: 1000)
    assert [c["owner"] for c in gpu_claims.claims(directory=tmp_path, now=lambda: 1030)] == ["redditcast"]
    assert gpu_claims.claims(directory=tmp_path, now=lambda: 1060) == []          # expired
    gpu_claims.release("redditcast", directory=tmp_path)
    assert gpu_claims.claims(directory=tmp_path, now=lambda: 1030) == []
    gpu_claims.release("redditcast", directory=tmp_path)                          # twice is fine


def test_a_booking_counts_only_from_its_start(tmp_path):
    gpu_claims.claim("redditcast", since=5000, ttl_s=100, directory=tmp_path)
    assert gpu_claims.claims(directory=tmp_path, now=lambda: 4999) == []
    assert len(gpu_claims.claims(directory=tmp_path, now=lambda: 5000)) == 1


def test_claims_filter_by_card_and_skip_broken_files(tmp_path):
    gpu_claims.claim("a", gpus=["gpu0"], directory=tmp_path, now=lambda: 0)
    gpu_claims.claim("b", gpus=["gpu1"], directory=tmp_path, now=lambda: 0)
    (tmp_path / "junk.json").write_text("{not json")
    (tmp_path / "old.json").write_text(json.dumps({"owner": "old"}))
    assert [c["owner"] for c in gpu_claims.claims(gpus=["gpu1"], directory=tmp_path, now=lambda: 1)] == ["b"]
    assert len(gpu_claims.claims(directory=tmp_path, now=lambda: 1)) == 2
    assert (tmp_path / "junk.json").exists()                                      # never deleted


def test_holding_releases_on_error_and_owner_names_are_checked(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_GPU_CLAIMS", str(tmp_path))
    with pytest.raises(RuntimeError):
        with gpu_claims.holding("redditcast"):
            assert len(gpu_claims.claims()) == 1
            raise RuntimeError("boom")
    assert gpu_claims.claims() == []
    with pytest.raises(ValueError):
        gpu_claims.claim("../etc/passwd")
    assert gpu_claims.claims(directory=tmp_path / "missing") == []
