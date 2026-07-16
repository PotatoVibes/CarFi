"""Settings loading — env + .env, offline."""

from __future__ import annotations

from carfinder.config import load_settings


def test_defaults_when_env_unset(monkeypatch, tmp_path):
    for k in (
        "AUTODEV_API_KEY",
        "ANTHROPIC_API_KEY",
        "MONTHLY_CALL_CAP_AUTODEV",
        "MONTHLY_AI_BUDGET_USD",
        "CARFINDER_DB_PATH",
    ):
        monkeypatch.delenv(k, raising=False)
    s = load_settings(env_file=tmp_path / "missing.env")
    assert s.monthly_call_cap_autodev == 800
    assert s.monthly_ai_budget_usd == 2.0
    assert s.autodev_api_key is None


def test_dotenv_is_read_but_does_not_override_real_env(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        'AUTODEV_API_KEY="from-dotenv"\nMONTHLY_CALL_CAP_AUTODEV=500\n', encoding="utf-8"
    )
    monkeypatch.delenv("AUTODEV_API_KEY", raising=False)
    monkeypatch.setenv("MONTHLY_CALL_CAP_AUTODEV", "123")  # real env wins
    s = load_settings(env_file=env)
    assert s.autodev_api_key == "from-dotenv"
    assert s.monthly_call_cap_autodev == 123
