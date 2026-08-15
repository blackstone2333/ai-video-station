from ainas.quality import (
    build_release,
    canonical_media_name,
    decode_thunder_url,
    detect_episode,
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


def test_size_episode_and_thunder_helpers():
    assert parse_size("size 1.5 TB") == ("1.5TB", int(1.5 * 1024**4))
    assert parse_size("700MB")[0] == "700MB"
    assert parse_size("unknown") == (None, 0)
    assert detect_episode("Show.S02E09.1080p") == "S02E09"
    assert detect_episode("节目 第 12 集") == "E12"
    assert detect_episode("Show EP03") == "E03"
    assert detect_episode("全集") is None
    assert detect_episode("01.mp4") == "E01"
    assert detect_episode("Show.S01E01-E02.mkv") == "S01E01-E02"
    assert detect_season("电视剧[第三季]") == 3
    assert detect_season("Show Season 12") == 12
    assert canonical_media_name("漫长的季节[第二季]") == "漫长的季节"
    assert canonical_media_name("2023高分剧情《奥本海默》1080p.BD") == "奥本海默"
    assert canonical_media_name("<font color='red'>大黄蜂</font>") == "大黄蜂"
    assert decode_thunder_url("magnet:?xt=x") == "magnet:?xt=x"
    assert decode_thunder_url("thunder://QUFlZDJrOi8vZmlsZVpa") == "ed2k://file"


def test_media_type_inference_recognizes_anime_and_episode_series():
    assert infer_media_type("Rick and Morty", "https://site.test/dm/123.html", "auto") == "anime"
    assert infer_media_type("Rick and Morty S01E01", "https://site.test/mj/123.html", "anime") == "anime"
    assert infer_media_type("凯蒂斯总统 S01E01", "https://site.test/unknown/123.html", "movie") == "tv"
    assert infer_media_type("奥本海默 1080p", "https://site.test/mj/123.html", "movie") == "movie"
    assert infer_media_type("奥本海默 1080p", "https://site.test/movie/1.html", "movie") == "movie"
    assert infer_media_type("Python 学习资料", "https://site.test/files/1.html", "custom") == "custom"
