"""Zmiana wybranych zmiennych w ``.env`` na serwerze (``gunb-admin sekrety``, ``gunb-admin monitor``)."""

import os
import stat
import sys

import pytest
from dotenv import dotenv_values

from gunb_tool.envfile import is_set, main, set_values

TOKEN = "123456789:AAH-synthetic_test_token_000000000000"


def test_changes_only_the_given_variables_and_keeps_the_rest(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# z pakietu migracji\nTELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=1111\nADMIN_CONTACT=@admin\n",
                   encoding="utf-8")

    set_values(env, {"TELEGRAM_BOT_TOKEN": TOKEN, "HEALTHCHECK_PING_URL": "https://hc-ping.com/abc"})

    assert env.read_text(encoding="utf-8").startswith("# z pakietu migracji\n")
    assert dotenv_values(env) == {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "1111",
                                  "ADMIN_CONTACT": "@admin", "HEALTHCHECK_PING_URL": "https://hc-ping.com/abc"}


def test_duplicate_lines_of_a_changed_variable_collapse_to_one(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_CHAT_ID=1\nTELEGRAM_CHAT_ID=2\n", encoding="utf-8")
    set_values(env, {"TELEGRAM_CHAT_ID": "3"})
    assert env.read_text(encoding="utf-8") == "TELEGRAM_CHAT_ID=3\n"


def test_creates_a_missing_file(tmp_path):
    env = tmp_path / ".env"
    set_values(env, {"TELEGRAM_CHAT_ID": "42"})
    assert dotenv_values(env) == {"TELEGRAM_CHAT_ID": "42"}


@pytest.mark.skipif(sys.platform == "win32", reason="prawa plików POSIX")
def test_file_is_readable_only_by_its_owner(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")
    os.chmod(env, 0o644)
    set_values(env, {"A": "2"})
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


@pytest.mark.parametrize("name, value", [("TELEGRAM_BOT_TOKEN", "abc\nEVIL=1"), ("zla nazwa", "1"), ("A", "x'y")])
def test_unsafe_names_and_values_are_refused_and_nothing_changes(tmp_path, name, value):
    env = tmp_path / ".env"
    env.write_text("A=1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        set_values(env, {name: value})
    assert env.read_text(encoding="utf-8") == "A=1\n"


def test_value_with_spaces_or_hash_survives_reading_back(tmp_path):
    env = tmp_path / ".env"
    set_values(env, {"ADMIN_CONTACT": "Jan #1 (biuro)"})
    assert dotenv_values(env)["ADMIN_CONTACT"] == "Jan #1 (biuro)"


def test_cli_takes_values_from_the_environment_not_from_arguments(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    monkeypatch.setenv("GUNB_SET_TELEGRAM_BOT_TOKEN", TOKEN)
    assert main(["ustaw", str(env)]) == 0
    assert dotenv_values(env)["TELEGRAM_BOT_TOKEN"] == TOKEN
    assert TOKEN not in capsys.readouterr().out  # sekret nie trafia na ekran ani do logu instalatora


def test_cli_tells_whether_a_variable_is_set_without_printing_it(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=\n", encoding="utf-8")
    assert is_set(env, "TELEGRAM_BOT_TOKEN") and not is_set(env, "TELEGRAM_CHAT_ID")
    assert main(["jest", str(env), "TELEGRAM_BOT_TOKEN"]) == 0
    assert main(["jest", str(env), "TELEGRAM_CHAT_ID"]) == 1
    assert main(["jest", str(tmp_path / "brak.env"), "TELEGRAM_BOT_TOKEN"]) == 1
    assert TOKEN not in capsys.readouterr().out
