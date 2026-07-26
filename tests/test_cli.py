import os

from cutmaster.cli import _load_runtime_environment


def test_runtime_environment_loads_dotenv_next_to_config(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.touch()
    (tmp_path / ".env").write_text(
        "DASHSCOPE_API_KEY=from-dotenv\n"
        "PRESERVED_VALUE=from-dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("PRESERVED_VALUE", "from-process")

    _load_runtime_environment(config_path)

    assert os.environ["DASHSCOPE_API_KEY"] == "from-dotenv"
    assert os.environ["PRESERVED_VALUE"] == "from-process"
