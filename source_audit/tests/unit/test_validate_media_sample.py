import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from validate_media_sample import determine_verdict  # noqa: E402


def test_valid_when_video_and_audio_present():
    streams = [{"codec_type": "video"}, {"codec_type": "audio"}]
    assert determine_verdict(clip_bytes=1000, streams=streams) == "VALID"


def test_inconclusive_when_only_video():
    streams = [{"codec_type": "video"}]
    assert determine_verdict(clip_bytes=1000, streams=streams) == "INCONCLUSIVE"


def test_inconclusive_when_only_audio():
    streams = [{"codec_type": "audio"}]
    assert determine_verdict(clip_bytes=1000, streams=streams) == "INCONCLUSIVE"


def test_inconclusive_when_zero_bytes():
    streams = [{"codec_type": "video"}, {"codec_type": "audio"}]
    assert determine_verdict(clip_bytes=0, streams=streams) == "INCONCLUSIVE"


def test_inconclusive_when_no_streams():
    assert determine_verdict(clip_bytes=1000, streams=[]) == "INCONCLUSIVE"


def test_valid_ignores_extra_streams_like_subtitles():
    streams = [{"codec_type": "video"}, {"codec_type": "audio"}, {"codec_type": "subtitle"}]
    assert determine_verdict(clip_bytes=1000, streams=streams) == "VALID"
