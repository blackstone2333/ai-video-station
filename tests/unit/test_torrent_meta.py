import hashlib

import pytest

from ainas.errors import ValidationAppError
from ainas.torrent_meta import parse_torrent_metadata


INFO = b"d6:lengthi123e4:name9:Movie.mkv12:piece lengthi16384e6:pieces20:12345678901234567890e"
TORRENT = b"d8:announce14:https://t.test4:info" + INFO + b"e"


def test_torrent_metadata_extracts_name_and_exact_info_hash():
    metadata = parse_torrent_metadata(TORRENT)
    assert metadata.name == "Movie.mkv"
    assert metadata.info_hash == hashlib.sha1(INFO).hexdigest()


@pytest.mark.parametrize("value", [b"", b"not-bencode", b"d4:infode", b"d4:infod4:name0:eejunk"])
def test_torrent_metadata_rejects_malformed_payloads(value):
    with pytest.raises(ValidationAppError, match="BT 种子格式无效"):
        parse_torrent_metadata(value)
