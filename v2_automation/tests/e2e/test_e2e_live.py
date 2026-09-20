"""Phase 13 — LIVE E2E (skipped unless a real token + Local Bot API are present).

Run manually (needs v2_automation/.env with real credentials):
    python -m pytest tests/e2e -q --no-header -p no:cacheprovider
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from v2_automation import app_config, evidence  # noqa: E402

_FE = pytest.importorskip("v2_automation.publisher")


def _ffmpeg() -> str:
    local = ROOT.parent / "source_audit" / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
    return str(local) if local.exists() else "ffmpeg"


def _reachable(base: str) -> bool:
    import httpx
    try:
        return httpx.post(f"{base}/getWebhookInfo", timeout=5).status_code in (200, 404, 401)
    except Exception:
        return False


def _token_valid(base: str, token: str) -> bool:
    """Real authorization check: a revoked/expired token must skip, not fail.
    (E2E live is only meaningful with a VALID token on the target channel.)"""
    import httpx
    try:
        return httpx.post(f"{base}/bot{token}/getMe", timeout=10).status_code == 200
    except Exception:
        return False


cfg = app_config.load_config()
_token_ok = len(cfg.bot_token.strip()) > 8 and bool(cfg.channel_id.strip())
_api_ok = bool(cfg.telegram.get("api_base_url")) and cfg.telegram["api_base_url"].strip()
_base = cfg.telegram["api_base_url"].strip() if _api_ok else ""
pytestmark = pytest.mark.skipif(
    not (_token_ok and _api_ok and _reachable(_base) and _token_valid(_base, cfg.bot_token.strip())),
    reason="E2E live requiert un TELEGRAM_BOT_TOKEN valide (non revoqué) + TELEGRAM_CHANNEL_ID "
           "+ Local Bot API joignable — jeton actuel invalide/rêvoqué => skip (pas de fake)")


def _make_small_mp4(out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([_ffmpeg(), "-y", "-f", "lavfi", "-i",
                    "testsrc=size=640x360:rate=24:duration=3",
                    "-f", "lavfi", "-i", "sine=frequency=1000:duration=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(out)], check=True, capture_output=True)


def test_live_publish_thumbnail_then_video():
    from v2_automation.publisher import (extract_thumbnail, local_bot_base_url,
                                     Publisher, V2TelegramClient)

    cfg = app_config.load_config()
    api_base = cfg.telegram["api_base_url"].strip()
    work = evidence.evidence_dir("e2e")
    video_path = work / "e2e_probe.mp4"
    thumb_path = work / "e2e_probe_thumb.jpg"
    _make_small_mp4(video_path)
    extract_thumbnail(video_path, thumb_path, Path(_ffmpeg()))
    assert thumb_path.exists() and thumb_path.stat().st_size > 0

    client = V2TelegramClient(cfg.bot_token, cfg.channel_id,
                              local_bot_base_url(api_base, cfg.bot_token))
    me = client.get_me()
    assert me.get("is_bot") is True and me.get("id")

    pub = Publisher(client, thumbnail_caption="V2 E2E thumbnail",
                    video_caption_fn=lambda: "V2 E2E probe")
    tmsg_id, tfile_id = pub.publish_thumbnail(thumb_path)
    msg = pub.publish_video(video_path, caption="V2 E2E probe")
    pub.close()

    video = getattr(msg, "video", None)
    assert tmsg_id and tfile_id
    assert video is not None and video.file_size == video_path.stat().st_size
    proof = evidence.record_publication({
        "episode_id": 0, "anime_key": "e2e", "episode_number": 0,
        "label": "e2e-live", "thumbnail_message_id": tmsg_id,
        "video_message_id": msg.message_id,
        "video_sha256": evidence.sha256_file(video_path),
        "video_file_size": video.file_size, "channel_id": cfg.channel_id})
    assert proof is not None and proof.exists()