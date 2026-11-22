from __future__ import annotations

import random

import pytest
import requests

from mathbot.config import HttpSettings
from mathbot.http import (
    HttpStatusError,
    PoliteClient,
    RobotsDisallowedError,
    RobotsRules,
    SourceBlockedError,
    TransportError,
    is_challenge,
)
from tests.conftest import FakeResponse, FakeSession


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(script, tmp_path=None, robots: str | int = 404, **overrides):
    settings = HttpSettings(**{"min_interval": 0.0, "backoff_base": 1.0, "max_retries": 3, **overrides})
    clock = Clock()
    session = FakeSession(script, robots)
    client = PoliteClient(settings, tmp_path, session=session, sleep=clock.sleep, clock=clock, rng=random.Random(0))
    return client, session, clock


URL = "https://example.org/page"


def test_success_sends_honest_user_agent():
    client, session, _ = make_client([FakeResponse(body=b"hello")], user_agent="math-contest-bot/test")
    assert client.get(URL).content == b"hello"
    assert session.requests[0][1]["User-Agent"] == "math-contest-bot/test"


def test_retries_transient_failures_with_exponential_backoff():
    client, session, clock = make_client(
        [FakeResponse(503), requests.ConnectionError("reset"), FakeResponse(500), FakeResponse(body=b"finally")]
    )
    assert client.get(URL).content == b"finally"
    assert len(session.requests) == 4
    # base 1s doubling, each with up to +50% jitter
    assert 1.0 <= clock.sleeps[0] <= 1.5 and 2.0 <= clock.sleeps[1] <= 3.0 and 4.0 <= clock.sleeps[2] <= 6.0


def test_retry_after_header_is_honoured():
    client, _, clock = make_client([FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse()])
    client.get(URL)
    assert 7.0 <= clock.sleeps[0] <= 7.5


def test_gives_up_after_max_retries_and_raises():
    client, session, _ = make_client([FakeResponse(503)] * 4)
    with pytest.raises(HttpStatusError) as excinfo:
        client.get(URL)
    assert excinfo.value.status == 503 and len(session.requests) == 4

    client, _, _ = make_client([requests.Timeout("slow")] * 4)
    with pytest.raises(TransportError):
        client.get(URL)


def test_client_errors_are_not_retried():
    client, session, clock = make_client([FakeResponse(404), FakeResponse()])
    with pytest.raises(HttpStatusError) as excinfo:
        client.get(URL)
    assert excinfo.value.status == 404 and len(session.requests) == 1 and clock.sleeps == []


def test_anti_bot_challenge_is_reported_not_retried():
    challenge = FakeResponse(403, b"<html><title>Just a moment...</title></html>", {"cf-mitigated": "challenge"})
    client, session, clock = make_client([challenge, FakeResponse()])
    with pytest.raises(SourceBlockedError):
        client.get(URL)
    assert len(session.requests) == 1 and clock.sleeps == []


def test_is_challenge_detection():
    html = {"content-type": "text/html"}
    assert is_challenge(403, {"Cf-Mitigated": "challenge"}, b"")
    assert is_challenge(403, html, b"<title>Just a moment...</title>")
    assert not is_challenge(403, html, b"<h1>Forbidden</h1>")
    assert not is_challenge(200, html, b"just a moment while I think")


def test_rate_limit_spaces_requests_to_the_same_host():
    client, _, clock = make_client([FakeResponse(), FakeResponse(), FakeResponse()], min_interval=2.0)
    for _ in range(3):
        client.get(URL, use_cache=False)
    # robots.txt is the first request to the host, so all three page requests wait.
    assert clock.sleeps == [2.0, 2.0, 2.0]


def test_cache_serves_fresh_entries_without_a_request(tmp_path):
    client, session, _ = make_client([FakeResponse(body=b"v1")], tmp_path)
    first = client.get(URL, {"a": 1})
    second = client.get(URL, {"a": 1})
    assert (first.content, first.from_cache) == (b"v1", False)
    assert (second.content, second.from_cache) == (b"v1", True)
    assert len(session.requests) == 1


def test_refresh_revalidates_with_etag_and_accepts_304(tmp_path):
    client, session, _ = make_client(
        [FakeResponse(body=b"v1", headers={"ETag": '"abc"'}), FakeResponse(304, b"")], tmp_path
    )
    client.get(URL)
    again = client.get(URL, refresh=True)
    assert again.content == b"v1" and again.from_cache
    assert session.requests[1][1]["If-None-Match"] == '"abc"'


def test_refresh_replaces_changed_content(tmp_path):
    client, _, _ = make_client([FakeResponse(body=b"v1"), FakeResponse(body=b"v2")], tmp_path)
    client.get(URL)
    assert client.get(URL, refresh=True).content == b"v2"
    assert client.get(URL).content == b"v2"


def test_errors_are_never_cached(tmp_path):
    client, _, _ = make_client([FakeResponse(404), FakeResponse(body=b"now ok")], tmp_path)
    with pytest.raises(HttpStatusError):
        client.get(URL)
    assert client.get(URL).content == b"now ok"


def test_seeded_cache_entry_is_served_offline(tmp_path):
    client, session, _ = make_client([], tmp_path)
    client.seed_cache(URL, b"<html>saved by hand</html>")
    assert client.get(URL).content == b"<html>saved by hand</html>"
    assert session.requests == []


AOPS_ROBOTS = """
User-agent: *
Allow: /
Disallow: /m/
Disallow: /*ajax
Disallow: /private/

User-agent: BadBot
Disallow: /
"""


def test_robots_disallow_blocks_the_request_before_it_is_sent():
    client, session, _ = make_client([FakeResponse()], robots=AOPS_ROBOTS)
    with pytest.raises(RobotsDisallowedError):
        client.get("https://example.org/m/community/ajax.php")
    assert session.requests == []
    assert client.get("https://example.org/wiki/api.php", {"action": "parse"}).status == 200


def test_robots_can_be_disabled_explicitly():
    client, _, _ = make_client([FakeResponse()], robots=AOPS_ROBOTS, respect_robots=False)
    assert client.get("https://example.org/m/x").status == 200


@pytest.mark.parametrize(
    "path, allowed",
    [
        ("/wiki/index.php/2024_AMC_10A_Problems", True),
        ("/wiki/api.php?action=parse", True),
        ("/m/community/ajax.php", False),
        ("/community/ajax.php", False),  # matched by /*ajax
        ("/private/x", False),
        ("/community/c3277", True),
    ],
)
def test_robots_wildcards(path, allowed):
    assert RobotsRules.parse(AOPS_ROBOTS).allowed("math-contest-bot/2.0", path) is allowed


def test_robots_specific_agent_group_and_anchors():
    rules = RobotsRules.parse(
        AOPS_ROBOTS + "\nUser-agent: *\nDisallow: /*.pdf$\nAllow: /core/*.css$\nDisallow: /core/\n"
    )
    assert rules.allowed("BadBot/1.0", "/wiki/") is False
    assert rules.allowed("math-contest-bot", "/files/a.pdf") is False
    assert rules.allowed("math-contest-bot", "/files/a.pdf?x=1") is True
    assert rules.allowed("math-contest-bot", "/core/theme.css") is True
    assert rules.allowed("math-contest-bot", "/core/theme.js") is False


def test_atomic_write_retries_while_another_process_holds_the_file(tmp_path, monkeypatch):
    """Windows refuses the rename while e.g. an antivirus scanner has the new file open."""
    import os

    from mathbot import http

    real_replace, calls = os.replace, []

    def held_twice(source, target):
        calls.append(source)
        if len(calls) <= 2:
            raise PermissionError(13, "Access is denied")
        real_replace(source, target)

    monkeypatch.setattr(http, "REPLACE_RETRY_DELAY", 0)
    monkeypatch.setattr(os, "replace", held_twice)
    http.atomic_write_bytes(tmp_path / "a.png", b"data")
    assert (tmp_path / "a.png").read_bytes() == b"data" and len(calls) == 3
    assert [p.name for p in tmp_path.iterdir()] == ["a.png"]

    def always_held(source, target):
        calls.append(source)
        raise PermissionError(13, "Access is denied")

    calls.clear()
    monkeypatch.setattr(os, "replace", always_held)
    with pytest.raises(PermissionError):
        http.atomic_write_bytes(tmp_path / "b.png", b"data")
    assert len(calls) == http.REPLACE_ATTEMPTS
    assert [p.name for p in tmp_path.iterdir()] == ["a.png"]  # no temp file left behind
