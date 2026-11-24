"""The server keeps the Kroger sign-in alive, and says so when it cannot"""

import json
import logging
import time

import anyio
import pytest
import requests
from fastmcp import Client

from kroger_api.token_storage import load_token, save_token
from kroger_mcp.server import create_server
from kroger_mcp.tools import auth_tools, shared, token_refresh
from kroger_mcp.tools.signin_status import fingerprint, status
from kroger_mcp.tools.token_refresh import refresh_interval, refresh_once, run_refresh_loop

from conftest import BASE, TOKEN_URL, call_error, call_ok, save_user_token

NEW_TOKEN = {"access_token": "renewed", "refresh_token": "renewed-refresh", "expires_in": 1800, "token_type": "bearer"}
REJECTED = {"error": "invalid_grant", "error_description": "invalid refresh_token"}
SECRETS = ("OLD-access-9f3a", "OLD-refresh-9f3a", "NEW-access-9f3a", "NEW-refresh-9f3a")


class StopLoop(Exception):
    """Raised by the fake sleep to end a loop that would never end"""


def fake_sleep(calls, limit, on_call=None):
    async def sleep(seconds):
        calls.append(seconds)
        if on_call:
            on_call(len(calls))
        if len(calls) >= limit:
            raise StopLoop
    return sleep


def aging_token(**overrides):
    """A saved sign-in that is half way through its life, so it is due for renewal"""
    return save_user_token(expires_at=time.time() + 600, **overrides)


@pytest.fixture(autouse=True)
def fresh_status():
    status.reset()
    yield
    status.reset()


# refresh_once

def test_refresh_once_renews_and_saves_the_token(requests_mock):
    aging_token()
    requests_mock.post(TOKEN_URL, json=NEW_TOKEN)
    assert refresh_once() == "refreshed"
    saved = load_token(shared.USER_TOKEN_FILE)
    assert saved["access_token"] == "renewed" and saved["refresh_token"] == "renewed-refresh"


def test_refresh_once_keeps_the_old_refresh_token_when_kroger_sends_none(requests_mock):
    aging_token()
    requests_mock.post(TOKEN_URL, json={"access_token": "renewed", "expires_in": 1800, "token_type": "bearer"})
    refresh_once()
    assert load_token(shared.USER_TOKEN_FILE)["refresh_token"] == "refresh"


def test_refresh_once_uses_a_token_that_was_just_renewed(requests_mock):
    save_user_token()  # issued just now: a full 1800 seconds left
    assert refresh_once() == "reused"
    assert requests_mock.call_count == 0


def test_refresh_once_without_a_saved_token(requests_mock):
    assert refresh_once() == "no_token"
    assert requests_mock.call_count == 0


def test_refresh_once_drops_the_cached_user_client(requests_mock, monkeypatch):
    aging_token()
    requests_mock.post(TOKEN_URL, json=NEW_TOKEN)
    monkeypatch.setattr(shared, "_authenticated_client", object())
    refresh_once()
    assert shared._authenticated_client is None


# the loop

async def test_loop_refreshes_at_startup_and_then_every_interval(requests_mock):
    aging_token()
    requests_mock.post(TOKEN_URL, json=NEW_TOKEN)
    calls = []
    with pytest.raises(StopLoop):
        await run_refresh_loop(900, sleep=fake_sleep(calls, 2, on_call=lambda n: aging_token()))
    assert calls == [900, 900]
    assert requests_mock.call_count == 2
    assert status.state == "ok" and status.last_success is not None


async def test_loop_retries_a_network_error_three_times_then_waits_for_the_next_cycle(requests_mock):
    aging_token()
    token = requests_mock.post(TOKEN_URL, exc=requests.exceptions.ConnectionError("Network is unreachable"))
    calls = []
    with pytest.raises(StopLoop):
        await run_refresh_loop(900, sleep=fake_sleep(calls, 3))
    assert token.call_count == 3
    assert calls[2] == 900 and calls[0] < 900 and calls[1] < 900  # two backoffs, then the interval
    assert status.state == "retrying"
    assert "Network is unreachable" in status.reason or "network" in status.reason.lower()


async def test_loop_stops_when_kroger_rejects_the_refresh_token_and_resumes_after_a_sign_in(requests_mock):
    aging_token()
    token = requests_mock.post(TOKEN_URL, status_code=400, json=REJECTED)
    calls = []

    def sign_in_after_a_while(n):
        if n == 3:
            token.reset()
            requests_mock.post(TOKEN_URL, json=NEW_TOKEN)
            save_user_token(expires_at=time.time() + 600)
            status.sign_in_completed()

    with pytest.raises(StopLoop):
        await run_refresh_loop(900, sleep=fake_sleep(calls, 4, on_call=sign_in_after_a_while))
    # Parked while the sign-in was dead: waiting, not asking Kroger again
    assert status.state == "ok"
    assert calls[:3] == [token_refresh.PARK_POLL_SECONDS] * 3
    assert calls[3] == 900


async def test_a_rejected_refresh_token_is_recorded_with_kroger_reason_and_logged_once(requests_mock, caplog):
    aging_token()
    requests_mock.post(TOKEN_URL, status_code=400, json=REJECTED)
    calls = []
    with caplog.at_level(logging.INFO), pytest.raises(StopLoop):
        await run_refresh_loop(900, sleep=fake_sleep(calls, 4))
    assert status.state == "signin_needed"
    assert "invalid refresh_token" in status.reason and status.since is not None
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR and r.name.startswith("kroger_mcp")]
    assert len(errors) == 1


def test_a_network_error_does_not_hide_a_dead_sign_in():
    status.mark_signin_needed("invalid refresh_token")
    status.mark_retrying("APIConnectionError: Network is unreachable")
    assert status.state == "signin_needed"
    assert status.reason == "invalid refresh_token"


async def test_a_dead_sign_in_found_by_a_cart_call_stays_dead_through_a_network_error_in_the_loop(requests_mock):
    aging_token()
    status.mark_signin_needed("invalid refresh_token")
    requests_mock.post(TOKEN_URL, exc=requests.exceptions.ConnectionError("Network is unreachable"))
    calls = []
    with pytest.raises(StopLoop):
        await run_refresh_loop(900, sleep=fake_sleep(calls, 3))
    assert calls == [*token_refresh.RETRY_BACKOFF_SECONDS, token_refresh.PARK_POLL_SECONDS]
    assert status.state == "signin_needed"


# a sign-in dead in this process that is alive on disk, and results that arrive late

async def test_a_parked_loop_resumes_when_another_process_signs_in(requests_mock):
    aging_token(access_token="rejected-access")
    requests_mock.post(TOKEN_URL, status_code=400, json=REJECTED)
    calls = []

    def another_process_signs_in(n):
        if n == 2:
            save_user_token(access_token="new-access", expires_at=time.time() + 600)
            requests_mock.post(TOKEN_URL, json=NEW_TOKEN)

    with pytest.raises(StopLoop):
        await run_refresh_loop(900, sleep=fake_sleep(calls, 3, on_call=another_process_signs_in))
    assert calls == [token_refresh.PARK_POLL_SECONDS, token_refresh.PARK_POLL_SECONDS, 900]
    assert status.state == "ok"


async def test_get_authentication_info_heals_when_the_saved_token_is_not_the_rejected_one(mcp, requests_mock):
    status.mark_signin_needed("invalid refresh_token", token_fingerprint=fingerprint("rejected-access"))
    save_user_token(access_token="new-access")
    info = await call_ok(mcp, "get_authentication_info")
    assert info["authenticated"] is True
    assert info["refresh"]["state"] != "signin_needed"


async def test_get_authentication_info_stays_dead_while_the_rejected_token_is_still_saved(mcp, requests_mock):
    status.mark_signin_needed("invalid refresh_token", token_fingerprint=fingerprint("user"))
    save_user_token(access_token="user")
    info = await call_ok(mcp, "get_authentication_info")
    assert info["authenticated"] is False


def test_a_cart_call_finding_a_new_sign_in_clears_signin_needed():
    status.mark_signin_needed("invalid refresh_token", token_fingerprint=fingerprint("rejected-access"))
    save_user_token(access_token="new-access")
    shared.get_authenticated_client()
    assert status.state != "signin_needed"


def test_a_result_that_arrives_after_a_new_sign_in_is_dropped():
    generation = status.generation
    status.sign_in_completed()
    status.mark_signin_needed("invalid refresh_token", generation=generation)
    assert status.state == "unknown"


def test_a_result_that_arrives_after_a_sign_out_is_dropped():
    generation = status.generation
    status.mark_signed_out()
    status.mark_ok(generation=generation)
    status.mark_retrying("late", generation=generation)
    assert status.state == "signed_out"


async def test_loop_survives_a_missing_token_and_keeps_waiting(requests_mock):
    calls = []
    with pytest.raises(StopLoop):
        await run_refresh_loop(900, sleep=fake_sleep(calls, 2))
    assert status.state == "signed_out"
    assert calls == [900, 900]


async def test_loop_survives_a_token_without_a_refresh_token(requests_mock):
    save_token({"access_token": "user", "expires_in": 1800, "expires_at": time.time() + 600},
               shared.USER_TOKEN_FILE)
    calls = []
    with pytest.raises(StopLoop):
        await run_refresh_loop(900, sleep=fake_sleep(calls, 2))
    assert status.state == "signin_needed"
    assert "refresh token" in status.reason


async def test_nothing_secret_is_logged_or_reported(requests_mock, caplog):
    aging_token(access_token="OLD-access-9f3a", refresh_token="OLD-refresh-9f3a")
    requests_mock.post(TOKEN_URL, json={**NEW_TOKEN, "access_token": "NEW-access-9f3a",
                                        "refresh_token": "NEW-refresh-9f3a"})
    calls = []
    with caplog.at_level(logging.DEBUG), pytest.raises(StopLoop):
        await run_refresh_loop(900, sleep=fake_sleep(calls, 1))
    text = caplog.text + json.dumps(status.as_dict())
    assert "NEW-access-9f3a" in json.dumps(load_token(shared.USER_TOKEN_FILE))  # the renewal did happen
    for secret in SECRETS:
        assert secret not in text


# settings

def test_interval_defaults_to_900_seconds(monkeypatch):
    monkeypatch.delenv("KROGER_REFRESH_INTERVAL_SECONDS", raising=False)
    assert refresh_interval() == 900


def test_interval_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("KROGER_REFRESH_INTERVAL_SECONDS", "120")
    assert refresh_interval() == 120
    monkeypatch.setenv("KROGER_REFRESH_INTERVAL_SECONDS", "0")
    assert refresh_interval() == 0


@pytest.mark.parametrize("value", ["soon", "-5", ""])
def test_a_bad_interval_falls_back_to_900_with_a_warning(monkeypatch, caplog, value):
    monkeypatch.setenv("KROGER_REFRESH_INTERVAL_SECONDS", value)
    with caplog.at_level(logging.WARNING):
        assert refresh_interval() == 900
    if value:
        assert "KROGER_REFRESH_INTERVAL_SECONDS" in caplog.text


# the status in the tools

async def test_get_authentication_info_reports_the_refresh_status(mcp, requests_mock):
    save_user_token()
    status.mark_ok()
    info = await call_ok(mcp, "get_authentication_info")
    assert info["authenticated"] is True
    assert info["refresh"]["state"] == "ok"


async def test_get_authentication_info_is_not_authenticated_when_the_sign_in_is_dead(mcp, requests_mock):
    save_user_token()  # the access token still works for a while
    status.mark_signin_needed("invalid refresh_token")
    info = await call_ok(mcp, "get_authentication_info")
    assert info["authenticated"] is False
    assert info["refresh"]["state"] == "signin_needed"
    assert "start_authentication" in info["message"]


async def test_a_rejected_refresh_token_found_by_a_cart_call_sets_signin_needed(mcp, requests_mock):
    save_user_token(expires_at=time.time() - 10)
    requests_mock.post(TOKEN_URL, status_code=400, json=REJECTED)
    await call_error(mcp, "add_items_to_cart", {"product_id": "0001111041700"})
    assert status.state == "signin_needed"
    assert "invalid refresh_token" in status.reason


async def test_signing_out_resets_the_status(mcp, requests_mock):
    save_user_token()
    status.mark_signin_needed("invalid refresh_token")
    await call_ok(mcp, "force_reauthenticate")
    assert status.state == "signed_out"


async def test_completing_a_sign_in_clears_signin_needed(mcp, requests_mock):
    await call_ok(mcp, "start_authentication")
    status.mark_signin_needed("invalid refresh_token")
    requests_mock.post(TOKEN_URL, json=NEW_TOKEN)
    await call_ok(mcp, "complete_authentication", {
        "redirect_url": f"http://localhost:8000/callback?code=abc&state={auth_tools._auth_state}"})
    assert status.state != "signin_needed"


# the server

async def test_the_server_runs_the_loop_while_it_is_up_and_stops_it_at_shutdown(requests_mock, monkeypatch):
    monkeypatch.setenv("KROGER_REFRESH_INTERVAL_SECONDS", "3600")
    aging_token()
    token = requests_mock.post(TOKEN_URL, json=NEW_TOKEN)
    async with Client(create_server()):
        with anyio.fail_after(5):
            while token.call_count < 1:
                await anyio.sleep(0.05)
    assert token.call_count == 1
    assert status.state == "ok"


async def test_an_interval_of_zero_starts_no_loop(requests_mock, monkeypatch):
    monkeypatch.setenv("KROGER_REFRESH_INTERVAL_SECONDS", "0")
    aging_token()
    token = requests_mock.post(TOKEN_URL, json=NEW_TOKEN)
    async with Client(create_server()):
        await anyio.sleep(0.2)
    assert token.call_count == 0
