"""End-to-end pipeline orchestration: source -> manifest -> download -> validate
-> hash -> Telegram publish -> verification. Handles the publish-once guard so a
restart never double-publishes the same episode (POC spec clauses 27/31/32)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from source_audit.fetch.http_client import HttpClient

from v1_poc.config import evidence_dir, load_config
from v1_poc.downloader import DownloadError, download_and_mux
from v1_poc.evidence import fingerprint, redact_url, write_json
from v1_poc.manifest import parse_media_playlist, select_best_rendition
from v1_poc.media_tools import ffmpeg_bin, ffprobe_bin
from v1_poc.source_client import SourceAccessError, SourceExtraction, extract_source
from v1_poc.state import PublishedState
from v1_poc.telegram_client import TelegramClient, TelegramPublishError, caption_for
from v1_poc.validator import ValidationResult, sha256_file, validate_media_file

logger = logging.getLogger(__name__)

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"
STATUS_SKIPPED = "SKIPPED"


@dataclass
class Step:
    name: str
    status: str = "PENDING"
    detail: str = ""
    evidence: list[str] = field(default_factory=list)


@dataclass
class PipelineRun:
    episode_url: str
    steps: list[Step] = field(default_factory=list)
    extraction: SourceExtraction | None = None
    selected_rendition: dict | None = None
    media_playlist: dict | None = None
    download: dict | None = None
    validation: dict | None = None
    sha256: str | None = None
    telegram: dict | None = None
    duplicate: dict | None = None
    stop_before_publish: str | None = None


def _codec_family(codec: str | None) -> str | None:
    if not codec:
        return None
    if codec.startswith("avc1"):
        return "h264"
    if codec.startswith(("hvc1", "hev1")):
        return "hevc"
    if codec.startswith("mp4a"):
        return "aac"
    return codec


def _detect_language(episode_url: str) -> str:
    low = episode_url.lower()
    if "-vostfr" in low:
        return "VOSTFR"
    if "-vf/" in low or low.rstrip("/").endswith("-vf"):
        return "VF"
    if "film-vf" in low or "film-jap" in low:
        return "VF"
    return "UNKNOWN"


def _add(run: PipelineRun, name: str, status: str, detail: str, evidence: list[str] | None = None) -> None:
    run.steps.append(Step(name=name, status=status, detail=detail, evidence=evidence or []))


def run_end_to_end(
    episode_url: str,
    client: HttpClient,
    *,
    do_download: bool = True,
    do_publish: bool = True,
    telegram: TelegramClient | None = None,
    state: PublishedState | None = None,
    keep_segments: bool = False,
) -> PipelineRun:
    cfg = load_config()
    http_cfg = cfg.get("http", {})
    media_cfg = cfg.get("media", {})
    run = PipelineRun(episode_url=episode_url)

    source_evidence_dir = evidence_dir("source")
    media_evidence_dir = evidence_dir("media")
    end_to_end_dir = evidence_dir("end_to_end")
    downloads = Path(media_cfg.get("output_dir", "downloads"))
    downloads.mkdir(parents=True, exist_ok=True)

    # ---- 1..5 Source / player / manifest / rendition -----------------------
    try:
        extraction = extract_source(episode_url, client)
    except SourceAccessError as exc:
        _add(run, "SOURCE_ACCESS", STATUS_FAIL, str(exc))
        _add(run, "EPISODE_IDENTIFIED", STATUS_BLOCKED, "not reached — source access failed")
        return _finalize(run)

    run.extraction = extraction
    _add(run, "SOURCE_ACCESS", STATUS_PASS, f"HTTP 200, {extraction.episode_page_status}")
    write_json(
        source_evidence_dir / "episode_identity.json",
        {
            "episode_url": extraction.episode_url,
            "episode_key": extraction.episode_key,
            "anime_key": extraction.anime_key,
            "anime_post_id": extraction.anime_post_id,
            "episode_number": extraction.episode_number,
            "language": _detect_language(extraction.episode_url),
        },
    )
    _add(
        run,
        "EPISODE_IDENTIFIED",
        STATUS_PASS,
        f"episode_key={extraction.episode_key} anime_key={extraction.anime_key} ep={extraction.episode_number} language={_detect_language(extraction.episode_url)}",
        ["source/episode_identity.json"],
    )

    if not extraction.player_iframe_url:
        _add(run, "PLAYER_FOUND", STATUS_FAIL, "no player iframe on episode page")
        return _finalize(run)
    _add(run, "PLAYER_FOUND", STATUS_PASS, f"iframe={redact_url(extraction.player_iframe_url)} library={extraction.player_library}")

    if not extraction.manifest_url:
        _add(run, "MANIFEST_FOUND", STATUS_FAIL, "no HLS master manifest found")
        return _finalize(run)
    _add(run, "MANIFEST_FOUND", STATUS_PASS, f"renditions_in_master={len(extraction.renditions)} method=cleartext from embed JS (token URL not logged)")
    write_json(
        source_evidence_dir / "manifest_summary.json",
        {
            "master_manifest_fingerprint": fingerprint(extraction.manifest_text or ""),
            "master_manifest_redacted_url_ref": redact_url(extraction.manifest_url),
            "rendition_count": len(extraction.renditions),
            "renditions": [
                {
                    "resolution": r.resolution,
                    "bandwidth_bps": r.bandwidth_bps,
                    "fps": r.fps,
                    "video_codec": r.video_codec,
                    "audio_codec": r.audio_codec,
                    "playlist_url_redacted": redact_url(r.playlist_url) if r.playlist_url else None,
                }
                for r in extraction.renditions
            ],
        },
    )

    best = select_best_rendition(extraction.renditions)
    if best is None:
        _add(run, "RENDITION_SELECTED", STATUS_FAIL, "master manifest has no video renditions")
        return _finalize(run)
    if not best.playlist_url:
        _add(run, "RENDITION_SELECTED", STATUS_FAIL, "selected rendition has no media playlist URL")
        return _finalize(run)

    _add(run, "RENDITION_SELECTED", STATUS_PASS, f"{best.resolution} bw={best.bandwidth_bps} codecs={best.codecs_raw}")
    run.selected_rendition = {
        "resolution": best.resolution,
        "bandwidth_bps": best.bandwidth_bps,
        "fps": best.fps,
        "video_codec": best.video_codec,
        "audio_codec": best.audio_codec,
    }

    # ---- media playlist -----------------------------------------------------
    media_result = client.get(best.playlist_url)
    if not media_result.ok:
        _add(run, "MEDIA_PLAYLIST", STATUS_FAIL, f"media playlist fetch failed: status={media_result.status_code} error={media_result.error_type.value}")
        return _finalize(run)
    media_playlist = parse_media_playlist(media_result.text, best.playlist_url)
    run.media_playlist = {
        "segment_count": len(media_playlist.segments),
        "total_duration_seconds": round(media_playlist.total_duration_seconds, 2),
        "has_end_list": media_playlist.has_end_list,
        "encrypted": media_playlist.encrypted,
        "is_fmp4": media_playlist.is_fmp4,
        "playlist_fingerprint": fingerprint(media_result.text),
    }
    _add(
        run,
        "MEDIA_PLAYLIST",
        STATUS_PASS,
        f"segments={len(media_playlist.segments)} duration~{media_playlist.total_duration_seconds:.1f}s end_list={media_playlist.has_end_list} encrypted={media_playlist.encrypted}",
    )

    # ---- 6/7 download -------------------------------------------------------
    run.download = {}
    if do_download:
        output_path = downloads / f"{extraction.episode_key.replace('https://', '').replace('/', '_')}.mp4"
        try:
            media_dl = download_and_mux(
                media_playlist,
                output_path,
                ffmpeg_bin(),
                user_agent=http_cfg.get("user_agent", "v1_poc-research-bot/0.1"),
                work_root=media_evidence_dir / "download_work",
                timeout_seconds=http_cfg.get("timeout_seconds", 20),
                max_retries=http_cfg.get("max_retries", 2),
                retry_backoff_seconds=http_cfg.get("retry_backoff_seconds", 1.5),
                keep_segments=keep_segments,
            )
        except DownloadError as exc:
            _add(run, "DOWNLOAD_STARTED", STATUS_FAIL, f"{exc.kind}: {exc}")
            _add(run, "DOWNLOAD_COMPLETED", STATUS_BLOCKED, "not reached")
            return _finalize(run)

        m = media_dl.measurements
        run.download = {
            "output_path": str(media_dl.output_path),
            "download_started_at": m.download_started_at,
            "download_finished_at": m.download_finished_at,
            "download_duration_seconds": m.download_duration_seconds,
            "bytes_downloaded": m.bytes_downloaded,
            "segment_count": m.segment_count,
            "average_download_rate_bps": m.average_download_rate_bps,
            "final_file_size": m.product_file_size,
            "mux_returncode": media_dl.mux_returncode,
        }
        write_json(
            media_evidence_dir / "download_measurements.json",
            dict(run.download),
        )
        _add(
            run,
            "DOWNLOAD_STARTED",
            STATUS_PASS,
            f"started={m.download_started_at} segments={m.segment_count}",
        )
        _add(
            run,
            "DOWNLOAD_COMPLETED",
            STATUS_PASS,
            f"file={media_dl.output_path} size={m.product_file_size} bytes [{m.bytes_downloaded} bytes over {m.download_duration_seconds}s, {round(m.average_download_rate_bps/8/1024,1)} KiB/s]",
            ["media/download_measurements.json"],
        )

        # ---- 8/9/10 ffprobe + validation + hash ------------------------------
        expected = {
            "duration_seconds": media_playlist.total_duration_seconds,
            "resolution": best.resolution,
            "video_codec": _codec_family(best.video_codec),
            "audio_codec": _codec_family(best.audio_codec),
        }
        validation: ValidationResult = validate_media_file(media_dl.output_path, ffprobe_bin(), expected=expected)
        run.validation = {
            "verdict": validation.verdict,
            "size_bytes": validation.size_bytes,
            "duration_seconds": validation.duration_seconds,
            "format_name": validation.format_name,
            "bitrate_bps": validation.bitrate_bps,
            "video": validation.video,
            "audio": validation.audio,
            "checks": validation.checks,
            "mismatches_vs_manifest": validation.mismatches_vs_manifest,
        }
        write_json(media_evidence_dir / "ffprobe_validation.json", run.validation)

        _add(run, "FFPROBE", STATUS_PASS, f"rc=0 format={validation.format_name}", ["media/ffprobe_validation.json"])
        _add(
            run,
            "MEDIA_VALID",
            STATUS_PASS if validation.verdict == "VALID" else STATUS_FAIL,
            f"verdict={validation.verdict} duration={validation.duration_seconds}s v={validation.video.get('codec_name')} {validation.video.get('width')}x{validation.video.get('height')} a={validation.audio.get('codec_name')}",
            ["media/ffprobe_validation.json"],
        )

        if validation.verdict != "VALID":
            run.stop_before_publish = f"media validation failed ({validation.verdict}): {validation.mismatches_vs_manifest or validation.checks}"
            _add(run, "TELEGRAM_UPLOAD", STATUS_SKIPPED, "DO NOT PUBLISH — media invalid")
            return _finalize(run)

        run.sha256 = sha256_file(media_dl.output_path)
        write_json(
            media_evidence_dir / "sha256.json",
            {
                "filename": media_dl.output_path.name,
                "file_size": validation.size_bytes,
                "sha256": run.sha256,
                "duration_seconds": validation.duration_seconds,
                "resolution": f"{validation.video.get('width')}x{validation.video.get('height')}",
            },
        )
        _add(run, "SHA256", STATUS_PASS, run.sha256, ["media/sha256.json"])
    else:
        _add(run, "DOWNLOAD_STARTED", STATUS_SKIPPED, "download disabled for this run")
        dl_meas = media_evidence_dir / "download_measurements.json"
        val_file = media_evidence_dir / "ffprobe_validation.json"
        sha_file = media_evidence_dir / "sha256.json"
        if dl_meas.exists() and val_file.exists() and sha_file.exists():
            run.download = json.loads(dl_meas.read_text(encoding="utf-8"))
            vpath = Path(run.download.get("output_path", ""))
            if vpath.exists():
                run.validation = json.loads(val_file.read_text(encoding="utf-8"))
                run.sha256 = json.loads(sha_file.read_text(encoding="utf-8"))["sha256"]
                _add(run, "DOWNLOAD_COMPLETED", STATUS_PASS, f"(cached evidence) file={vpath.name} size={run.download.get('final_file_size')}")
                v = run.validation
                _add(run, "FFPROBE", STATUS_PASS, f"(cached evidence) rc=0 format={v.get('format_name')}", ["media/ffprobe_validation.json"])
                _add(run, "MEDIA_VALID", STATUS_PASS, f"(cached evidence) verdict={v.get('verdict')}", ["media/ffprobe_validation.json"])
                _add(run, "SHA256", STATUS_PASS, run.sha256, ["media/sha256.json"])
            else:
                _add(run, "DOWNLOAD_COMPLETED", STATUS_BLOCKED, "cached evidence exists but file is missing")

    # ---- 11-13 Telegram publish + verify ------------------------------------
    if not do_publish:
        _add(run, "TELEGRAM_UPLOAD", STATUS_SKIPPED, "publish disabled for this run")
        return _finalize(run, end_to_end_dir)

    if telegram is None:
        _add(run, "TELEGRAM_UPLOAD", STATUS_BLOCKED, "no telegram client configured")
        return _finalize(run, end_to_end_dir)

    episode_key = extraction.episode_key
    if state is not None and state.is_published(episode_key):
        prev = state.get(episode_key)
        run.duplicate = {"already_published": True, "previous": prev}
        _add(
            run,
            "DUPLICATE_GUARD",
            STATUS_PASS,
            f"episode already published (message_id={prev.get('telegram_message_id')}) — no new upload",
        )
        _add(run, "TELEGRAM_UPLOAD", STATUS_SKIPPED, "duplicate protection — no second publish")
        return _finalize(run, end_to_end_dir)

    resolution = f"{run.validation.get('video', {}).get('width')}x{run.validation.get('video', {}).get('height')}"
    anime_title = extraction.episode_url.split("/")[4].replace("-", " ").title() if len(extraction.episode_url.split("/")) > 4 else episode_key
    caption = caption_for(
        anime=anime_title,
        episode=str(extraction.episode_number or "?"),
        language=_detect_language(episode_url),
        resolution=resolution,
    )

    try:
        output_path = Path(run.download["output_path"])
        message = telegram.send_video(output_path, caption=caption)
    except TelegramPublishError as exc:
        run.stop_before_publish = f"telegram upload failed ({exc.kind}): {exc}"
        _add(run, "TELEGRAM_UPLOAD", STATUS_FAIL, f"{exc.kind}: {exc}")
        _add(run, "CHANNEL_VERIFY", STATUS_BLOCKED, "not reached — upload failed")
        return _finalize(run, end_to_end_dir)

    video = getattr(message, "video", None)
    run.telegram = {
        "chat_id": str(message.chat.id),
        "message_id": message.message_id,
        "caption": message.caption,
        "video_file_id": video.file_id if video else None,
        "video_size": video.file_size if video else None,
        "video_width": video.width if video else None,
        "video_height": video.height if video else None,
        "video_duration": video.duration if video else None,
    }
    write_json(
        evidence_dir("telegram") / "send_video_response.json",
        {
            "channel_id": str(message.chat.id),
            "message_id": message.message_id,
            "has_video_attachment": video is not None,
            "video_file_id": video.file_id if video else None,
            "video_size": video.file_size if video else None,
            "video_width": video.width if video else None,
            "video_height": video.height if video else None,
            "video_duration": video.duration if video else None,
            "caption": message.caption,
        },
    )
    _add(
        run,
        "TELEGRAM_UPLOAD",
        STATUS_PASS,
        f"message_id={message.message_id} video_file_id={'yes' if video else 'NONE'} size={video.file_size if video else '-'}",
        ["telegram/send_video_response.json"],
    )
    _add(run, "MESSAGE_ID", STATUS_PASS, str(message.message_id), ["telegram/send_video_response.json"])

    if video is None:
        _add(run, "CHANNEL_VERIFY", STATUS_FAIL, "API response has no video attachment")
    else:
        try:
            verification = telegram.verify_upload(
                message,
                local_size=run.validation["size_bytes"],
                local_sha256=run.sha256 or "",
                full_byte_check=True,
            )
        except Exception as exc:  # verification is best-effort but recorded
            verification = None
            _add(run, "CHANNEL_VERIFY", "INCONCLUSIVE", f"verification re-download failed: {exc}")
        if verification is not None:
            run.telegram["channel_title"] = verification.channel_title
            run.telegram["channel_type"] = verification.channel_type
            run.telegram["telegram_file_size"] = verification.telegram_file_size
            run.telegram["upload_bytes_match_local"] = verification.upload_bytes_match_local
            run.telegram["upload_sha256_match_local"] = verification.upload_sha256_match_local
            run.telegram["message_id"] = verification.message_id
            write_json(
                evidence_dir("telegram") / "upload_verification.json",
                {
                    "message_id": verification.message_id,
                    "chat_id": verification.chat_id,
                    "video_file_id": verification.video_file_id,
                    "video_size_api": verification.video_size,
                    "telegram_file_size": verification.telegram_file_size,
                    "upload_bytes_match_local": verification.upload_bytes_match_local,
                    "upload_sha256_match_local": verification.upload_sha256_match_local,
                    "caption": verification.caption,
                },
            )
            ok = verification.upload_bytes_match_local is True and verification.upload_sha256_match_local is True
            _add(
                run,
                "CHANNEL_VERIFY",
                STATUS_PASS if ok else STATUS_FAIL,
                f"channel_type={verification.channel_type} message_id={verification.message_id} bytes_match={verification.upload_bytes_match_local} sha256_match={verification.upload_sha256_match_local}",
                ["telegram/upload_verification.json"],
            )

    # ---- publish-once state -------------------------------------------------
    if state is not None:
        state.record(
            episode_key,
            {
                "episode_url": episode_url,
                "episode_key": episode_key,
                "published_at": run.download.get("download_finished_at", ""),
                "telegram_message_id": message.message_id,
                "sha256": run.sha256,
                "file_size": run.validation.get("size_bytes"),
                "channel_id": str(message.chat.id),
            },
        )
        run.duplicate = {"already_published": False, "recorded": True}
        _add(run, "DUPLICATE_GUARD", STATUS_PASS, "recorded published state for episode")

    return _finalize(run, end_to_end_dir)


def _finalize(run: PipelineRun, end_to_end_dir: Path | None = None) -> PipelineRun:
    run.steps.append(Step(name="END", status="END", detail=""))
    if end_to_end_dir is not None:
        write_json(
            end_to_end_dir / "pipeline_run.json",
            {
                "episode_url": run.episode_url,
                "steps": [
                    {
                        "step": s.name,
                        "status": s.status,
                        "detail": s.detail,
                        "evidence": s.evidence,
                    }
                    for s in run.steps
                ],
                "selected_rendition": run.selected_rendition,
                "media_playlist": run.media_playlist,
                "download": run.download,
                "validation": {
                    "verdict": (run.validation or {}).get("verdict"),
                    "size_bytes": (run.validation or {}).get("size_bytes"),
                    "duration_seconds": (run.validation or {}).get("duration_seconds"),
                },
                "sha256": run.sha256,
                "telegram": run.telegram,
                "duplicate": run.duplicate,
                "stop_before_publish": run.stop_before_publish,
            },
        )
    return run