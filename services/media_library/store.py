"""`images` table + the two ways pictures get in: frames from analysed videos, files from a curated local library."""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from services.block_engine.media import ts_to_seconds
from services.media_library.frames import FrameError, extract_frame, image_size
from services.media_library.vision import fetch_youtube_thumbnail, index_shots

log = logging.getLogger(__name__)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
FRAME_BLOCK_TYPES = ("media", "key_moment")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ImageRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    kind: Literal["frame", "library"]
    job_id: str | None = None
    media_id: str | None = None
    block_id: str | None = None
    timestamp_seconds: float | None = None
    path: str
    width: int | None = None
    height: int | None = None
    size_bytes: int | None = None
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    suitable_for: list[str] = Field(default_factory=list)
    source_name: str | None = None
    sha256: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @property
    def timestamp(self) -> str | None:
        if self.timestamp_seconds is None:
            return None
        s = int(round(self.timestamp_seconds))
        return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"

    def offer_line(self) -> str:
        """How the writer sees it: one line per available picture."""
        where = f"frame @ {self.timestamp} from {self.source_name}" if self.kind == "frame" else f"library photo: {self.source_name}"
        extra = f" (suitable for: {', '.join(self.suitable_for)})" if self.suitable_for else ""
        tags = f" tags: {', '.join(self.tags)}" if self.tags else ""
        return f"- [img={self.id}] ({where}){extra} {self.description}{tags}".rstrip()


_COLS = "id, kind, job_id, media_id, block_id, timestamp_seconds, path, width, height, size_bytes, description, tags, suitable_for, source_name, sha256, created_at, updated_at"


class ImageStore:
    def __init__(self, pool, images_dir: Path):
        self.pool = pool
        self.dir = images_dir
        (self.dir / "frames").mkdir(parents=True, exist_ok=True)
        (self.dir / "library").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ CRUD
    def _insert(self, conn, rec: ImageRecord) -> None:
        conn.execute(
            f"INSERT INTO images ({_COLS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (rec.id, rec.kind, rec.job_id, rec.media_id, rec.block_id, rec.timestamp_seconds, rec.path, rec.width, rec.height, rec.size_bytes,
             rec.description, Jsonb(rec.tags), Jsonb(rec.suitable_for), rec.source_name, rec.sha256, rec.created_at, rec.updated_at),
        )

    def get(self, image_id: str) -> ImageRecord | None:
        with self.pool.connection() as conn:
            r = conn.execute(f"SELECT {_COLS} FROM images WHERE id = %s", (image_id,)).fetchone()
            return ImageRecord.model_validate(r) if r else None

    def get_many(self, ids: list[str]) -> dict[str, ImageRecord]:
        if not ids:
            return {}
        with self.pool.connection() as conn:
            rows = conn.execute(f"SELECT {_COLS} FROM images WHERE id = ANY(%s)", (list(ids),)).fetchall()
            return {r["id"]: ImageRecord.model_validate(r) for r in rows}

    def list(self, *, job_id: str | None = None, kind: str | None = None, limit: int = 500) -> list[ImageRecord]:
        with self.pool.connection() as conn:
            sql, params = f"SELECT {_COLS} FROM images WHERE true", []
            if job_id:
                sql += " AND job_id = %s"; params.append(job_id)
            if kind:
                sql += " AND kind = %s"; params.append(kind)
            rows = conn.execute(sql + " ORDER BY job_id, timestamp_seconds NULLS LAST, created_at DESC LIMIT %s", [*params, limit]).fetchall()
            return [ImageRecord.model_validate(r) for r in rows]

    def update(self, image_id: str, *, description: str | None = None, tags: list[str] | None = None, suitable_for: list[str] | None = None) -> ImageRecord | None:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                """UPDATE images SET description = COALESCE(%s, description), tags = COALESCE(%s, tags), suitable_for = COALESCE(%s, suitable_for), updated_at = %s
                   WHERE id = %s""",
                (description, Jsonb(tags) if tags is not None else None, Jsonb(suitable_for) if suitable_for is not None else None, _now(), image_id),
            )
        return self.get(image_id)

    def delete(self, image_id: str) -> bool:
        rec = self.get(image_id)
        if rec is None:
            return False
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM images WHERE id = %s", (image_id,))
        Path(rec.path).unlink(missing_ok=True)
        return True

    # ------------------------------------------------------------------ frames from a video
    def frames_for_job(self, job, blocks: list, *, force: bool = False, max_frames: int = 12) -> list[ImageRecord]:
        """One frame per media / key_moment block of the job (idempotent on block_id). Needs the job's local file."""
        if not job.source.path or not Path(job.source.path).exists():
            return self.list(job_id=job.id, kind="frame")
        video = Path(job.source.path)
        duration = job.source.duration_seconds
        existing = {r.block_id: r for r in self.list(job_id=job.id, kind="frame") if r.block_id}
        wanted = [b for b in blocks if b.block_type in FRAME_BLOCK_TYPES and b.timestamp]
        wanted.sort(key=lambda b: (b.block_type != "media", 0 if b.payload.get("importance") == "high" else 1, ts_to_seconds(b.timestamp) or 0))
        out: list[ImageRecord] = []
        for b in wanted[:max_frames]:
            if b.block_id in existing and not force:
                out.append(existing[b.block_id])
                continue
            t = ts_to_seconds(b.timestamp)
            if t is None:
                continue
            ordinal = b.block_id.rsplit(":", 1)[-1]
            dest = self.dir / "frames" / job.id / f"{b.block_type}_{ordinal}.jpg"
            try:
                _, w, h = extract_frame(video, float(t), dest, duration=duration)
            except FrameError as exc:
                log.warning("frame for %s failed: %s", b.block_id, exc)
                continue
            rec = ImageRecord(
                kind="frame", job_id=job.id, media_id=job.media_id, block_id=b.block_id, timestamp_seconds=float(t), path=str(dest),
                width=w, height=h, size_bytes=dest.stat().st_size, description=b.payload.get("description") or b.text,
                suitable_for=list(b.payload.get("suitable_for") or []) if b.block_type == "media" else [],
                tags=["blocks", b.block_type] + ([b.payload["importance"]] if b.payload.get("importance") else []), source_name=job.source.name,
            )
            with self.pool.connection() as conn, conn.transaction():
                if b.block_id in existing:
                    conn.execute("DELETE FROM images WHERE id = %s", (existing[b.block_id].id,))
                self._insert(conn, rec)
            out.append(rec)
        return out

    def index_job(self, job, blocks: list, provider, *, force: bool = False, institution_context: str = "", model: str | None = None,
                  max_shots: int = 60) -> tuple[list[ImageRecord], dict]:
        """AI-verified stills: shot detection + the gateway describing what each still really shows. Replaces earlier AI stills
        of the job; block-based frames whose block an AI still matches are superseded. Editor picks are kept."""
        if not job.source.path or not Path(job.source.path).exists():
            raise FrameError(f"job {job.id} has no local video file")
        existing = [r for r in self.list(job_id=job.id, kind="frame") if "ai" in r.tags]
        if existing and not force:
            return existing, {"cached": True}
        video = Path(job.source.path)
        shots, meta = index_shots(video, duration=job.source.duration_seconds, source_name=job.source.name, blocks=blocks, provider=provider,
                                  institution_context=institution_context, model=model, max_shots=max_shots)
        out: list[ImageRecord] = []
        with self.pool.connection() as conn, conn.transaction():
            for r in existing:
                conn.execute("DELETE FROM images WHERE id = %s", (r.id,))
                Path(r.path).unlink(missing_ok=True)
            taken: set[str] = set()
            for i, sh in enumerate(shots):
                if sh.quality != "good":
                    continue
                dest = self.dir / "frames" / job.id / f"shot_{i:03d}.jpg"
                try:
                    _, w, h = extract_frame(video, sh.time, dest, duration=job.source.duration_seconds, offsets=(0.0,))
                except FrameError as exc:
                    log.warning("shot %.1fs of %s failed: %s", sh.time, job.id, exc)
                    continue
                block_id = sh.block_id if sh.block_id and sh.block_id not in taken else None
                if block_id:   # the AI still now owns that block (an earlier timestamp-based frame is dropped)
                    old = conn.execute("SELECT id, path FROM images WHERE block_id = %s", (block_id,)).fetchone()
                    if old:
                        conn.execute("DELETE FROM images WHERE id = %s", (old["id"],))
                        Path(old["path"]).unlink(missing_ok=True)
                    taken.add(block_id)
                rec = ImageRecord(kind="frame", job_id=job.id, media_id=job.media_id, block_id=block_id, timestamp_seconds=round(sh.time, 2), path=str(dest),
                                  width=w, height=h, size_bytes=dest.stat().st_size, description=sh.description or f"Still at {sh.time:.0f}s",
                                  tags=["shot", "ai", sh.quality] + ([f"people:{sh.people}"] if sh.people else []), suitable_for=sh.suitable_for,
                                  source_name=job.source.name)
                self._insert(conn, rec)
                out.append(rec)
        meta["stored"] = len(out)
        meta["rejected"] = sum(1 for sh in shots if sh.quality != "good")
        return out, meta

    def youtube_thumbnail_for_job(self, job, *, title: str | None = None) -> ImageRecord | None:
        """For jobs analysed from a YouTube URL (no local file): the video's own thumbnail as a candidate picture."""
        if not job.source.url:
            return None
        for r in self.list(job_id=job.id, kind="frame"):
            if "youtube_thumbnail" in r.tags:
                return r
        dest = self.dir / "frames" / job.id / "youtube_thumbnail.jpg"
        if fetch_youtube_thumbnail(job.source.url, dest) is None:
            return None
        w, h = image_size(dest)
        rec = ImageRecord(kind="frame", job_id=job.id, media_id=job.media_id, path=str(dest), width=w, height=h, size_bytes=dest.stat().st_size,
                          description=f"YouTube thumbnail of the video{(': ' + title) if title else ''}", tags=["youtube_thumbnail"],
                          suitable_for=["blog_hero", "social_post"], source_name=job.source.url)
        with self.pool.connection() as conn, conn.transaction():
            self._insert(conn, rec)
        return rec

    def frame_at(self, job, at: float, *, description: str = "", tags: list[str] | None = None) -> ImageRecord:
        """An editor-chosen frame at an arbitrary position."""
        if not job.source.path or not Path(job.source.path).exists():
            raise FrameError(f"job {job.id} has no local video file")
        dest = self.dir / "frames" / job.id / f"at_{int(round(at * 10)):06d}.jpg"
        _, w, h = extract_frame(Path(job.source.path), at, dest, duration=job.source.duration_seconds, offsets=(0.0,))
        rec = ImageRecord(kind="frame", job_id=job.id, media_id=job.media_id, timestamp_seconds=float(at), path=str(dest), width=w, height=h,
                          size_bytes=dest.stat().st_size, description=description, tags=tags or ["editor"], source_name=job.source.name)
        with self.pool.connection() as conn, conn.transaction():
            old = conn.execute("SELECT id FROM images WHERE job_id = %s AND path = %s", (job.id, str(dest))).fetchone()
            if old:
                conn.execute("DELETE FROM images WHERE id = %s", (old["id"],))
            self._insert(conn, rec)
        return rec

    # ------------------------------------------------------------------ library
    def add_library_file(self, src: Path, *, description: str, tags: list[str] | None = None, suitable_for: list[str] | None = None,
                         original_name: str | None = None, move: bool = False) -> ImageRecord:
        ext = src.suffix.lower()
        if ext not in IMAGE_EXTENSIONS:
            raise ValueError(f"unsupported image type {ext} (use {', '.join(sorted(IMAGE_EXTENSIONS))})")
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        with self.pool.connection() as conn:
            dup = conn.execute(f"SELECT {_COLS} FROM images WHERE sha256 = %s", (digest,)).fetchone()
        if dup:
            return ImageRecord.model_validate(dup)
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", (original_name or src.name)).strip("._")[:80] or f"image{ext}"
        dest = self.dir / "library" / f"{digest[:12]}_{name}"
        (shutil.move if move else shutil.copy)(str(src), str(dest))
        w, h = image_size(dest)
        rec = ImageRecord(kind="library", path=str(dest), width=w, height=h, size_bytes=dest.stat().st_size, description=description.strip(),
                          tags=[t.strip() for t in (tags or []) if t.strip()], suitable_for=suitable_for or [], source_name=original_name or src.name, sha256=digest)
        with self.pool.connection() as conn, conn.transaction():
            self._insert(conn, rec)
        return rec

    def import_folder(self, folder: Path, *, tags: list[str] | None = None) -> list[ImageRecord]:
        """Every image in a folder (not recursive). A sidecar <name>.txt or a 'captions.txt' (name<TAB>description) gives descriptions;
        otherwise the file name, de-underscored, is the description - edit it afterwards."""
        captions: dict[str, str] = {}
        cap_file = folder / "captions.txt"
        if cap_file.exists():
            for ln in cap_file.read_text(encoding="utf-8").splitlines():
                if "\t" in ln:
                    k, v = ln.split("\t", 1)
                    captions[k.strip()] = v.strip()
        out = []
        for f in sorted(folder.iterdir()):
            if not f.is_file() or f.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            side = f.with_suffix(".txt")
            desc = captions.get(f.name) or (side.read_text(encoding="utf-8").strip() if side.exists() else "") or re.sub(r"[_\-]+", " ", f.stem).strip()
            try:
                out.append(self.add_library_file(f, description=desc, tags=list(tags or []) + [folder.name], original_name=f.name))
            except (ValueError, OSError) as exc:
                log.warning("skipping %s: %s", f.name, exc)
        return out

    # ------------------------------------------------------------------ offers for the writer
    def offers(self, *, job_ids: list[str], brief: str, max_frames: int = 24, max_library: int = 10) -> list[ImageRecord]:
        """Frames of the evidence videos + the library photos that best match the brief (simple keyword overlap)."""
        frames: list[ImageRecord] = []
        for jid in job_ids:
            mine = self.list(job_id=jid, kind="frame")
            if any("ai" in r.tags for r in mine):   # verified stills exist: timestamp-guessed frames are not offered any more
                mine = [r for r in mine if "blocks" not in r.tags]
            mine.sort(key=lambda r: (0 if "blog_hero" in r.suitable_for else 1, 0 if ("ai" in r.tags or "youtube_thumbnail" in r.tags) else 1, r.timestamp_seconds or 0))
            frames += mine
        frames = frames[:max_frames]
        words = {w for w in re.findall(r"[a-zA-Zऀ-ॿ]{3,}", brief.lower())}
        scored = []
        for rec in self.list(kind="library"):
            hay = f"{rec.description} {' '.join(rec.tags)} {rec.source_name or ''}".lower()
            score = sum(1 for w in words if w in hay)
            scored.append((score, rec))
        scored.sort(key=lambda x: (-x[0], x[1].created_at), reverse=False)
        library = [r for s, r in scored if s > 0][:max_library]
        if not library:   # nothing matched by keyword: still offer the newest few, the writer may find a fit
            library = [r for _, r in scored][: min(4, max_library)]
        return frames + library
