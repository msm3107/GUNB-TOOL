from pathlib import Path

import pytest

from gunb_tool.config import ConfigError, load_config
from gunb_tool.models import Source

REPO_ROOT = Path(__file__).resolve().parent.parent


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_minimal_config_gets_defaults(tmp_path):
    cfg = load_config(write_config(tmp_path, "gunb:\n  voivodeships: ['16']\n"), env={})
    assert cfg.gunb.voivodeships == ("16",)
    assert cfg.gunb.powiats == ()
    assert cfg.gunb.sources == (Source.POZWOLENIA,)
    assert cfg.gunb.date_field == "decyzja"
    assert cfg.filter.drop_noise is True
    assert "ogrodzen" in cfg.filter.exclude_keywords
    assert cfg.http.user_agents, "domyślna pula User-Agent nie może być pusta"
    assert cfg.geocoding.enabled is True


def test_env_placeholders_are_interpolated_with_defaults(tmp_path):
    path = write_config(
        tmp_path,
        "gunb:\n  voivodeships: ['16']\n"
        "telegram:\n  bot_token: ${TG_TOKEN}\n  chat_id: ${TG_CHAT:-12345}\n"
        "discord:\n  webhook_url: ${MISSING_VAR}\n",
    )
    cfg = load_config(path, env={"TG_TOKEN": "abc:def"})
    assert cfg.telegram.bot_token == "abc:def"
    assert cfg.telegram.chat_id == "12345"
    assert cfg.discord.webhook_url == ""


def test_relative_paths_resolve_against_config_directory(tmp_path):
    path = write_config(
        tmp_path,
        "gunb:\n  voivodeships: ['16']\n  cache_dir: cache\n"
        "storage:\n  db_path: db/leads.sqlite\n"
        "sheets:\n  service_account_file: sa.json\n",
    )
    cfg = load_config(path, env={})
    assert cfg.gunb.cache_dir == tmp_path / "cache"
    assert cfg.storage.db_path == tmp_path / "db" / "leads.sqlite"
    assert cfg.sheets.service_account_file == tmp_path / "sa.json"


def test_powiats_are_normalized_and_imply_their_voivodeships(tmp_path):
    path = write_config(tmp_path, "gunb:\n  voivodeships: ['małopolskie']\n  powiats: [1206, '1606']\n")
    cfg = load_config(path, env={})
    assert cfg.gunb.powiats == ("1206", "1606")
    assert cfg.gunb.voivodeships == ("12", "16")


def test_sources_accept_both_registries(tmp_path):
    path = write_config(tmp_path, "gunb:\n  voivodeships: ['16']\n  sources: [pozwolenia, zgloszenia]\n")
    assert load_config(path, env={}).gunb.sources == (Source.POZWOLENIA, Source.ZGLOSZENIA)


@pytest.mark.parametrize(
    "body,fragment",
    [
        ("gunb:\n  voivodeships: ['99']\n", "województwo"),
        # Niecytowane 0201 YAML 1.1 czyta jako liczbę ósemkową (129) – musi zostać odrzucone.
        ("gunb:\n  powiats: [0201]\n", "cudzysłowie"),
        ("gunb:\n  voivodeships: ['16']\n  date_field: jutro\n", "date_field"),
        ("gunb:\n  voivodeships: ['16']\n  sources: [mapa]\n", "sources"),
        ("gunb:\n  voivodeships: []\n", "voivodeships"),
        ("gunb:\n  voivodeships: ['16']\nhttp:\n  min_delay: 5\n  max_delay: 1\n", "min_delay"),
        ("gunb:\n  voivodeships: ['16']\nhttp:\n  user_agents: []\n", "user_agents"),
    ],
)
def test_invalid_values_raise_config_error(tmp_path, body, fragment):
    with pytest.raises(ConfigError, match=fragment):
        load_config(write_config(tmp_path, body), env={})


def test_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml", env={})


def test_repository_config_file_is_valid():
    cfg = load_config(REPO_ROOT / "config.yaml", env={})
    assert cfg.gunb.voivodeships
