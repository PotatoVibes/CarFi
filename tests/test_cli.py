"""CLI smoke test — offline."""

from __future__ import annotations

from typer.testing import CliRunner

from carfinder.cli import app

runner = CliRunner()


def test_budget_command_reports_both_providers(monkeypatch, tmp_path):
    monkeypatch.setenv("CARFINDER_DB_PATH", str(tmp_path / "cli.sqlite"))
    monkeypatch.delenv("MONTHLY_CALL_CAP_AUTODEV", raising=False)
    result = runner.invoke(app, ["budget"])
    assert result.exit_code == 0, result.output
    assert "autodev" in result.output
    assert "anthropic" in result.output
    assert "800" in result.output  # default Auto.dev cap


def test_rescore_only_model_scopes_to_one_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("CARFINDER_DB_PATH", str(tmp_path / "cli.sqlite"))
    result = runner.invoke(app, ["rescore", "--only-model", "ct4v_blackwing"])
    assert result.exit_code == 0, result.output
    # Only the requested profile is reported — the C8 is skipped entirely.
    assert "ct4v_blackwing" in result.output
    assert "c8_stingray" not in result.output


def test_rescore_unknown_model_fails_with_known_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("CARFINDER_DB_PATH", str(tmp_path / "cli.sqlite"))
    result = runner.invoke(app, ["rescore", "--only-model", "nope"])
    assert result.exit_code != 0
    assert "nope" in result.output and "c8_stingray" in result.output
