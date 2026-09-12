from __future__ import annotations

from pathlib import Path

from dl_senpai.draw_store import DrawStore


def test_archive_from_cache() -> None:
    base = Path("data/test_tmp/draw_store")
    if base.exists():
        import shutil

        shutil.rmtree(base)
    store = DrawStore(base, max_per_session=10)
    cache = base / "cache.png"
    cache.write_bytes(b"fake-png-bytes")

    archived = store.archive_from_cache(
        "private:123",
        cache_path=cache,
        prompt="学姐自拍，夏日橙风格",
        scene="selfie",
        user_request="画张自拍",
        source_user="失语",
        source_user_id="2843422418",
        model="gpt-image-1",
        size="1024x1024",
    )
    assert archived is not None
    assert archived.is_file()
    assert archived.parent.name == "files"

    items = store._load_raw("private:123")
    assert len(items) == 1
    assert items[0]["prompt"] == "学姐自拍，夏日橙风格"
    assert items[0]["scene"] == "selfie"
    assert items[0]["source_user"] == "失语"
    assert items[0]["source_user_id"] == "2843422418"
