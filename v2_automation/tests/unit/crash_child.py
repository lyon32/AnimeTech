"""Child process of test_crash_resume_real: runs ONE episode through the real DownloadManager with a real (slow, segment by
segment) download function and the real V2TelegramClient talking HTTP to a local fake Bot API.  It is killed from outside."""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1] / "src")]

from test_downloader import (FakeClient, FakeDownloadResult, FakePlaylist, FakeExtraction, FakeValidation, _cfg)  # noqa: E402

from v2_automation import db, downloader as dm, recovery, repo  # noqa: E402
from v2_automation.publisher import V2TelegramClient  # noqa: E402

SEGMENTS = 20


def main(db_path: str, work: str, api: str, eid: int, seg_delay: float, reuse: str) -> None:
    work_p = Path(work)
    conn = db.connect(Path(db_path))
    cfg = _cfg(work_p)
    cfg.downloads["dir"] = str(work_p / "media")
    cfg.telegram["api_base_url"] = api
    counter, segdir = work_p / "segments_served.log", work_p / "segments"
    segdir.mkdir(exist_ok=True)

    def download(playlist, output_path, ffmpeg, **kw):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        for i in range(SEGMENTS):
            seg = segdir / f"{i:03d}.ts"
            if reuse == "yes" and seg.exists() and seg.stat().st_size == 16:      # a valid, complete segment is kept
                continue
            time.sleep(seg_delay)
            tmp = seg.with_suffix(".part")
            tmp.write_bytes(b"S" * 16)
            tmp.replace(seg)                                                      # a segment is complete or absent
            with open(counter, "a") as fh:
                fh.write(f"{i}\n")
        output_path.write_bytes(b"".join((segdir / f"{i:03d}.ts").read_bytes() for i in range(SEGMENTS)))
        return FakeDownloadResult(output_path)

    tg = V2TelegramClient("123:TESTTOKEN", "-100x", api.rstrip("/") + "/bot", connect_timeout=5, read_timeout=60, write_timeout=60)
    deps = dm.DownloadDeps(
        extract=lambda url, client: FakeExtraction(), load_media_playlist=lambda t, u: FakePlaylist(), download=download,
        validate=lambda path, ffprobe, expected=None: FakeValidation(), sha256=lambda p: "sha" + "0" * 60,
        http_client=lambda: FakeClient(), ffmpeg=lambda: Path("ffmpeg"), ffprobe=lambda: Path("ffprobe"),
        telegram=lambda: tg, structure_check=lambda url: (True, "ok"),
        thumbnail=lambda v, out, ff, **kw: (out.parent.mkdir(parents=True, exist_ok=True), out.write_bytes(b"jpg"), out)[2])
    mgr = dm.DownloadManager(conn, cfg, deps=deps)
    print("RESULT", mgr.process_episode(eid), flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), float(sys.argv[5]), sys.argv[6])
