"""Readable descriptions of failed CPI requests.

httpx reports failures with technical texts such as "[Errno 8] nodename nor servname
provided, or not known" or "Client error '401 Unauthorized' for url …". describe() turns
them into one sentence about what failed and a hint about what to check, for the
connection test and the per-tenant errors of a fetch. Callers log the original exception."""

import socket
import ssl
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import httpx

# token: the OAuth token request (OAuth URL); api: a call to the LogFiles API (API URL).
Step = Literal["token", "api"]

_FIELD = {"token": "OAuth URL", "api": "API URL"}
_SERVER = {"token": "The OAuth server", "api": "The CPI API"}
_USE_TOKEN_URL = "Use the token URL from the service key (tokenurl), which usually ends with /oauth/token."
_USE_API_URL = "Use the API URL from the service key (url), without a path."
_USE_URL = {"token": _USE_TOKEN_URL, "api": _USE_API_URL}


@dataclass(frozen=True)
class CpiProblem:
    """Why a CPI request failed, for people."""

    # unreachable | timeout | tls | invalid_url | invalid_credentials | token_rejected | missing_role |
    # not_found | redirect | rate_limited | server_error | unexpected_response | failed
    kind: str
    message: str  # what failed, one sentence
    hint: str = ""  # what to check
    status: int | None = None  # HTTP status of the answer, if there was one

    def text(self) -> str:
        return f"{self.message} {self.hint}".strip()


def _causes(exc: BaseException) -> list[BaseException]:
    """The exception and the ones it was raised from (httpx wraps the socket and TLS errors)."""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _find(exc: BaseException, kind: type[BaseException]) -> BaseException | None:
    return next((e for e in _causes(exc) if isinstance(e, kind)), None)


def _host(url: str) -> str:
    """The host name of a URL, or the URL itself if it has none or can't be parsed."""
    try:
        return urlsplit(url).hostname or url
    except ValueError:  # e.g. "https://[::1/x", which the tenant URL pattern lets through
        return url


def describe(exc: BaseException, step: Step, url: str) -> CpiProblem:
    """Describe an exception of a CPI request to `url` (the OAuth URL for the token step,
    the API URL for API calls). Never raises: it runs while handling the original error."""
    try:
        return _describe(exc, step, url)
    except Exception:
        return CpiProblem("failed", f"Unexpected error ({type(exc).__name__}).", "The server log has the details.")


def _describe(exc: BaseException, step: Step, url: str) -> CpiProblem:
    host = _host(url)
    field = _FIELD[step]
    if isinstance(exc, httpx.InvalidURL):
        return CpiProblem("invalid_url", f"The {field} is not a valid URL.", _USE_URL[step])
    if isinstance(exc, httpx.HTTPStatusError):
        return _describe_status(exc.response.status_code, step, host)
    if isinstance(exc, httpx.TimeoutException):
        return CpiProblem(
            "timeout",
            f"{host} did not answer in time.",
            f"Check the {field} and that this machine can reach it (proxy, firewall).",
        )
    if isinstance(exc, httpx.ConnectError):
        if _find(exc, socket.gaierror):
            return CpiProblem("unreachable", f"Can't reach {host}: the host name is unknown.", f"Check the {field}.")
        if _find(exc, ConnectionRefusedError):
            refused = f"Can't reach {host}: the connection was refused."
            return CpiProblem("unreachable", refused, f"Check the {field}, including the port.")
        tls = _find(exc, ssl.SSLError)
        if tls is not None:
            reason = getattr(tls, "verify_message", None) or getattr(tls, "reason", None) or "TLS error"
            return CpiProblem(
                "tls",
                f"The secure connection to {host} failed: {reason}.",
                f"Check the {field}. A proxy that inspects HTTPS traffic can cause this.",
            )
        return CpiProblem(
            "unreachable",
            f"Can't connect to {host}.",
            f"Check the {field} and that this machine can reach it (proxy, firewall).",
        )
    if isinstance(exc, httpx.TooManyRedirects):
        return CpiProblem("redirect", f"The {field} redirects too often.", _USE_URL[step])
    if isinstance(exc, httpx.RequestError):
        return CpiProblem(
            "unreachable",
            f"The connection to {host} failed ({type(exc).__name__}).",
            "Try again; if it keeps failing, check the network (proxy, firewall).",
        )
    if isinstance(exc, (ValueError, KeyError, TypeError, AttributeError)):
        # The answer was not the JSON the client expects (e.g. a login page).
        unexpected = {
            "token": "The OAuth URL did not return an access token.",
            "api": "The API URL did not answer like the CPI LogFiles API.",
        }
        return CpiProblem("unexpected_response", unexpected[step], _USE_URL[step])
    return CpiProblem("failed", f"Unexpected error ({type(exc).__name__}).", "The server log has the details.")


def _describe_status(status: int, step: Step, host: str) -> CpiProblem:
    server, field = _SERVER[step], _FIELD[step]
    http = f"(HTTP {status})"
    if 300 <= status < 400:
        return CpiProblem("redirect", f"The {field} redirects to another address {http}.", _USE_URL[step], status)
    if status == 401 and step == "token":
        return CpiProblem(
            "invalid_credentials",
            f"The OAuth server rejected the client ID or secret {http}.",
            "Copy the client ID and secret from the service key again.",
            status,
        )
    if status == 401:
        return CpiProblem(
            "token_rejected",
            f"The CPI API did not accept the token {http}.",
            "Check that the API URL and the OAuth URL come from the same service key.",
            status,
        )
    if status == 403 and step == "api":
        return CpiProblem(
            "missing_role",
            f"The credentials work, but they may not read log files {http}.",
            "Assign a role that allows reading log files to the service instance of this service key "
            "(see the SAP documentation of the LogFiles API).",
            status,
        )
    if status == 404:
        if step == "token":
            return CpiProblem("not_found", f"The OAuth URL was not found {http}.", _USE_URL[step], status)
        return CpiProblem("not_found", f"The API URL does not offer the LogFiles API {http}.", _USE_URL[step], status)
    if status == 429:
        return CpiProblem("rate_limited", f"{host} is limiting requests {http}.", "Try again in a few minutes.", status)
    if status >= 500:
        return CpiProblem("server_error", f"{server} had an internal error {http}.", "Try again later.", status)
    hint = "Check the OAuth URL and the client ID." if step == "token" else f"Check the {field}."
    return CpiProblem("failed", f"{server} refused the request {http}.", hint, status)
