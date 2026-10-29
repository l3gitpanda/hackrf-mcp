import pytest
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from hackrf_mcp import server
from hackrf_mcp.auth import BearerTokenMiddleware, load_token

TOKEN = "test-token-0123456789abcdef"

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    },
}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def client():
    app = server.mcp.http_app(path="/mcp", middleware=[Middleware(BearerTokenMiddleware, token=TOKEN)])
    with TestClient(app) as c:
        yield c


def test_request_without_token_is_rejected(client):
    response = client.post("/mcp", json=INITIALIZE, headers=HEADERS)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_request_with_wrong_token_is_rejected(client):
    response = client.post("/mcp", json=INITIALIZE, headers={**HEADERS, "Authorization": "Bearer wrong"})

    assert response.status_code == 401


def test_request_with_token_initializes(client):
    response = client.post("/mcp", json=INITIALIZE, headers={**HEADERS, "Authorization": f"Bearer {TOKEN}"})

    assert response.status_code == 200
    assert "serverInfo" in response.text


def test_token_accepted_by_digest_alongside_plain_token():
    import hashlib

    second = "second-client-token-abcdef0123"
    digest = hashlib.sha256(second.encode()).digest()
    app = server.mcp.http_app(path="/mcp", middleware=[Middleware(BearerTokenMiddleware, token=TOKEN, digests=[digest])])

    with TestClient(app) as c:
        for token in (TOKEN, second):
            response = c.post("/mcp", json=INITIALIZE, headers={**HEADERS, "Authorization": f"Bearer {token}"})
            assert response.status_code == 200
        response = c.post("/mcp", json=INITIALIZE, headers={**HEADERS, "Authorization": f"Bearer {second}x"})
        assert response.status_code == 401


def test_load_token_digests_from_env(monkeypatch):
    import hashlib

    from hackrf_mcp.auth import load_token_digests

    monkeypatch.delenv("HACKRF_MCP_TOKEN_FILE", raising=False)
    monkeypatch.setenv("HACKRF_MCP_TOKEN", TOKEN)
    hex_digest = hashlib.sha256(b"another-token-0123456789").hexdigest()
    monkeypatch.setenv("HACKRF_MCP_TOKEN_SHA256", f" {hex_digest} ,")

    digests = load_token_digests()

    assert digests == [hashlib.sha256(TOKEN.encode()).digest(), bytes.fromhex(hex_digest)]


def test_load_token_digests_rejects_malformed_digest(monkeypatch):
    from hackrf_mcp.auth import load_token_digests

    monkeypatch.delenv("HACKRF_MCP_TOKEN", raising=False)
    monkeypatch.delenv("HACKRF_MCP_TOKEN_FILE", raising=False)
    monkeypatch.setenv("HACKRF_MCP_TOKEN_SHA256", "not-a-digest")

    with pytest.raises(ValueError):
        load_token_digests()


def test_non_bearer_scheme_is_rejected(client):
    response = client.post("/mcp", json=INITIALIZE, headers={**HEADERS, "Authorization": f"Basic {TOKEN}"})

    assert response.status_code == 401


def test_load_token_from_file(tmp_path, monkeypatch):
    monkeypatch.delenv("HACKRF_MCP_TOKEN", raising=False)
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")

    assert load_token(str(token_file)) == TOKEN


def test_load_token_rejects_short_token(monkeypatch):
    monkeypatch.delenv("HACKRF_MCP_TOKEN_FILE", raising=False)
    monkeypatch.setenv("HACKRF_MCP_TOKEN", "short")

    with pytest.raises(ValueError):
        load_token()


def test_http_transport_refuses_to_start_without_token(monkeypatch):
    monkeypatch.delenv("HACKRF_MCP_TOKEN", raising=False)
    monkeypatch.delenv("HACKRF_MCP_TOKEN_FILE", raising=False)
    monkeypatch.delenv("HACKRF_MCP_TOKEN_SHA256", raising=False)

    with pytest.raises(SystemExit):
        server.main(["--transport", "http"])
