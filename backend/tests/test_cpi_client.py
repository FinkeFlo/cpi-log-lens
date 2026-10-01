"""CPI client against the fake CPI server: token, file list, download, retries and errors."""

import gzip

import httpx
import pytest

from app import cpi
from app.cpi.client import CpiClient, RemoteLogFile, epoch_ms
from app.cpi.fake import FAKE_URL, FakeCpi, FakeFile, sample_cpi
from tests.support import numbered_lines

pytestmark = pytest.mark.anyio


def client_for(fake: FakeCpi | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> CpiClient:
    transport = transport or httpx.ASGITransport(app=(fake or FakeCpi()).app)
    return CpiClient(FAKE_URL, f"{FAKE_URL}/oauth/token", "id", "secret", transport=transport, retry_backoff=0)


async def test_token_list_and_download(tmp_path):
    fake = FakeCpi()
    fake.add("a.log", numbered_lines(3), last_modified_ms=1786543619000, application="app1")
    fake.add("h.log", numbered_lines(1), log_type="http")
    async with client_for(fake) as client:
        assert await client.get_token() == "fake-token"
        files = await client.list_files("trace")
        assert files == [RemoteLogFile("a.log", "app1", len(fake.files["a.log"].content), 1786543619000)]
        received = await client.download(files[0], tmp_path / "a.log")
    assert received == len(fake.files["a.log"].content)
    # Stored gzip-compressed, whatever the server sent.
    assert gzip.decompress((tmp_path / "a.log").read_bytes()) == fake.files["a.log"].content
    assert fake.requests == ["token", "list", "download a.log"]


async def test_gzip_bodies_are_stored_as_they_are(tmp_path):
    fake = FakeCpi()
    fake.files["g.log"] = FakeFile(content=gzip.compress(b"already compressed\n"))
    async with client_for(fake) as client:
        await client.get_token()
        [f] = await client.list_files("trace")
        await client.download(f, tmp_path / "g.log")
    assert gzip.decompress((tmp_path / "g.log").read_bytes()) == b"already compressed\n"


async def test_names_are_quoted_in_download_urls(tmp_path):
    fake = FakeCpi()
    fake.add("odd name+1.log", numbered_lines(1), application="App/One")
    async with client_for(fake) as client:
        await client.get_token()
        [f] = await client.list_files("trace")
        await client.download(f, tmp_path / "x.log")
    assert fake.requests[-1] == "download odd name+1.log"


async def test_calls_need_a_token_first():
    async with client_for() as client:
        with pytest.raises(RuntimeError, match="get_token"):
            await client.list_files("trace")


async def test_rejected_token_request_is_not_retried():
    fake = FakeCpi(token_status=401)
    async with client_for(fake) as client:
        with pytest.raises(httpx.HTTPStatusError) as e:
            await client.get_token()
    assert e.value.response.status_code == 401
    assert fake.requests == ["token"]


async def test_server_errors_are_retried_up_to_three_times(tmp_path):
    fake = FakeCpi()
    fake.add("a.log", numbered_lines(1))
    fake.download_status["a.log"] = [503, 429]
    async with client_for(fake) as client:
        await client.get_token()
        [f] = await client.list_files("trace")
        await client.download(f, tmp_path / "a.log")
    assert fake.requests.count("download a.log") == 3

    fake.list_status = 500
    async with client_for(fake) as client:
        await client.get_token()
        with pytest.raises(httpx.HTTPStatusError):
            await client.list_files("trace")
    assert fake.requests.count("list") == 1 + 3


async def test_failed_download_leaves_no_file(tmp_path):
    fake = FakeCpi()
    fake.add("a.log", numbered_lines(1))
    fake.download_status["a.log"] = [404]
    async with client_for(fake) as client:
        await client.get_token()
        [f] = await client.list_files("trace")
        with pytest.raises(httpx.HTTPStatusError):
            await client.download(f, tmp_path / "a.log")
    assert list(tmp_path.iterdir()) == []
    assert fake.requests.count("download a.log") == 1


async def test_timeouts_and_connection_errors_are_retried():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        if len(attempts) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        if len(attempts) == 2:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"access_token": "t"})

    async with client_for(transport=httpx.MockTransport(handler)) as client:
        assert await client.get_token() == "t"
    assert attempts == ["/oauth/token"] * 3


@pytest.mark.parametrize(
    ("value", "ms"),
    [("/Date(1786543619000)/", 1786543619000), ("", 0), (None, 0), ("garbage", 0)],
)
def test_epoch_ms(value, ms):
    assert epoch_ms(value) == ms


def test_list_entries_tolerate_missing_fields():
    assert RemoteLogFile.from_odata({}) == RemoteLogFile("", "", 0, 0)


async def test_demo_tenant_and_mock_mode_use_the_sample(settings, monkeypatch, tmp_path):
    demo = {"api_url": "demo://sample", "oauth_url": "demo://sample", "client_id": "d", "client_secret": "d"}
    real = {"api_url": "https://cpi.example", "oauth_url": "https://auth.example/t", "client_id": "c"}
    real["client_secret"] = "s"
    async with cpi.client_for(demo) as client:
        assert client.api_url == FAKE_URL
        await client.get_token()
        files = await client.list_files("trace")
        assert [f.name for f in files] == sorted(sample_cpi().files)
        assert await client.list_files("http") == []
    async with cpi.client_for(real) as client:
        assert client.api_url == "https://cpi.example"
    monkeypatch.setattr(settings, "mock", True)
    async with cpi.client_for(real) as client:
        assert client.api_url == FAKE_URL
