from ainas.quality import (
    build_release,
    canonical_media_name,
    decode_thunder_url,
    detect_episode,
    detect_episode_range,
    detect_season,
    infer_media_type,
    is_cam_release,
    parse_size,
    sort_releases,
)


def release(label: str, link: str):
    return build_release("测试影片", label, "https://sixv.test/dy/2026-01-01/1.html", link, "movie")


def test_cam_filter_uses_token_boundaries():
    assert is_cam_release("Movie.1080p.HDTS.x264")
    assert is_cam_release("电影 抢先版")
    assert not is_cam_release("The Artists 1080p WEB-DL")
    assert release("CAM 1080p", "magnet:?xt=urn:btih:" + "a" * 40) is None


def test_release_metadata_and_priority_sorting():
    four_k = release(
        "28.3GB 国英双语 4K Dolby Vision BluRay x265",
        "magnet:?xt=urn:btih:" + "a" * 40,
    )
    full_hd = release(
        "99GB 英语 1080p WEB-DL HDR10 x264",
        "magnet:?xt=urn:btih:" + "b" * 40,
    )
    assert four_k is not None and full_hd is not None
    assert four_k.resolution == "2160p"
    assert four_k.source == "BluRay"
    assert four_k.language == "国英双语"
    assert four_k.hdr == "Dolby Vision"
    assert four_k.encoding == "x265"
    assert release("1080p.BD中英双字", "magnet:?xt=urn:btih:" + "c" * 40).source == "BluRay"
    assert sort_releases([full_hd, four_k])[0] == four_k
    api_value = four_k.to_api()
    assert api_value["type"] == "movie"
    assert "size_bytes" not in api_value


def test_release_parses_remux_audio_sets_original_language_and_complete_episode_range():
    value = build_release(
        "重器 第1季",
        "全33集 1080p REMUX 西英双语",
        "https://site.test/mj/1.html",
        "magnet:?xt=urn:btih:" + "d" * 40,
        "tv",
        metadata_original_language="西班牙语",
    )

    assert value is not None
    assert value.source == "Remux"
    assert value.audio_languages == ("es", "en")
    assert value.original_language == "es"
    assert value.episodes[0] == "S01E01"
    assert value.episodes[-1] == "S01E33"
    assert len(value.episodes) == 33


def test_episode_range_requires_episode_context_and_ignores_resolution_ranges():
    assert detect_episode_range("Show.S01E01-E33.1080p")[-1] == "S01E33"
    assert detect_episode_range("第 01-33 集 720-1080p")[-1] == "S01E33"
    assert detect_episode_range("Show S01 480-720p") == ()


def test_size_episode_and_thunder_helpers():
    assert parse_size("size 1.5 TB") == ("1.5TB", int(1.5 * 1024**4))
    assert parse_size("700MB")[0] == "700MB"
    assert parse_size("unknown") == (None, 0)
    assert detect_episode("Show.S02E09.1080p") == "S02E09"
    assert detect_episode("节目 第 12 集") == "E12"
    assert detect_episode("Show EP03") == "E03"
    assert detect_episode("全集") is None
    assert detect_episode("01.mp4") == "E01"
    assert detect_episode(
        "01.2160p.HD国语中字无水印[最新电影www.dyg7.com].mkv",
        allow_numeric_prefix=True,
    ) == "E01"
    assert detect_episode("720p.HD.mkv", allow_numeric_prefix=True) is None
    assert detect_episode("Show.S01E01-E02.mkv") == "S01E01-E02"
    assert detect_season("电视剧[第三季]") == 3
    assert detect_season("Show Season 12") == 12
    assert canonical_media_name("漫长的季节[第二季]") == "漫长的季节"
    assert canonical_media_name("2023高分剧情《奥本海默》1080p.BD") == "奥本海默"
    assert canonical_media_name("<font color='red'>大黄蜂</font>") == "大黄蜂"
    assert canonical_media_name("重器[全集]") == "重器"
    assert decode_thunder_url("magnet:?xt=x") == "magnet:?xt=x"
    assert decode_thunder_url("thunder://QUFlZDJrOi8vZmlsZVpa") == "ed2k://file"


def test_media_type_inference_recognizes_anime_and_episode_series():
    assert infer_media_type("Rick and Morty", "https://site.test/dm/123.html", "auto") == "anime"
    assert infer_media_type("Rick and Morty S01E01", "https://site.test/mj/123.html", "anime") == "anime"
    assert infer_media_type("凯蒂斯总统 S01E01", "https://site.test/unknown/123.html", "movie") == "tv"
    assert infer_media_type("奥本海默 1080p", "https://site.test/mj/123.html", "movie") == "movie"
    assert infer_media_type("奥本海默 1080p", "https://site.test/movie/1.html", "movie") == "movie"
    assert infer_media_type("Python 学习资料", "https://site.test/files/1.html", "custom") == "custom"


import pytest
from ainas.quality import expand_episode


@pytest.mark.parametrize("token, expected", [
    ("S01E03-04", "S01E03-E04"),
    ("S01E03-E04", "S01E03-E04"),
    ("S01E03_E04", "S01E03-E04"),
    ("S01E03E04", "S01E03-E04"),
    ("S01E03~04", "S01E03-E04"),
    ("S01E03至04", "S01E03-E04"),
    ("EP03-04", "E03-E04"),
    ("E03-E04", "E03-E04"),
    ("第03-04集", "E03-E04"),
    ("第03至04话", "E03-E04"),
])
def test_joined_episode_tokens_share_naming_and_coverage(token, expected):
    assert detect_episode(f"Show.{token}.1080p.mkv") == expected
    assert detect_episode_range(f"Show.{token}.1080p.mkv", 1) == ("S01E03", "S01E04")
    assert expand_episode(expected, 1) == ("S01E03", "S01E04")


def test_joined_episode_parser_does_not_consume_resolution_or_invalid_ranges():
    assert detect_episode("Show.S01E03-1080p.mkv") == "S01E03"
    assert detect_episode("Show.S01E04-03.mkv") is None
    assert detect_episode_range("Show.S01E04-03.mkv") == ()
    assert detect_episode_range("Show.S01.480-720p.mkv") == ()
    assert detect_episode("03-04.1080p.mkv", allow_numeric_prefix=True) == "E03-E04"
    assert expand_episode("S00E03-E04", 1) == ("S00E03", "S00E04")
    assert expand_episode("S01E04-E03") == ()
