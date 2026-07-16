from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from app.config import load_config
from app.plugins.registry import register_builtins
from app.sync_engine import SyncEngine
from app.web.routes import _client_ip, set_engine


class _FakeClient:
    def __init__(self, host: str):
        self.host = host


class _FakeRequest:
    """Minimal stand-in for starlette.Request for _client_ip tests."""

    def __init__(self, peer: str, headers: dict[str, str]):
        self._client = _FakeClient(peer)
        self._headers = headers

    @property
    def client(self) -> _FakeClient | None:
        return self._client

    @property
    def headers(self) -> dict[str, str]:
        return self._headers


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[SyncEngine]:
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir()
    target = tmp_path / "data"
    target.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "sync": {
                    "interval": 3600,
                    "lock_dir": str(lock_dir),
                    "trusted_proxies": [
                        "172.20.0.0/16",
                        "10.0.0.0/8",
                        "127.0.0.0/8",
                    ],
                },
                "server": {"host": "127.0.0.1", "port": 8099},
                "plugins": [
                    {
                        "type": "local_dir",
                        "name": "Local",
                        "slug": "local",
                        "config": {"target": str(target)},
                    }
                ],
            }
        )
    )
    load_config(str(config_path))
    register_builtins()
    eng = SyncEngine()
    eng.start()
    set_engine(eng)
    yield eng
    eng.stop()


def test_is_trusted_proxy_defaults() -> None:
    from app.config import SyncConfig

    sc = SyncConfig({})
    assert sc.is_trusted_proxy("127.0.0.1")
    assert sc.is_trusted_proxy("192.168.1.5")
    assert not sc.is_trusted_proxy("203.0.113.4")


def test_direct_client_when_not_trusted(engine: SyncEngine) -> None:
    # Peer is a public IP (not a trusted proxy) → header ignored, peer returned
    req = _FakeRequest("203.0.113.4", {"x-forwarded-for": "198.51.100.7, 203.0.113.4"})
    assert _client_ip(req) == "203.0.113.4"


def test_trusted_proxy_x_forwarded_for(engine: SyncEngine) -> None:
    # Peer is the trusted proxy; real client is the leftmost (rightmost untrusted)
    req = _FakeRequest(
        "172.20.0.2",
        {"x-forwarded-for": "198.51.100.7, 10.0.0.1"},
    )
    assert _client_ip(req) == "198.51.100.7"


def test_trusted_proxy_skips_internal_hops(engine: SyncEngine) -> None:
    # Multiple internal hops before the real client
    req = _FakeRequest(
        "172.20.0.2",
        {"x-forwarded-for": "203.0.113.9, 172.20.0.9, 10.0.0.5"},
    )
    assert _client_ip(req) == "203.0.113.9"


def test_trusted_proxy_x_real_ip(engine: SyncEngine) -> None:
    req = _FakeRequest("172.20.0.2", {"x-real-ip": "198.51.100.23"})
    assert _client_ip(req) == "198.51.100.23"


def test_trusted_proxy_forwarded_rfc7239(engine: SyncEngine) -> None:
    req = _FakeRequest(
        "172.20.0.2",
        {"forwarded": "for=198.51.100.40; proto=https; host=example.com"},
    )
    assert _client_ip(req) == "198.51.100.40"


def test_trusted_proxy_ipv6_forwarded(engine: SyncEngine) -> None:
    req = _FakeRequest(
        "172.20.0.2",
        {"forwarded": "for=[2001:db8::1]:443"},
    )
    assert _client_ip(req) == "2001:db8::1"


def test_trusted_proxy_ipv6_xff_bare(engine: SyncEngine) -> None:
    # Bare IPv6 client in X-Forwarded-For must not be mangled by port-stripping.
    req = _FakeRequest(
        "172.20.0.2",
        {"x-forwarded-for": "2001:db8::42, 10.0.0.1"},
    )
    assert _client_ip(req) == "2001:db8::42"


def test_trusted_proxy_ipv6_xff_bracketed_port(engine: SyncEngine) -> None:
    req = _FakeRequest(
        "172.20.0.2",
        {"x-forwarded-for": "[2001:db8::99]:51234, 10.0.0.1"},
    )
    assert _client_ip(req) == "2001:db8::99"


def test_trusted_proxy_ipv6_x_real_ip(engine: SyncEngine) -> None:
    req = _FakeRequest("172.20.0.2", {"x-real-ip": "2001:db8::7"})
    assert _client_ip(req) == "2001:db8::7"


def test_ipv4_xff_strips_port(engine: SyncEngine) -> None:
    req = _FakeRequest(
        "172.20.0.2",
        {"x-forwarded-for": "198.51.100.7:44321, 10.0.0.1"},
    )
    assert _client_ip(req) == "198.51.100.7"


def test_trusted_proxy_all_hops_internal_falls_back(engine: SyncEngine) -> None:
    req = _FakeRequest(
        "172.20.0.2",
        {"x-forwarded-for": "172.20.0.9, 10.0.0.5"},
    )
    assert _client_ip(req) == "172.20.0.9"


def test_no_headers_returns_peer(engine: SyncEngine) -> None:
    req = _FakeRequest("172.20.0.2", {})
    assert _client_ip(req) == "172.20.0.2"
