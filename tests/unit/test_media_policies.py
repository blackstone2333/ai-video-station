import pytest

from ainas.media_policies import MEDIA_POLICIES, UnsupportedMediaType, policy_for


def test_media_policies_preserve_the_four_supported_external_types():
    assert tuple(MEDIA_POLICIES) == ("movie", "tv", "anime", "custom")

    assert policy_for("movie").episodic is False
    assert policy_for("movie").naming_layout == "movie"
    assert policy_for("movie").target_layout == "title"

    for media_type in ("tv", "anime"):
        policy = policy_for(media_type)
        assert policy.episodic is True
        assert policy.naming_layout == "episodic"
        assert policy.target_layout == "season"

    custom = policy_for("custom")
    assert custom.rename_enabled is False
    assert custom.naming_layout == "preserve"
    assert custom.target_layout == "preserve"


def test_media_policies_do_not_open_new_external_types():
    with pytest.raises(UnsupportedMediaType, match="unsupported media type"):
        policy_for("podcast")
