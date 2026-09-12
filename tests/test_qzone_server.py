from __future__ import annotations

from dl_senpai.config import PluginConfig
from dl_senpai.qzone_server import parse_bridge_url


def test_parse_bridge_url() -> None:
    assert parse_bridge_url("http://127.0.0.1:5700") == ("127.0.0.1", 5700)
    assert parse_bridge_url("") == ("127.0.0.1", 5700)


def test_should_autostart_qzone() -> None:
    on = PluginConfig(DL_SENPAI_QZONE_ENABLE=True, DL_SENPAI_QZONE_AUTOSTART=True)
    assert on.should_autostart_qzone() is True
    off = PluginConfig(DL_SENPAI_QZONE_ENABLE=True, DL_SENPAI_QZONE_AUTOSTART=False)
    assert off.should_autostart_qzone() is False
    assert PluginConfig(DL_SENPAI_QZONE_ENABLE=False).should_autostart_qzone() is False


def test_qzone_home_default() -> None:
    cfg = PluginConfig()
    assert cfg.qzone_home_path().name == "onebot-qzone"
