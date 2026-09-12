from __future__ import annotations

from dl_senpai.friend_add_rpa import Calibration, build_deeplink, load_progress, save_progress


def test_build_deeplink_modes() -> None:
    u = build_deeplink("2480465844", "deeplink")
    assert u.startswith("tencent://ntqq-open?")
    assert "2480465844" in u
    a = build_deeplink("2480465844", "addcontact")
    assert a.startswith("tencent://AddContact/")
    assert "uin=2480465844" in a


def test_calibration_roundtrip(tmp_path, monkeypatch) -> None:
    import dl_senpai.friend_add_rpa as rpa

    path = tmp_path / "cal.json"
    monkeypatch.setattr(rpa, "_CALIB_PATH", path)
    c = Calibration(add_friend_btn=(10, 20), send_btn=(30, 40), verify_field=(1, 2))
    c.save(path)
    loaded = Calibration.load(path)
    assert loaded.add_friend_btn == (10, 20)
    assert loaded.send_btn == (30, 40)
    assert loaded.ready_for_clicks()


def test_progress_roundtrip(tmp_path, monkeypatch) -> None:
    import dl_senpai.friend_add_rpa as rpa

    monkeypatch.setattr(rpa, "_CAND_DIR", tmp_path)
    monkeypatch.setattr(rpa, "_PROGRESS_PATH", tmp_path / "rpa_progress.json")
    save_progress(["1", "2"], ["9"])
    p = load_progress()
    assert set(p["done"]) == {"1", "2"}
    assert p["failed"] == ["9"]
