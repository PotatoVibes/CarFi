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
