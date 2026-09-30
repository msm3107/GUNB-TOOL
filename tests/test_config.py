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


# --- Segmenty klientów ----------------------------------------------------------------------

SEGMENTS_YAML = """
gunb:
  voivodeships: ['16']
segments:
  domki:
    label: Domki jednorodzinne
    categories: [mieszkaniowa-jednorodzinna]
    max_kubatura: 2500
    telegram_chat_id: ${CHAT_DOMKI}
  duze:
    label: Duże inwestycje
    categories: [mieszkaniowa-jednorodzinna, mieszkaniowa-wielorodzinna, komercyjna]
"""


def test_segments_are_loaded_in_declared_order(tmp_path):
    cfg = load_config(write_config(tmp_path, SEGMENTS_YAML), env={"CHAT_DOMKI": "-100777"})
    assert [s.name for s in cfg.segments] == ["domki", "duze"]
    domki, duze = cfg.segments
    assert (domki.label, domki.max_kubatura, domki.min_kubatura) == ("Domki jednorodzinne", 2500, None)
    assert domki.categories == ("mieszkaniowa-jednorodzinna",)
    assert domki.telegram_chat_id == "-100777"
    assert duze.telegram_chat_id == ""


def test_config_without_segments_has_none(tmp_path):
    assert load_config(write_config(tmp_path, "gunb:\n  voivodeships: ['16']\n"), env={}).segments == ()


@pytest.mark.parametrize(
    "segment,fragment",
    [
        ("x:\n    categories: [palace]\n", "palace"),
        ("x:\n    min_kubatura: 5000\n    max_kubatura: 100\n", "min_kubatura"),
        ("Zla Nazwa:\n    label: a\n", "Zla Nazwa"),
    ],
)
def test_invalid_segments_raise(tmp_path, segment, fragment):
    body = "gunb:\n  voivodeships: ['16']\nsegments:\n  " + segment
    with pytest.raises(ConfigError, match=fragment):
        load_config(write_config(tmp_path, body), env={})


def test_repository_config_defines_domki_and_duze_segments():
    cfg = load_config(REPO_ROOT / "config.yaml", env={})
    assert [s.name for s in cfg.segments] == ["domki", "duze"]


# --- Powiadomienia ---------------------------------------------------------------------------

def test_notification_defaults(tmp_path):
    cfg = load_config(write_config(tmp_path, "gunb:\n  voivodeships: ['16']\n"), env={})
    assert cfg.notifications.max_leads_per_run == 200
    assert cfg.notifications.digest_threshold == 10
    assert cfg.telegram.delay_seconds == 1.0


def test_legacy_max_messages_per_run_is_still_accepted(tmp_path):
    body = "gunb:\n  voivodeships: ['16']\nnotifications:\n  max_messages_per_run: 30\n"
    assert load_config(write_config(tmp_path, body), env={}).notifications.max_leads_per_run == 30


def test_telegram_cannot_send_faster_than_one_message_per_second(tmp_path):
    body = "gunb:\n  voivodeships: ['16']\ntelegram:\n  delay_seconds: 0.5\n"
    with pytest.raises(ConfigError, match="delay_seconds"):
        load_config(write_config(tmp_path, body), env={})


# --- Utwardzenie backendu ------------------------------------------------------------------------

def test_hardening_defaults(tmp_path):
    cfg = load_config(write_config(tmp_path, "gunb:\n  voivodeships: ['16']\n"), env={})
    assert (cfg.http.circuit_breaker_failures, cfg.http.circuit_breaker_cooldown) == (3, 600.0)
    assert cfg.telegram.admin_chat_id == ""
    assert cfg.storage.backup_dir == tmp_path / "data" / "backups"  # obok bazy
    # codzienna kopia, dwa tygodnie wstecz
    assert (cfg.storage.backup_every_days, cfg.storage.backup_keep, cfg.storage.vacuum_threshold) == (1, 14, 500)


def test_hardening_settings_are_read_from_file(tmp_path):
    cfg = load_config(write_config(
        tmp_path,
        "gunb:\n  voivodeships: ['16']\n"
        "http:\n  circuit_breaker_failures: 5\n  circuit_breaker_cooldown: 120\n"
        "telegram:\n  admin_chat_id: ${ADMIN}\n"
        "storage:\n  db_path: baza/leady.sqlite\n  backup_dir: kopie\n  backup_every_days: 1\n"
        "  backup_keep: 2\n  vacuum_threshold: 50\n",
    ), env={"ADMIN": "-100777"})
    assert (cfg.http.circuit_breaker_failures, cfg.http.circuit_breaker_cooldown) == (5, 120.0)
    assert cfg.telegram.admin_chat_id == "-100777"
    assert cfg.storage.backup_dir == tmp_path / "kopie"
    assert (cfg.storage.backup_every_days, cfg.storage.backup_keep, cfg.storage.vacuum_threshold) == (1, 2, 50)


def test_repository_config_retries_each_request_three_times():
    assert load_config(REPO_ROOT / "config.yaml", env={}).http.max_retries == 3


# --- Paywall: administrator i kontakt -----------------------------------------------------------------

PAYWALL_YAML = ("gunb:\n  voivodeships: ['16']\ntelegram:\n  chat_id: ${TELEGRAM_CHAT_ID}\n"
                "bot:\n  admins: ['${ADMIN_CHAT_ID}']\n  admin_contact: ${ADMIN_CONTACT}\n")


def test_admin_chat_id_sets_the_bot_admin(tmp_path):
    cfg = load_config(write_config(tmp_path, PAYWALL_YAML), env={"ADMIN_CHAT_ID": "777", "TELEGRAM_CHAT_ID": "555"})
    assert cfg.bot.admins == (777,)


def test_without_admin_chat_id_the_owner_chat_is_admin(tmp_path):
    cfg = load_config(write_config(tmp_path, PAYWALL_YAML), env={"TELEGRAM_CHAT_ID": "555"})
    assert cfg.bot.admins == (555,)


@pytest.mark.parametrize("value, expected", [("jan_kowalski", "@jan_kowalski"), ("@jan_kowalski", "@jan_kowalski"),
                                             ("", "")])
def test_admin_contact_is_normalised_to_telegram_nick(tmp_path, value, expected):
    cfg = load_config(write_config(tmp_path, PAYWALL_YAML), env={"ADMIN_CONTACT": value})
    assert cfg.bot.admin_contact == expected


def test_repository_config_backs_up_daily_for_two_weeks():
    storage = load_config(REPO_ROOT / "config.yaml", env={}).storage
    assert (storage.backup_every_days, storage.backup_keep) == (1, 14)
