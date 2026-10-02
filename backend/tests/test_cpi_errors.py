"""Readable descriptions of failed CPI requests (connection test, per-tenant fetch errors)."""

import json
import socket
import ssl

import httpx
import pytest

from app.cpi.client import CpiClient
from app.cpi.errors import describe
from app.cpi.fake import FAKE_URL, FakeCpi

pytestmark = pytest.mark.anyio

TOKEN_URL = "https://auth.example.test/oauth/token"
API_URL = "https://tenant.example.test"


def raised_from(exc: Exception, cause: BaseException) -> Exception:
    """`exc` raised from `cause`, the way httpx wraps socket and TLS errors."""
    try:
        raise exc from cause
    except Exception as e:
        return e


async def token_error(fake: FakeCpi) -> Exception:
    transport = httpx.ASGITransport(app=fake.app)
    async with CpiClient(FAKE_URL, f"{FAKE_URL}/oauth/token", "id", "s", transport=transport, retries=1) as client:
        with pytest.raises(Exception) as info:
            await client.get_token()
            await client.list_files("trace")
    return info.value


@pytest.mark.parametrize(
    ("token_status", "list_status", "step", "kind", "words"),
    [
        (401, 200, "token", "invalid_credentials", "rejected the client ID or secret (HTTP 401)"),
        (400, 200, "token", "failed", "refused the request (HTTP 400)"),
        (404, 200, "token", "not_found", "OAuth URL was not found (HTTP 404)"),
        (503, 200, "token", "server_error", "OAuth server had an internal error (HTTP 503)"),
        (200, 401, "api", "token_rejected", "did not accept the token (HTTP 401)"),
        (200, 403, "api", "missing_role", "may not read log files (HTTP 403)"),
        (200, 404, "api", "not_found", "does not offer the LogFiles API (HTTP 404)"),
        (200, 429, "api", "rate_limited", "is limiting requests (HTTP 429)"),
        (200, 500, "api", "server_error", "CPI API had an internal error (HTTP 500)"),
    ],
)
async def test_http_errors(token_status, list_status, step, kind, words):
    error = await token_error(FakeCpi(token_status=token_status, list_status=list_status))
    problem = describe(error, step, TOKEN_URL if step == "token" else API_URL)
    assert problem.kind == kind
    assert words in problem.message
    assert problem.hint
    assert problem.status == (token_status if step == "token" else list_status)
    assert "developer.mozilla.org" not in problem.text()


def test_redirect_of_the_token_request():
    request = httpx.Request("POST", TOKEN_URL)
    response = httpx.Response(302, headers={"Location": "https://login.example.test"}, request=request)
    error = httpx.HTTPStatusError("redirect", request=request, response=response)
    problem = describe(error, "token", TOKEN_URL)
    assert (problem.kind, problem.status) == ("redirect", 302)
    assert "tokenurl" in problem.hint


def test_unknown_host():
    error = raised_from(
        httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known"),
        socket.gaierror(8, "nodename nor servname provided, or not known"),
    )
    problem = describe(error, "token", TOKEN_URL)
    assert problem.kind == "unreachable"
    assert problem.message == "Can't reach auth.example.test: the host name is unknown."
    assert problem.hint == "Check the OAuth URL."
    assert "Errno" not in problem.text()


async def test_refused_connection():
    with socket.socket() as s:  # a local port nobody listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    async with CpiClient(url, f"{url}/oauth/token", "id", "s", retries=1, timeout=5) as client:
        with pytest.raises(httpx.ConnectError) as info:
            await client.get_token()
    problem = describe(info.value, "token", f"{url}/oauth/token")
    assert problem.kind == "unreachable"
    assert problem.message == "Can't reach 127.0.0.1: the connection was refused."


def test_untrusted_certificate():
    cause = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    cause.verify_message = "self-signed certificate"
    error = raised_from(httpx.ConnectError(str(cause)), cause)
    problem = describe(error, "api", API_URL)
    assert problem.kind == "tls"
    assert problem.message == "The secure connection to tenant.example.test failed: self-signed certificate."


def test_timeout():
    problem = describe(httpx.ConnectTimeout(""), "api", API_URL)
    assert problem.kind == "timeout"
    assert problem.message == "tenant.example.test did not answer in time."
    assert "API URL" in problem.hint


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ("token", "The OAuth URL did not return an access token."),
        ("api", "The API URL did not answer like the CPI LogFiles API."),
    ],
)
def test_answers_that_are_not_the_expected_json(step, message):
    error = raised_from(json.JSONDecodeError("Expecting value", "<html>", 0), ValueError())
    problem = describe(error, step, API_URL)
    assert (problem.kind, problem.message) == ("unexpected_response", message)


def test_anything_else():
    problem = describe(RuntimeError("boom"), "api", API_URL)
    assert (problem.kind, problem.message) == ("failed", "Unexpected error (RuntimeError).")
