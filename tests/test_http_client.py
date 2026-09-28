import json

import pytest
import requests

from gunb_tool.http_client import HttpError
from tests.fakes import FakeResponse, FakeTime, make_client

AGENTS = ("UA-one", "UA-two", "UA-three")


# --- request(): retry / backoff / Retry-After --------------------------------

def test_retries_server_errors_with_growing_backoff_until_success():
    client, session, clock = make_client([FakeResponse(503), FakeResponse(502), FakeResponse(200, "ok")])

    response = client.get("https://example.test/data")

    assert response.status_code == 200
    assert len(session.calls) == 3
    first, second = clock.sleeps
    assert 0.5 <= first <= 1.0  # „equal jitter”: połowa limitu + losowa reszta
    assert 1.0 <= second <= 2.0


def test_retries_connection_errors():
    client, session, _ = make_client([requests.ConnectionError("reset"), FakeResponse(200)])
    assert client.get("https://example.test/").status_code == 200
    assert len(session.calls) == 2


def test_raises_http_error_when_retries_are_exhausted():
    client, session, _ = make_client([FakeResponse(503)] * 4, max_retries=3)
    with pytest.raises(HttpError) as excinfo:
        client.get("https://example.test/")
    assert excinfo.value.status_code == 503
    assert len(session.calls) == 4


def test_returns_non_retryable_client_errors_without_retrying():
    client, session, _ = make_client([FakeResponse(404)])
    assert client.get("https://example.test/missing").status_code == 404
    assert len(session.calls) == 1


def test_honors_retry_after_header_in_seconds():
    client, _, clock = make_client([FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200)])
    client.get("https://example.test/")
    assert clock.sleeps == [7.0]


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": False, "error_code": 429, "parameters": {"retry_after": 3}},  # Telegram
        {"message": "You are being rate limited.", "retry_after": 3},  # Discord
    ],
)
def test_honors_retry_after_from_json_body(payload):
    client, _, clock = make_client([FakeResponse(429, json_data=payload), FakeResponse(200)])
    client.post("https://example.test/send", json={"text": "hi"})
    assert clock.sleeps == [3.0]


def test_secrets_in_urls_are_redacted_from_logs_and_errors(caplog):
    telegram = "https://api.telegram.org/bot123456:SECRET-token_x/sendMessage"
    discord = "https://discord.com/api/webhooks/987654/WEBHOOK-secret_y"
    client, _, _ = make_client([FakeResponse(503)] * 4 + [FakeResponse(503)] * 4, max_retries=3)

    with caplog.at_level("WARNING"):
        for url in (telegram, discord):
            with pytest.raises(HttpError) as excinfo:
                client.post(url, json={})
            assert "SECRET" not in str(excinfo.value) and "WEBHOOK-secret" not in str(excinfo.value)
            assert "SECRET" not in (excinfo.value.url or "")

    assert caplog.records, "ponowienia powinny być logowane"
    assert "SECRET" not in caplog.text
    assert "WEBHOOK-secret" not in caplog.text
    assert "api.telegram.org/bot<token>/sendMessage" in caplog.text


# --- User-Agent i uprzejme opóźnienia ----------------------------------------

def test_rotates_user_agent_from_pool_without_immediate_repeats():
    client, session, _ = make_client([FakeResponse(200)] * 12, user_agents=AGENTS)
    for _ in range(12):
        client.get("https://example.test/")
    agents = [call.headers["User-Agent"] for call in session.calls]
    assert set(agents) <= set(AGENTS)
    assert len(set(agents)) > 1
    assert all(a != b for a, b in zip(agents, agents[1:]))


def test_waits_random_polite_delay_between_consecutive_requests():
    client, _, clock = make_client([FakeResponse(200)] * 3, min_delay=1.0, max_delay=2.0)
    client.get("https://example.test/1")
    assert clock.sleeps == []  # pierwsze zapytanie bez czekania
    client.get("https://example.test/2")
    client.get("https://example.test/3")
    assert len(clock.sleeps) == 2
    assert all(1.0 <= s <= 2.0 for s in clock.sleeps)


def test_polite_delay_is_skipped_when_enough_time_has_passed():
    fake_time = FakeTime()
    client, _, _ = make_client([FakeResponse(200)] * 2, fake_time=fake_time, min_delay=1.0, max_delay=2.0)
    client.get("https://example.test/1")
    fake_time.now += 10
    client.get("https://example.test/2")
    assert fake_time.sleeps == []


# --- download(): warunkowe i wznawiane pobieranie ----------------------------

def test_download_writes_file_and_metadata(tmp_path):
    dest = tmp_path / "wynik.zip"
    client, _, _ = make_client(
        [FakeResponse(200, headers={"ETag": '"v1"', "Last-Modified": "Sun, 27 Sep 2026 21:35:36 GMT",
                                    "Content-Length": "6"}, chunks=[b"abc", b"def"])]
    )

    result = client.download("https://example.test/wynik.zip", dest)

    assert dest.read_bytes() == b"abcdef"
    assert result.not_modified is False
    assert result.etag == '"v1"'
    meta = json.loads((tmp_path / "wynik.zip.meta.json").read_text(encoding="utf-8"))
    assert meta["etag"] == '"v1"'
    assert meta["last_modified"] == "Sun, 27 Sep 2026 21:35:36 GMT"


def test_download_sends_conditional_headers_and_keeps_file_on_304(tmp_path):
    dest = tmp_path / "wynik.zip"
    dest.write_bytes(b"old")
    (tmp_path / "wynik.zip.meta.json").write_text(
        json.dumps({"etag": '"v1"', "last_modified": "Sun, 27 Sep 2026 21:35:36 GMT"}), encoding="utf-8"
    )
    client, session, _ = make_client([FakeResponse(304)])

    result = client.download("https://example.test/wynik.zip", dest)

    assert result.not_modified is True
    assert dest.read_bytes() == b"old"
    headers = session.calls[0].headers
    assert headers["If-None-Match"] == '"v1"'
    assert headers["If-Modified-Since"] == "Sun, 27 Sep 2026 21:35:36 GMT"


def test_download_without_cached_file_is_unconditional(tmp_path):
    (tmp_path / "wynik.zip.meta.json").write_text(json.dumps({"etag": '"v1"'}), encoding="utf-8")
    client, session, _ = make_client([FakeResponse(200, "data")])
    client.download("https://example.test/wynik.zip", tmp_path / "wynik.zip")
    assert "If-None-Match" not in session.calls[0].headers


def test_download_resumes_interrupted_stream_with_range_request(tmp_path):
    dest = tmp_path / "wynik.zip"
    client, session, _ = make_client(
        [
            FakeResponse(200, headers={"ETag": '"v2"', "Content-Length": "6"},
                         chunks=[b"abc", requests.exceptions.ChunkedEncodingError("przerwane")]),
            FakeResponse(206, headers={"Content-Range": "bytes 3-5/6"}, chunks=[b"def"]),
        ]
    )

    client.download("https://example.test/wynik.zip", dest)

    assert dest.read_bytes() == b"abcdef"
    resume_headers = session.calls[1].headers
    assert resume_headers["Range"] == "bytes=3-"
    assert resume_headers["If-Range"] == '"v2"'


def test_download_restarts_when_server_ignores_range(tmp_path):
    dest = tmp_path / "wynik.zip"
    client, _, _ = make_client(
        [
            FakeResponse(200, headers={"ETag": '"v2"', "Content-Length": "6"},
                         chunks=[b"abc", requests.ConnectionError("reset")]),
            FakeResponse(200, headers={"ETag": '"v2"', "Content-Length": "6"}, chunks=[b"abcdef"]),
        ]
    )
    client.download("https://example.test/wynik.zip", dest)
    assert dest.read_bytes() == b"abcdef"


def test_download_fails_on_persistently_truncated_body_and_keeps_old_file(tmp_path):
    dest = tmp_path / "wynik.zip"
    dest.write_bytes(b"old")
    truncated = [FakeResponse(200, headers={"Content-Length": "10"}, chunks=[b"abc"]) for _ in range(4)]
    client, _, _ = make_client(truncated, max_retries=3)

    with pytest.raises(HttpError):
        client.download("https://example.test/wynik.zip", dest)

    assert dest.read_bytes() == b"old"
    assert not (tmp_path / "wynik.zip.part").exists()


def test_download_retries_when_validator_rejects_file(tmp_path):
    dest = tmp_path / "wynik.zip"
    client, session, _ = make_client([FakeResponse(200, "broken"), FakeResponse(200, "good")])

    client.download("https://example.test/wynik.zip", dest, validator=lambda p: p.read_bytes() == b"good")

    assert dest.read_bytes() == b"good"
    assert len(session.calls) == 2
