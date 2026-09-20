"""DownloadManager — the durable per-episode orchestrator for V2.

Drives one episode through the full state machine using the proven V1 building
blocks (never copying them):

  QUEUED -> DOWNLOADING -> DOWNLOADED -> VALIDATING -> VALIDATED
    -> PUBLISHING_THUMBNAIL -> THUMBNAIL_PUBLISHED -> PUBLISHING_VIDEO
    -> PUBLISHED -> CLEANUP_PENDING

Failure handling: transient errors -> RETRY_WAIT (re-queued until the 24h
window expires -> then FAILED); structure-change -> fingerprint gate blocks as
STRUCTURE_CHANGED.  The dedup UNIQUE constraints protect at discovery; a
pre-download re-check adds a second guard.

Everything outside the DB is injectable so unit tests never touch the network.
"""
from __future__ import annotations

import logging
import statistics
import threading
from contextlib import contextmanager
import time
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import app_config, errors, evidence, repo
from .metadata import (MediaMetadata, anime_page_url, build_caption, build_metadata,
                       build_thumbnail_caption, detect_language, detect_media_type,
                       parse_anime_info)
from .models import Episode
from . import players as _players
from .publisher import V2TelegramClient, extract_thumbnail, local_bot_base_url
from .states import State
from .timeutil import add_seconds, now_utc

logger = logging.getLogger(__name__)

PNG_SIGNATURE = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
DIRECT_MIN_DURATION_RATIO = 0.6   # a direct MP4 shorter than 60 % of its siblings' median is truncated

IMAGE_SIGNATURES = ((bytes([0xFF, 0xD8]), ".jpg"), (PNG_SIGNATURE, ".png"))   # + WebP below: what sendPhoto accepts


def _image_ext(data: bytes) -> str | None:
    """Extension for a real JPEG / PNG / WebP payload (by magic bytes), else None."""
    for sig, ext in IMAGE_SIGNATURES:
        if data.startswith(sig):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


@dataclass
class DownloadDeps:
    extract: Callable[[str, Any], Any]                            # -> SourceExtraction
    load_media_playlist: Callable[[str, str], Any]                # (playlist text, url) -> MediaPlaylist
    download: Callable[..., Any]                                  # -> DownloadResult
    validate: Callable[[Path, Path, dict | None], Any]            # -> ValidationResult
    sha256: Callable[[Path], str]
    http_client: Callable[[], Any]
    ffmpeg: Callable[[], Path]
    ffprobe: Callable[[], Path]
    telegram: Callable[[], V2TelegramClient | None]
    structure_check: Callable[[str], tuple[bool, str]]            # (ok, detail)
    scheduler: Any = None
    now: Callable[[], str] = field(default=now_utc)
    thumbnail: Callable[[Path, Path, Path], Path] = field(default_factory=lambda: extract_thumbnail)


# end states: the episode leaves its anime queue so the next episode can start
QUEUE_RELEASING = ("published", "cleanup_pending", "cleaned", "failed", "skipped_dup")


class RetryWindowExceeded(RuntimeError):
    pass


class EpisodeAlreadyDone(RuntimeError):
    pass


def make_telegram_client(cfg: app_config.AppConfig) -> V2TelegramClient | None:
    """The production Telegram client (Local Bot API base URL, file:// upload container)."""
    if not cfg.bot_token or not cfg.channel_id:
        return None
    base = cfg.telegram.get("api_base_url") or None
    to = cfg.telegram.get("upload_timeout_seconds", 600)
    client = V2TelegramClient(cfg.bot_token, cfg.channel_id,
                              base_url=local_bot_base_url(base, cfg.bot_token) if base else None,
                              read_timeout=to, write_timeout=to)
    client.local_container = (cfg.telegram.get("local_upload_container") or None) if base else None
    return client


def default_deps(cfg: app_config.AppConfig) -> DownloadDeps:
    from v1_poc import downloader as v1_dl
    from v1_poc import manifest as v1_manifest
    from v1_poc import source_client as v1_src, validator as v1_val
    from v1_poc.media_tools import ffmpeg_bin, ffprobe_bin
    from source_audit.fetch.http_client import HttpClient

    def make_client():
        return HttpClient(timeout_seconds=20, max_retries=2,
                          retry_backoff_seconds=1.5,
                          user_agent="v2_automation-research-bot/2.0 (+contact: lionelyvan24@gmail.com)")

    telegram_factory = lambda: make_telegram_client(cfg)  # noqa: E731

    v1_cfg = __import__("v1_poc.config", fromlist=["load_config"]).load_config()
    ua = v1_cfg.get("http", {}).get("user_agent", "v2_automation-research-bot/2.0")

    def dl(playlist, output, ffmpeg, **kw):
        return v1_dl.download_and_mux(
            playlist, output, ffmpeg, user_agent=ua,
            work_root=kw.pop("work_root", Path("output/evidence/download_work")),
            timeout_seconds=kw.pop("timeout_seconds", 20),
            max_retries=kw.pop("max_retries", 2),
            retry_backoff_seconds=kw.pop("retry_backoff_seconds", 1.5),
        )

    def structure_check(url: str) -> tuple[bool, str]:
        from source_audit.detection import fingerprint as fp
        try:
            ok = fp.verify_url_structure(url, digest_store=None)
            return (True, "structure_ok") if ok else (False, "structure_mismatch")
        except Exception as exc:
            return (True, f"fingerprint_unavailable:{type(exc).__name__}")

    return DownloadDeps(
        extract=_players.extract_with_fallback,
        load_media_playlist=v1_manifest.parse_media_playlist,
        download=dl,
        validate=v1_val.validate_media_file,
        sha256=v1_val.sha256_file,
        http_client=make_client,
        ffmpeg=ffmpeg_bin,
        ffprobe=ffprobe_bin,
        telegram=telegram_factory,
        structure_check=structure_check,
    )


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


def _detect_language(url: str) -> str:
    low = url.lower()
    if "-vostfr" in low:
        return "vostfr"
    if low.rstrip("/").endswith("-vf") or "-vf/" in low or "film-vf" in low:
        return "vf"
    return "unknown"


class PublishGate:
    """ONE publication at a time in the channel, in order of arrival (= order in which the downloads finished).
    An episode publishes its thumbnail AND its video before the next one starts its thumbnail, so the channel
    never shows two thumbnails without their videos.  A ticket is released in every case (success, failure,
    exception): a problem on one episode never blocks the others."""

    class Ticket:
        def __init__(self, gate: "PublishGate", token: object):
            self._gate, self._token = gate, token

        def wait_turn(self) -> None:
            g, t = self._gate, self._token
            with g._cv:
                while g._busy is not None or g._waiting[0] is not t:
                    g._cv.wait()
                g._busy = t
                g._waiting.remove(t)

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._waiting: list[object] = []
        self._busy: object | None = None

    @contextmanager
    def arrive(self):
        token = object()
        with self._cv:
            self._waiting.append(token)               # position in line = arrival order
        try:
            yield PublishGate.Ticket(self, token)
        finally:
            with self._cv:
                if token in self._waiting:
                    self._waiting.remove(token)
                if self._busy is token:
                    self._busy = None
                self._cv.notify_all()


class DownloadManager:
    def __init__(self, conn: sqlite3.Connection, cfg: app_config.AppConfig,
                 deps: DownloadDeps | None = None,
                 *, retry_window_hours: float | None = None,
                 max_publish_bytes: int | None = None,
                 alerter=None, progress: Callable[[int, str, str], None] | None = None):
        self.conn = conn
        self.progress = progress  # optional (n, label, detail) reporter for the 7 pipeline steps
        # publications (Telegram uploads) are bounded independently of downloads
        self._ctx = threading.local()
        self._gate = PublishGate()
        self.cfg = cfg
        self.deps = deps or default_deps(cfg)
        self.alerter = alerter  # Callable(kind, akey, title, body) -> None (best effort)
        self.retry_window = timedelta(
            hours=retry_window_hours if retry_window_hours is not None
            else float(cfg.downloads.get("retry_window_hours", 24)))
        # Phase 6 finding: the Local Bot API build 10.3 (commit e3e9dd8) is only
        # PROVEN to 800 MiB (900 MiB segfaults).  Any episode whose estimated
        # size exceeds the cap is refused BEFORE disk is spent.
        if max_publish_bytes is not None:
            self.max_publish_bytes = max_publish_bytes
        else:
            limits = cfg.limits
            safe_mb = limits.get("max_safe_publish_mib") or limits.get("tested_upload_limit_mb") or 768
            self.max_publish_bytes = int(safe_mb * 1024 * 1024)

    # ── high-level entry ─────────────────────────────────────────────────────────

    def process_episode(self, episode_id: int, on_downloaded: Callable[[int], None] | None = None) -> str:
        """Runs one episode through the machine. Returns its final status.  `on_downloaded(episode_id)` is called as soon
        as the download and validation are finished (the worker frees the download slot: the next anime starts while this
        one is still being sent / published)."""
        ep = repo.get(self.conn, episode_id)
        if ep is None:
            raise KeyError(f"episode {episode_id} not found")
        if ep.status in ("published", "cleaned", "cleanup_pending", "skipped_dup"):
            raise EpisodeAlreadyDone(f"episode already {ep.status} — no-op")
        if ep.video_message_id:
            raise EpisodeAlreadyDone(f"video_message_id={ep.video_message_id} already recorded — no re-publish")
        if ep.status == "retry_wait" and self._retry_exhausted(ep):
            repo.mark_failed(self.conn, episode_id, "retry window exhausted (24h)")
            self.conn.commit()
            final = repo.get(self.conn, episode_id)
            repo.release_queue_item(self.conn, episode_id)
            self.conn.commit()
            self._route_alert(final)
            return State.FAILED.value

        self._ctx.ep = ep
        self._ctx.on_downloaded = on_downloaded
        repo.bump_attempt(self.conn, episode_id)   # retry observability (closure)
        self.conn.commit()

        try:
            self._do_download_and_publish(ep)
        except EpisodeAlreadyDone:
            raise
        except Exception as exc:  # any failure routes through the retry machinery
            self._handle_failure(ep, exc)
            self.conn.commit()
        final = repo.get(self.conn, episode_id)
        if final.status in QUEUE_RELEASING:
            repo.release_queue_item(self.conn, episode_id)
            self.conn.commit()
        if final.status in ("published", "cleanup_pending"):
            from . import alerts as _alerts
            _alerts.resolve_for_episode(self.conn, episode_id)      # its earlier retry/failure alerts are moot
            self.conn.commit()
            self._notify_published(final)
        self._route_alert(final)
        return final.status

    def _notify_published(self, final) -> None:
        """Informational push (if enabled): `Bleach E36 publié · 365 Mo`. Never blocks processing."""
        if self.alerter is None:
            return
        try:
            row = self.conn.execute("SELECT title FROM animes WHERE anime_key=?", (final.anime_key,)).fetchone()
            name = (row["title"] if row and row["title"] else final.anime_key)
            size = f" · {round(final.file_size / 1048576)} Mo" if final.file_size else ""
            self.alerter("published", f"ep:{final.id}",
                         f"{name} E{final.episode_number if final.episode_number is not None else '?'} publié{size}",
                         "miniature et vidéo publiées")
        except Exception as exc:
            logger.warning("notification de publication impossible: %s", exc)

    def _route_alert(self, final) -> None:
        """Best-effort DB-backed alert on terminal/operational statuses.  The
        row write is debounced by (kind, akey); dispatch uses the hourly slot."""
        if self.alerter is None:
            return
        kind = None
        if final.status == "failed":
            kind = "definitive_failure"
        elif final.status == "retry_wait":
            return              # silent automatic retries; alerts.raise_persistent_errors signals it after N minutes
        elif final.status == "structure_changed":
            kind = "structure_changed"
        elif final.status == "blocked":
            kind = "authorization_blocked"
        if kind is None:
            return
        ep = final
        try:
            self.alerter(kind, f"ep:{ep.id}",
                         f"{kind.replace('_', ' ')} — épisode {ep.id}",
                         f"{ep.anime_key} E{ep.episode_number or '?'} : "
                         f"{(ep.last_error or '')[:300]}")
        except Exception as exc:  # an alert problem must never break processing
            logger.warning("alerte épisode %s échoue: %s", ep.id, exc)

    # ── internals ────────────────────────────────────────────────────────────────

    def _retry_exhausted(self, ep) -> bool:
        until = ep.retry_until_at
        if not until:
            return False
        try:
            exp = datetime.fromisoformat(until.replace("Z", "+00:00"))
        except ValueError:
            try:
                exp = datetime.fromisoformat(until)
            except ValueError:
                return False
        now = datetime.fromisoformat(self.deps.now().replace("Z", "+00:00"))
        return now >= exp

    _STEP_TAGS = {1: "SOURCE", 2: "RENDITION", 3: "PLAYLIST", 4: "DOWNLOAD", 5: "VALIDATION",
                  6: "THUMBNAIL", 7: "TELEGRAM"}

    def _step(self, n: int, label: str, detail: str = "") -> None:
        if self.progress is not None:
            self.progress(n, label, detail)
        ep = getattr(self._ctx, "ep", None)        # per-thread: several episodes run in parallel
        logger.info("[%s] job=%s anime=%s episode=%s %s", self._STEP_TAGS.get(n, label.upper()),
                    getattr(ep, "id", "?"), getattr(ep, "anime_key", "?"),
                    getattr(ep, "episode_number", "?"), detail)

    def _do_download_and_publish(self, ep) -> None:
        client = self.deps.http_client()
        try:
            # 0) fingerprint structure gate (blocks as STRUCTURE_CHANGED).
            ok, _ = self.deps.structure_check(ep.episode_url)
            if not ok:
                repo.transition(self.conn, ep.id, State.STRUCTURE_CHANGED.value)
                self.conn.commit()
                return

            # 1) source extraction (episode page -> embed -> master manifest).
            try:
                extraction = self.deps.extract(ep.episode_url, client)
            except Exception as exc:
                from v1_poc.source_client import SourceNotAvailableError
                if isinstance(exc, SourceNotAvailableError):
                    code = errors.NOT_AVAILABLE_YET
                elif "manifest fetch failed" in str(exc) and "404" in str(exc):
                    code = errors.SOURCE_VIDEO_PROCESSING          # the player exists, the video file does not (yet)
                else:
                    code = errors.SOURCE_EXTRACTION_FAILED
                raise errors.wrap(code, exc) from exc
            if extraction.episode_key and extraction.episode_key not in (ep.episode_key, ep.canonical_episode_url):
                repo.transition(self.conn, ep.id, State.SKIPPED_DUP.value)
                self.conn.commit()
                return
            direct_url = getattr(extraction, "direct_url", None)        # a player that serves ONE direct MP4 (Stape)
            player = getattr(extraction, "player_name", "myTV")
            self._remember_player(ep.id, player)
            self._step(1, "Source", (f"lecteur {player} : MP4 direct" if direct_url
                                     else f"{len(extraction.renditions)} rendition(s)"))
            output_path = self._output_path(ep)

            if direct_url:
                # a direct MP4 announces no duration: the reference is what this anime's other episodes lasted
                ref = self._reference_duration(ep)
                media_playlist, best = None, None
                expected = {"duration_seconds": ref * DIRECT_MIN_DURATION_RATIO} if ref else None
                if ref is None:
                    logger.warning("job=%s aucune durée de référence (premier épisode de cet anime) : durée non contrôlée", ep.id)
                est = getattr(extraction, "direct_size", None)
                self._disk_gate(est, f"MP4 direct {player}")
                self._step(2, "Rendition", f"MP4 direct ({(est or 0) // (1024 * 1024)} Mio)")
            else:
                # 2) best rendition.
                from v1_poc.manifest import select_best_rendition
                best = select_best_rendition(extraction.renditions)
                if best is None or not best.playlist_url:
                    raise errors.PipelineError(errors.NO_RENDITION, "no usable rendition (master manifest empty)")
                self._step(2, "Rendition", f"{best.resolution} bw={best.bandwidth_bps}")

                # 3) media playlist (fetched once, then parsed).
                try:
                    media_result = client.get(best.playlist_url)
                except Exception as exc:
                    raise errors.wrap(errors.PLAYLIST_FETCH_FAILED, exc) from exc
                if not media_result.ok:
                    raise errors.PipelineError(errors.PLAYLIST_FETCH_FAILED,
                                               f"HTTP {media_result.status_code}")
                try:
                    media_playlist = self.deps.load_media_playlist(media_result.text, best.playlist_url)
                except Exception as exc:
                    raise errors.wrap(errors.PLAYLIST_PARSE_FAILED, exc) from exc
                if not media_playlist.segments:
                    raise errors.PipelineError(errors.PLAYLIST_PARSE_FAILED, "media playlist has zero segments")
                self._step(3, "Playlist", f"{len(media_playlist.segments)} segments, "
                                          f"{media_playlist.total_duration_seconds:.0f}s")

                # 4) disk budget (dynamic scheduler) BEFORE consuming disk, and the
                #    Phase 6 proven upload ceiling (refuse sizes the server cannot take).
                est = self._estimate_size(best, media_playlist)
                self._disk_gate(est, f"rendition {best.resolution}")

                expected = {
                    "duration_seconds": media_playlist.total_duration_seconds,
                    "resolution": best.resolution,
                    "video_codec": _codec_family(best.video_codec),
                    "audio_codec": _codec_family(best.audio_codec),
                }

            # 5) download (QUEUED -> DOWNLOADING -> DOWNLOADED), reusing a valid cached MP4.
            repo.transition(self.conn, ep.id, State.DOWNLOADING.value)
            self.conn.commit()
            result = self._reuse_cached(output_path, expected)
            if result is not None:
                self._step(4, "Download", f"cache reused ({output_path.stat().st_size} bytes)")
            elif direct_url:
                try:
                    dl_result = self._download_direct(extraction, output_path)
                except Exception as exc:
                    raise errors.wrap(errors.DOWNLOAD_FAILED, exc) from exc
                m = dl_result.measurements
                self._step(4, "Download", f"{output_path.stat().st_size} bytes in {m.download_duration_seconds}s")
                if self.deps.scheduler is not None and m.average_download_rate_bps:
                    self.deps.scheduler.observe_transfer(m.average_download_rate_bps)
            else:
                try:
                    dl_result = self.deps.download(
                        media_playlist, output_path, self.deps.ffmpeg(),
                        # one private segment folder per episode: parallel jobs used to share ".../segments" and
                        # overwrite / delete each other's seg_00123.ts (corrupt mux, FileNotFoundError)
                        work_root=evidence.evidence_dir("download_work") / f"ep_{ep.id}",
                    )
                except Exception as exc:
                    raise errors.wrap(errors.DOWNLOAD_FAILED, exc) from exc
                if not output_path.exists() or output_path.stat().st_size <= 0:
                    raise errors.PipelineError(errors.DOWNLOAD_FAILED, f"output missing or empty: {output_path}")
                m = dl_result.measurements
                self._step(4, "Download", f"{output_path.stat().st_size} bytes in {m.download_duration_seconds}s")
                if self.deps.scheduler is not None and m.average_download_rate_bps:
                    self.deps.scheduler.observe_transfer(m.average_download_rate_bps)
            repo.transition(self.conn, ep.id, State.DOWNLOADED.value)

            # 6) validation (ffprobe vs manifest facts) — nothing is published if it fails.
            repo.transition(self.conn, ep.id, State.VALIDATING.value)
            self.conn.commit()
            if result is None:
                result = self._validate(output_path, expected)
            repo.transition(self.conn, ep.id, State.VALIDATED.value)
            cb = getattr(self._ctx, "on_downloaded", None)
            if cb is not None:
                try:
                    cb(ep.id)                              # download slot released: the queue moves on at once
                except Exception as exc:
                    logger.warning("libération de la place impossible: %s", exc)
            self._step(5, "Validation", f"{result.format_name} {result.duration_seconds}s "
                                        f"{result.video.get('width')}x{result.video.get('height')}")

            self._remember_duration(ep.id, result.duration_seconds)
            sha = self.deps.sha256(output_path)
            # the download is finished: take our place in line NOW (arrival order); released in every case
            with self._gate.arrive() as ticket:
                repo.upsert_episode(self.conn, _updated(
                    repo.get(self.conn, ep.id), file_size=result.size_bytes,
                    file_path=str(output_path), video_sha256=sha, media_hash=sha))
                self.conn.commit()

                meta = build_metadata(
                    episode_url=ep.episode_url, page_title_raw=getattr(extraction, "page_title_raw", None),
                    episode_number=ep.episode_number,
                    source_language=getattr(extraction, "source_language", None) or ep.language,
                    validation=result)

                # 7) thumbnail (with the anime info card) + video, in that order.
                card = self._anime_card(client, ep.episode_url)
                ticket.wait_turn()                           # one publication at a time, first finished first posted
                self._publish(ep, output_path, sha, meta, getattr(extraction, "thumbnail_url", None), card)
        finally:
            client.close()

    def _anime_card(self, client, episode_url: str) -> str | None:
        """Info card for the thumbnail caption, from the source's anime page. Best effort:
        without it the thumbnail is published with no caption."""
        url = anime_page_url(episode_url)
        if not url:
            return None
        try:
            page = client.get(url)
            if not page.ok:
                return None
            tag = self.cfg.publication.get("channel_tag")
            return build_thumbnail_caption(parse_anime_info(page.text), tag)
        except Exception as exc:
            logger.warning("fiche anime indisponible (%s)", exc)
            return None

    def _validate(self, output_path: Path, expected: dict):
        try:
            result = self.deps.validate(output_path, self.deps.ffprobe(), expected=expected)
        except Exception as exc:
            raise errors.wrap(errors.VALIDATION_FAILED, exc) from exc
        if result.verdict != "VALID":
            failed = [c for c in (getattr(result, "checks", None) or []) if not c.get("pass", True)]
            raise errors.PipelineError(
                errors.VALIDATION_FAILED,
                f"verdict={result.verdict} failed_checks={failed} "
                f"mismatches={getattr(result, 'mismatches_vs_manifest', None)}")
        return result

    def _reuse_cached(self, output_path: Path, expected: dict):
        """Returns the ValidationResult of an already-present MP4 when it is intact.
        An invalid file is deleted (only that file) so the caller re-downloads."""
        if not output_path.exists():
            return None
        try:
            result = self._validate(output_path, expected)
        except errors.PipelineError as exc:
            logger.warning("cache invalide, suppression de %s: %s", output_path.name, exc)
            output_path.unlink(missing_ok=True)
            return None
        return result

    def _make_thumbnail(self, ep, video_path: Path, source_url: str | None = None) -> Path:
        """Thumbnail = the source's own poster when available; otherwise a frame of the video.
        An intact cached file is reused."""
        if source_url:
            thumb_dir = evidence.evidence_dir("thumbnail")
            for cached in sorted(thumb_dir.glob(f"thumb_{ep.id}_source.*")):
                if self._intact_image(cached):
                    return cached
            try:
                import httpx
                from urllib.parse import urlsplit
                origin = "{0.scheme}://{0.netloc}/".format(urlsplit(source_url))
                r = httpx.get(source_url, timeout=30, follow_redirects=True,
                              headers={"User-Agent": "v2_automation-research-bot/2.0 (+contact: lionelyvan24@gmail.com)",
                                       "Referer": origin})
                ext = _image_ext(r.content) if r.status_code == 200 else None
                if ext:
                    poster = thumb_dir / f"thumb_{ep.id}_source{ext}"
                    poster.write_bytes(r.content)
                    return poster
                logger.warning("poster source inutilisable (HTTP %s, %s, %d octets), repli sur une image de la vidéo",
                               r.status_code, r.headers.get("content-type"), len(r.content))
            except Exception as exc:
                logger.warning("poster source injoignable (%s), repli sur une image de la vidéo", exc)
        thumb = evidence.evidence_dir("thumbnail") / f"thumb_{ep.id}.jpg"
        if self._intact_image(thumb):
            return thumb
        thumb.unlink(missing_ok=True)
        try:
            out = self.deps.thumbnail(video_path, thumb, self.deps.ffmpeg())
        except Exception as exc:
            raise errors.wrap(errors.THUMBNAIL_FAILED, exc) from exc
        if not out.exists() or out.stat().st_size <= 0:
            out.unlink(missing_ok=True)
            raise errors.PipelineError(errors.THUMBNAIL_FAILED, f"thumbnail missing or empty: {out}")
        return out

    @staticmethod
    def _intact_image(path: Path) -> bool:
        return path.exists() and path.stat().st_size > 0 and _image_ext(path.read_bytes()) is not None

    def _cleanup_at(self) -> str:
        """Scheduled local-file cleanup: published_at + retention (the local file only, never the messages)."""
        days = float((self.cfg.publication or {}).get("cleanup_after_days", 14))
        return add_seconds(now_utc(), days * 86400)

    def _await_late_video(self, telegram, thumbnail_message_id: int | None, caption: str,
                          video_path: Path) -> int | None:
        """Id of the video the server published after dropping the connection, or None."""
        waiter = getattr(telegram, "wait_for_message", None)
        if waiter is None or thumbnail_message_id is None:
            return None
        every = float(self.cfg.telegram.get("late_video_poll_seconds", 20))
        limit = float(self.cfg.telegram.get("late_video_wait_seconds", 1800))
        self._step(7, "Telegram", "connexion coupée pendant l'envoi — attente de la publication côté serveur")
        try:
            return waiter(range(thumbnail_message_id + 1, thumbnail_message_id + 4), caption,
                          timeout_s=limit, poll_s=every)
        finally:
            cleanup = getattr(telegram, "cleanup_remote", None)
            if cleanup is not None:
                cleanup(video_path)

    def _sent(self, episode_id: int, publication_type: str) -> int | None:
        row = self.conn.execute(
            "SELECT message_id FROM publications WHERE episode_id=? AND publication_type=? "
            "AND status='sent'", (episode_id, publication_type)).fetchone()
        return row["message_id"] if row else None

    def _publish(self, ep, video_path: Path, sha: str, meta: MediaMetadata,
                 thumbnail_url: str | None = None, card: str | None = None) -> None:
        telegram = self.deps.telegram()
        if telegram is None:
            raise RuntimeError("no telegram client configured — publication impossible")
        try:
            from .publisher import Publisher
            pub = Publisher(telegram, thumbnail_caption=card)
            caption = build_caption(meta)
            tmsg_id = self._sent(ep.id, "thumbnail")
            thumbnail_path = None

            # dedup guard: a video message already recorded is never re-sent — the
            # episode row is simply completed from the publication record (crash window
            # between the Telegram ACK commit and the episode update).
            prev_video_id = self._sent(ep.id, "first_publication")
            if prev_video_id is not None:
                self._step(7, "Telegram", f"video_message_id={prev_video_id} already published — no re-send")
                repo.upsert_episode(self.conn, _updated(
                    repo.get(self.conn, ep.id), video_message_id=prev_video_id,
                    thumbnail_message_id=tmsg_id, video_sha256=sha, published_at=True))
                repo.transition(self.conn, ep.id, State.PUBLISHING_THUMBNAIL.value)
                repo.transition(self.conn, ep.id, State.THUMBNAIL_PUBLISHED.value)
                repo.transition(self.conn, ep.id, State.PUBLISHING_VIDEO.value)
                repo.transition(self.conn, ep.id, State.PUBLISHED.value)
                repo.transition(self.conn, ep.id, State.CLEANUP_PENDING.value)
                repo.set_cleanup_at(self.conn, ep.id, self._cleanup_at())
                self.conn.commit()
                return

            if self.cfg.telegram.get("thumbnail_before_video", True):
                repo.transition(self.conn, ep.id, State.PUBLISHING_THUMBNAIL.value)
                self.conn.commit()
                thumbnail_path = self._make_thumbnail(ep, video_path, thumbnail_url)
                meta.thumbnail = str(thumbnail_path)
                self._step(6, "Thumbnail", f"{thumbnail_path.stat().st_size} bytes")
                if tmsg_id is None:      # never re-post a thumbnail already sent (retry after video failure)
                    try:
                        tmsg_id, _ = pub.publish_thumbnail(thumbnail_path)
                    except Exception as exc:
                        raise errors.wrap(errors.TELEGRAM_THUMBNAIL_FAILED, exc) from exc
                    repo.commit_publication(self.conn, ep.id, "thumbnail",
                                            self.cfg.channel_id, tmsg_id, "photo", None,
                                            thumbnail_path.stat().st_size)
                repo.transition(self.conn, ep.id, State.THUMBNAIL_PUBLISHED.value)
                self.conn.commit()
                repo.transition(self.conn, ep.id, State.PUBLISHING_VIDEO.value)
                self.conn.commit()
            else:
                self._step(6, "Thumbnail", "disabled by config")

            video_id, chat_id, video_size = None, self.cfg.channel_id, video_path.stat().st_size
            t_send = time.monotonic()
            try:
                msg = pub.publish_video(video_path, caption=caption)
                video = getattr(msg, "video", None)
                if video is None:
                    raise errors.PipelineError(errors.TELEGRAM_VIDEO_FAILED, "API response has no video attachment")
                video_id, chat_id, video_size = msg.message_id, str(msg.chat.id), video.file_size
            except Exception as exc:
                err = errors.wrap(errors.TELEGRAM_VIDEO_FAILED, exc)
                if not _upload_outcome_unknown(err):
                    raise err if err is exc else err from exc
                # connection dropped mid-upload: the server usually finishes it minutes later.
                logger.warning("envoi vidéo: connexion perdue après %ss — %s", round(time.monotonic() - t_send), err)
                video_id = self._await_late_video(telegram, tmsg_id, caption, video_path)
                if video_id is None:
                    raise err if err is exc else err from exc
            self._step(7, "Telegram", f"thumbnail_message_id={tmsg_id} video_message_id={video_id}")
            repo.commit_publication(self.conn, ep.id, "first_publication", chat_id, video_id, "video",
                                    sha, video_size)
            # single metadata upsert mirroring the freshest DB row (never stale fields)
            repo.upsert_episode(self.conn, _updated(
                repo.get(self.conn, ep.id),
                video_message_id=video_id,
                thumbnail_message_id=tmsg_id,
                thumbnail_path=str(thumbnail_path) if thumbnail_path else None,
                thumbnail_size=thumbnail_path.stat().st_size if thumbnail_path else None,
                video_sha256=sha, published_at=True))
            repo.transition(self.conn, ep.id, State.PUBLISHED.value)
            repo.transition(self.conn, ep.id, State.CLEANUP_PENDING.value)
            repo.set_cleanup_at(self.conn, ep.id, self._cleanup_at())
            self.conn.commit()
            logger.info("[PUBLISHED] job=%s anime=%s episode=%s thumbnail_message_id=%s video_message_id=%s",
                        ep.id, ep.anime_key, ep.episode_number, tmsg_id, video_id)
            # Phase 12 — publication proof (no secrets: token/channel excluded).
            evidence.record_publication({
                "episode_id": ep.id, "anime_key": ep.anime_key,
                "episode_number": ep.episode_number, "label": ep.episode_key,
                "title": meta.title, "language": meta.language, "quality": meta.quality,
                "duration": meta.duration,
                "thumbnail_message_id": tmsg_id,
                "video_message_id": video_id,
                "thumbnail_sha256": evidence.sha256_file(Path(thumbnail_path)) if thumbnail_path else None,
                "video_sha256": sha, "video_file_size": video_size,
                "channel": self.cfg.channel_id,
            })
        finally:
            telegram.close()

    # ── failure bookkeeping (24h retry window, then FAILED) ─────────────────────

    def _handle_failure(self, ep, exc: Exception) -> None:
        if _upload_outcome_unknown(exc):
            # the connection dropped AFTER the upload was sent: the message may be live.
            repo.mark_failed(self.conn, ep.id,
                             f"{exc} — publication INCERTAINE (connexion coupée pendant l'envoi): "
                             "vérifier le canal puis décision MANUELLE, pas de renvoi automatique")
            return
        if isinstance(exc, errors.PipelineError) and exc.code == errors.NOT_AVAILABLE_YET:
            self._handle_not_available(ep, exc)
            return
        if isinstance(exc, errors.PipelineError) and exc.code == errors.SOURCE_VIDEO_PROCESSING:
            self._handle_processing(ep, exc)
            return
        # 24h retry window anchored at the FIRST failure (not refreshed).
        keep_existing = bool(ep.retry_until_at) and not self._retry_exhausted(ep)
        if keep_existing:
            until_iso: str = ep.retry_until_at  # type: ignore[assignment]
        else:
            until_iso = (datetime.now(timezone.utc) + self.retry_window).isoformat()
        # next attempt after an exponential backoff (1, 2, 4 ... min, capped at 1 h).
        backoff_s = min(3600, 60 * 2 ** ep.retry_count)
        next_at = add_seconds(now_utc(), backoff_s)
        repo.set_retry_until(self.conn, ep.id, until_iso,
                             retry_count=ep.retry_count + 1, error=str(exc)[:600],
                             next_retry_at=next_at)
        repo.transition(self.conn, ep.id, State.RETRY_WAIT.value)
        # hand the item back to its queue so the worker can pick it up again.
        from .queues import QueueManager
        QueueManager(self.conn).unqueue(ep.anime_key, ep.id, "queued")

    def _handle_processing(self, ep, exc: Exception) -> None:
        """The player answers but the video file is not served (404): retry every few minutes, silently, inside the
        24 h window; ONE alert after `persistent_error_alert_minutes` (alerts.raise_persistent_errors)."""
        every_min = float(self.cfg.downloads.get("source_processing_retry_minutes", 3))
        until = ep.retry_until_at if (ep.retry_until_at and not self._retry_exhausted(ep)) else             (datetime.now(timezone.utc) + self.retry_window).isoformat()
        repo.set_retry_until(self.conn, ep.id, until, retry_count=ep.retry_count + 1, error=str(exc)[:600],
                             next_retry_at=add_seconds(now_utc(), every_min * 60))
        repo.transition(self.conn, ep.id, State.RETRY_WAIT.value)
        from .queues import QueueManager
        QueueManager(self.conn).unqueue(ep.anime_key, ep.id, "queued")

    def _handle_not_available(self, ep, exc: Exception) -> None:
        """Episode not published on the source yet: wait (fixed interval, long window), not a failure."""
        every_min = float(self.cfg.downloads.get("not_available_retry_minutes", 30))
        window_days = float(self.cfg.downloads.get("not_available_window_days", 30))
        until = ep.retry_until_at if (ep.retry_until_at and not self._retry_exhausted(ep)) else             (datetime.now(timezone.utc) + timedelta(days=window_days)).isoformat()
        repo.set_retry_until(self.conn, ep.id, until, retry_count=ep.retry_count,
                             error=str(exc)[:600],
                             next_retry_at=add_seconds(now_utc(), every_min * 60))
        repo.transition(self.conn, ep.id, State.RETRY_WAIT.value)
        from .queues import QueueManager
        QueueManager(self.conn).unqueue(ep.anime_key, ep.id, "queued")

    # ── helpers ──────────────────────────────────────────────────────────────────

    def _estimate_size(self, rendition, media_playlist) -> int | None:
        bw = getattr(rendition, "bandwidth_bps", None)
        dur = getattr(media_playlist, "total_duration_seconds", None)
        return int(bw * dur / 8) if bw and dur else None

    def _pending_anime_count(self) -> int:
        from .queues import QueueManager
        return QueueManager(self.conn).busy_anime_count()

    def _free_disk(self) -> int:
        import psutil
        p = app_config.DATA_DIR
        if not p.exists():
            p = p.parent
        return psutil.disk_usage(str(p)).free

    def _disk_gate(self, est: int | None, what: str) -> None:
        """Disk budget (dynamic scheduler) BEFORE consuming disk, and the proven upload ceiling."""
        if est is not None and est > self.max_publish_bytes:
            raise RuntimeError(f"{what} estimé {est//(1024*1024)} MiB > plafond PROVEN "
                               f"{self.max_publish_bytes//(1024*1024)} MiB (Phase 6) — ")
        if self.deps.scheduler is not None:
            allowance = self.deps.scheduler.tick(pending_animes=self._pending_anime_count(),
                                                 free_disk_bytes=self._free_disk(), next_estimated_bytes=est)
            if allowance.paused:
                raise RuntimeError(f"scheduler disk gate: {allowance.reason}")

    def _remember_duration(self, episode_id: int, seconds: float | None) -> None:
        """Validated duration of an episode: the reference for the direct-MP4 truncation check of its siblings."""
        if not seconds:
            return
        try:
            self.conn.execute("INSERT INTO control (ckey, cvalue, updated_at) VALUES (?, ?, ?) ON CONFLICT(ckey) "
                              "DO UPDATE SET cvalue=excluded.cvalue, updated_at=excluded.updated_at",
                              (f"duration:{episode_id}", str(float(seconds)), now_utc()))
            self.conn.commit()
        except Exception as exc:
            logger.warning("durée non enregistrée: %s", exc)

    def _reference_duration(self, ep) -> float | None:
        """Median validated duration of the OTHER published episodes of the same anime, None when there is none."""
        rows = self.conn.execute(
            "SELECT c.cvalue FROM control c JOIN episodes e ON c.ckey = 'duration:' || e.id "
            "WHERE e.anime_key = ? AND e.id <> ? AND e.video_message_id IS NOT NULL", (ep.anime_key, ep.id)).fetchall()
        vals = sorted(float(r[0]) for r in rows if r[0])
        return statistics.median(vals) if vals else None

    def _remember_player(self, episode_id: int, player: str) -> None:
        """Which player served the episode (shown in the panels)."""
        try:
            self.conn.execute("INSERT INTO control (ckey, cvalue, updated_at) VALUES (?, ?, ?) ON CONFLICT(ckey) "
                              "DO UPDATE SET cvalue=excluded.cvalue, updated_at=excluded.updated_at",
                              (f"player:{episode_id}", player, now_utc()))
            self.conn.commit()
        except Exception as exc:
            logger.warning("lecteur non enregistré: %s", exc)

    def _download_direct(self, extraction, output_path: Path):
        """Plain HTTP download of a direct MP4 (private `.part` file, size checked, then renamed)."""
        import httpx
        from types import SimpleNamespace
        part = output_path.with_suffix(".part")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        headers = {"Referer": extraction.direct_referer, "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")}
        written = 0
        with httpx.Client(timeout=httpx.Timeout(30, read=120), follow_redirects=True) as http:
            with http.stream("GET", extraction.direct_url, headers=headers) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code} sur le MP4 direct")
                expected = int(r.headers.get("content-length", 0)) or getattr(extraction, "direct_size", None)
                with open(part, "wb") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        fh.write(chunk)
                        written += len(chunk)
        if expected and written != expected:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"téléchargement incomplet : {written} / {expected} octets")
        part.replace(output_path)
        elapsed = max(0.001, time.monotonic() - t0)
        return SimpleNamespace(measurements=SimpleNamespace(
            download_duration_seconds=round(elapsed, 1), average_download_rate_bps=int(written * 8 / elapsed)))

    def _output_path(self, ep) -> Path:
        key = ep.episode_key.replace("https://", "").replace("http://", "").replace("/", "_")
        return evidence.evidence_dir("media", "downloads") / f"{key}.mp4"


def _upload_outcome_unknown(exc: BaseException) -> bool:
    """A video upload whose connection dropped: the message may be live (or appear minutes later)."""
    from .publisher import connection_lost
    return (isinstance(exc, errors.PipelineError) and exc.code == errors.TELEGRAM_VIDEO_FAILED
            and connection_lost(exc))


def cap_for_episode(ep) -> str:
    """Description from the DB row alone (no download facts): title from the anime key."""
    meta = MediaMetadata(
        title=ep.anime_key.replace("-", " ").title() if ep.anime_key else "?",
        episode=ep.episode_number, media_type=detect_media_type(ep.episode_url, ep.episode_number),
        language=detect_language(ep.episode_url, ep.language))
    return build_caption(meta)


def _block_unauthorized(ep, detail: str) -> None:
    """No-op: authorization gate removed."""
    pass


def _updated(ep: Episode, **kw) -> Episode:
    """Rebuild an Episode merging sparse updates (published_at truthy => now)."""
    from .timeutil import now_utc as _now
    return Episode(
        id=ep.id, anime_key=ep.anime_key, episode_key=ep.episode_key, source=ep.source,
        canonical_episode_url=ep.canonical_episode_url, language=ep.language,
        season=ep.season, episode_number=ep.episode_number, label=ep.label,
        episode_url=ep.episode_url, status=ep.status, queued_at=ep.queued_at,
        first_seen_at=ep.first_seen_at, retry_until_at=ep.retry_until_at,
        retry_count=ep.retry_count, last_error=ep.last_error, last_error_at=ep.last_error_at,
        media_hash=kw.get("media_hash", ep.media_hash),
        file_size=kw.get("file_size", ep.file_size),
        file_path=kw.get("file_path", ep.file_path),
        thumbnail_path=kw.get("thumbnail_path", ep.thumbnail_path),
        thumbnail_sha256=kw.get("thumbnail_sha256", ep.thumbnail_sha256),
        thumbnail_size=kw.get("thumbnail_size", ep.thumbnail_size),
        thumbnail_message_id=kw.get("thumbnail_message_id", ep.thumbnail_message_id),
        video_sha256=kw.get("video_sha256", ep.video_sha256),
        video_message_id=kw.get("video_message_id", ep.video_message_id),
        published_at=(_now() if kw.get("published_at") else ep.published_at),
        cleanup_at=ep.cleanup_at, attempt_count=ep.attempt_count,
        first_attempt_at=ep.first_attempt_at, last_attempt_at=ep.last_attempt_at,
        next_retry_at=ep.next_retry_at,
        created_at=ep.created_at, updated_at=_now(),
    )