from ainas.quality import build_release
from ainas.resource_preferences import profile_for, rank_releases, select_episode_files


def release(label: str, hash_char: str, *, original_language: str | None = None):
    value = build_release(
        "测试节目",
        label,
        "https://sixv.test/mj/2026-01-01/1.html",
        "magnet:?xt=urn:btih:" + hash_char * 40,
        "tv",
        metadata_original_language=original_language,
    )
    assert value is not None
    return value


def test_daily_profile_prioritizes_resolution_then_source_and_keeps_fallbacks():
    values = [
        release("S01E01 4K BluRay 国英双语", "a"),
        release("S01E01 1080p WEB-DL 英语", "b"),
        release("S01E01 1080p BluRay 西班牙语", "c", original_language="西班牙语"),
        release("S01E01 720p BluRay 多国语言", "d"),
    ]

    ranked = rank_releases(values, profile_for("daily"))

    assert [item.id for item in ranked] == [values[2].id, values[1].id, values[0].id, values[3].id]


def test_audio_priority_is_multilingual_then_original_then_chinese_english_then_other_unknown():
    values = [
        release("S01E01 1080p BluRay 法语", "a", original_language="西班牙语"),
        release("S01E01 1080p BluRay 英语", "b", original_language="西班牙语"),
        release("S01E01 1080p BluRay 西班牙语", "c", original_language="西班牙语"),
        release("S01E01 1080p BluRay 西英双语", "d", original_language="西班牙语"),
        release("S01E01 1080p BluRay", "e", original_language="西班牙语"),
    ]

    ranked = rank_releases(values, profile_for("daily"))

    assert [item.id for item in ranked] == [values[3].id, values[2].id, values[1].id, values[0].id, values[4].id]


def test_collection_and_compact_profiles_change_resolution_order_without_filtering():
    full_hd = release("S01E01 1080p BluRay", "a")
    four_k = release("S01E01 4K WEB-DL", "b")
    hd = release("S01E01 720p HDTV", "c")

    assert rank_releases([full_hd, four_k, hd], profile_for("collection"))[0] == four_k
    assert rank_releases([full_hd, four_k, hd], profile_for("compact"))[0] == hd
    assert len(rank_releases([full_hd, four_k, hd], profile_for("daily"))) == 3


def test_movie_daily_profile_defaults_to_4k_resolution_order():
    movie_profile = profile_for("daily", media_type="movie")
    assert movie_profile.resolution_order[0] == "2160p"
    assert movie_profile.resolution_order[1] == "1080p"

    tv_profile = profile_for("daily", media_type="tv")
    assert tv_profile.resolution_order[0] == "1080p"
    assert tv_profile.resolution_order[1] == "2160p"

    anime_profile = profile_for("daily", media_type="anime")
    assert anime_profile.resolution_order[0] == "1080p"
    assert anime_profile.resolution_order[1] == "2160p"


def test_episode_file_selection_downloads_only_missing_episodes_and_their_subtitles():
    files = [
        {"index": 0, "name": "Season 01/Show.S01E01.mkv", "size": 1000, "priority": 1},
        {"index": 1, "name": "Season 01/Show.S01E02.mkv", "size": 1000, "priority": 1},
        {"index": 2, "name": "Season 01/Show.S01E02.zh-CN.srt", "size": 10, "priority": 1},
        {"index": 3, "name": "poster.jpg", "size": 10, "priority": 1},
    ]

    result = select_episode_files(files, ["S01E02"])

    assert result["safe"] is True
    assert result["selected_indices"] == [1, 2]
    assert result["skipped_indices"] == [0, 3]
    assert result["matched_episodes"] == ["S01E02"]


def test_episode_file_selection_understands_sixv_numeric_prefix_names():
    files = [
        {
            "index": 0,
            "name": "重器.2160p/26.2160p.HD国语中字无水印[最新电影www.dyg7.com].mkv",
            "size": 1000,
            "priority": 1,
        },
        {
            "index": 1,
            "name": "重器.2160p/27.2160p.HD国语中字无水印[最新电影www.dyg7.com].mkv",
            "size": 1000,
            "priority": 1,
        },
    ]

    result = select_episode_files(files, ["S01E27"])

    assert result["safe"] is True
    assert result["selected_indices"] == [1]
    assert result["skipped_indices"] == [0]
    assert result["matched_episodes"] == ["S01E27"]


def test_episode_file_selection_refuses_an_indivisible_bundle():
    result = select_episode_files(
        [{"index": 0, "name": "Show.S01.Complete.mkv", "size": 33000, "priority": 1}],
        ["S01E27", "S01E28"],
    )

    assert result["safe"] is False
    assert result["reason"] == "episode-files-not-separable"
