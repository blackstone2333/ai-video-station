from __future__ import annotations

import re

import pytest
import requests
import responses

from ainas.crawler import SixVClient
from ainas.errors import UpstreamError


SEARCH_HTML = """
<html><meta charset="gb2312"><div class="listBox"><ul>
  <li><div class="listInfo"><h3><a href="/jddy/2023-11-09/46705.html" title="奥本海默">奥本海默</a></h3></div></li>
  <li><div class="listInfo"><h3><a href="/mj/2026-01-02/50000.html" title="测试剧">测试剧</a></h3></div></li>
  <li><div class="listInfo"><h3><a href="/dm/2026-01-03/50001.html" title="Rick and Morty">Rick and Morty</a></h3></div></li>
</ul></div></html>
"""

DETAIL_HTML = """
<html><body>
<p>◎年　　代　2023</p>
<a href="magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&amp;dn=movie">12.5GB 国英双语 1080p BluRay x265</a>
<a href="magnet:?xt=urn:btih:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb">1080p HDTS 抢先版</a>
<a href="https://www.6v123.net/">地址发布页</a>
<script>var x='magnet:\\x3fxt=urn:btih:cccccccccccccccccccccccccccccccccccccccc';</script>
</body></html>
"""

MISFILED_ANIME_SEARCH_HTML = """
<html><meta charset="gb2312"><div class="listBox"><ul>
  <li><div class="listInfo"><h3><a href="/mj/2026-08-15/60001.html" title="Rick and Morty">Rick and Morty</a></h3></div></li>
</ul></div></html>
"""

MISFILED_ANIME_DETAIL_HTML = """
<html><body>
<a href="magnet:?xt=urn:btih:dddddddddddddddddddddddddddddddddddddddd">S01E01 1080p WEB-DL</a>
</body></html>
"""


def test_parse_search_and_detail_pages():
    items = SixVClient.parse_search_page(SEARCH_HTML, "https://sixv.test")
    assert [item.media_type for item in items] == ["movie", "tv", "anime"]
    releases = SixVClient.parse_detail_page(
        DETAIL_HTML,
        items[0].url,
        items[0].title,
        items[0].media_type,
    )
    assert len(releases) == 2
    assert releases[0].size == "12.5GB"
    assert releases[0].year == 2023
    assert all("bbbb" not in item.download_link for item in releases)


def test_decode_html_supports_gbk_and_utf8():
    assert "中文" in SixVClient.decode_html("<meta charset=gb2312>中文".encode("gb18030"))
    assert "中文" in SixVClient.decode_html(b'<meta charset="utf-8">' + "中文".encode())


@responses.activate
def test_search_posts_gbk_and_filters_media_type(settings):
    responses.get("https://sixv.test/", body="ok", status=200)

    def search_callback(request):
        assert b"%B0%C2%B1%BE%BA%A3%C4%AC" in request.body
        return 302, {"Location": "https://sixv.test/e/search/result/?searchid=1"}, ""

    responses.add_callback(responses.POST, "https://sixv.test/e/search/index.php", callback=search_callback)
    responses.get(
        "https://sixv.test/e/search/result/?searchid=1",
        body=SEARCH_HTML.encode("gb18030"),
        status=200,
        content_type="text/html",
    )
    responses.get(
        "https://sixv.test/jddy/2023-11-09/46705.html",
        body=DETAIL_HTML.encode("gb18030"),
        status=200,
    )
    client = SixVClient(settings)
    results = client.search("奥本海默", "movie")
    assert len(results) == 2
    assert all(item.media_type == "movie" for item in results)


@responses.activate
def test_name_match_can_find_anime_misfiled_in_sixv_tv_category(settings):
    responses.get("https://sixv.test/", body="ok", status=200)
    responses.post(
        "https://sixv.test/e/search/index.php",
        body=MISFILED_ANIME_SEARCH_HTML.encode("gb18030"),
        status=200,
        content_type="text/html",
    )
    responses.get(
        "https://sixv.test/mj/2026-08-15/60001.html",
        body=MISFILED_ANIME_DETAIL_HTML.encode("gb18030"),
        status=200,
    )

    results = SixVClient(settings).search("Rick and Morty", "anime")

    assert len(results) == 1
    assert results[0].media_type == "anime"


@responses.activate
def test_probe_discovers_address_page_domain(settings):
    responses.get("https://sixv.test/", body=requests.ConnectionError("down"))
    responses.get("https://backup.test/", body=requests.ConnectionError("down"))
    responses.get(
        "https://address.test/",
        body='<a href="https://www.6vnew.test/path">new</a>',
        status=200,
    )
    responses.get("https://www.6vnew.test/", body="ok", status=200)
    assert SixVClient(settings).probe() == "https://www.6vnew.test"


@responses.activate
def test_probe_raises_when_all_domains_fail(settings):
    for url in ("https://sixv.test/", "https://backup.test/", "https://address.test/"):
        responses.get(url, body=requests.ConnectionError("down"))
    with pytest.raises(UpstreamError):
        SixVClient(settings).probe()
