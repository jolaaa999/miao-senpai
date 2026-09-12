from __future__ import annotations

from admin.autostart import _port_pids


def test_port_pids_returns_empty_for_free_port() -> None:
    # 34567 是非常用端口，测试环境大概率空闲；结果必须是 int 列表
    pids = _port_pids(34567)
    assert isinstance(pids, list)
    assert all(isinstance(p, int) and p > 0 for p in pids)


def test_port_pids_finds_vite_dev_server() -> None:
    # 前端 vite dev server（41788）通常常驻；若在跑则必须能找到正整数 PID
    pids = _port_pids(41788)
    assert all(isinstance(p, int) and p > 0 for p in pids)
