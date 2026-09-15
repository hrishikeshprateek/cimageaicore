"""Where should a cover-crop look? Sample frames across the cut, find the faces, keep the speaker in frame.

A 16:9 talk dropped into the 9:16 reel keeps only ~66% of its width, so a centre crop loses a presenter who
stands at the side of a wide shot. Face detection is OpenCV's YuNet (the ONNX model is vendored under
services/video_composer/models - nothing is downloaded at run time). Without cv2 or the model the
suggestion is simply the centre; the composer UI slider still lets a human place the window.
"""
from __future__ import annotations

import logging
import statistics
import tempfile
from pathlib import Path

from pydantic import BaseModel

from services.video_composer import ffmpeg as ff

log = logging.getLogger(__name__)

MODEL_PATH = Path(__file__).parent / "models" / "face_detection_yunet_2023mar.onnx"
SCORE_THRESHOLD = 0.7
DEFAULT_SAMPLES = 5
FRAME_WIDTH = 640

try:  # optional dependency: pip install "cimage-ai[composer]"
    import cv2  # type: ignore

    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)   # else every detector build prints a backend warning
except Exception:  # noqa: BLE001 - ImportError or a broken native wheel; both mean "no detector"
    cv2 = None


class Face(BaseModel):
    x: float                     # normalised 0..1 of the frame
    y: float
    w: float
    h: float
    score: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


class Subject(BaseModel):
    """Where the people are, across the sampled frames (normalised source coordinates)."""

    center_x: float | None = None        # area-weighted centre of all faces, median over frames
    center_y: float | None = None
    method: str = "center"               # faces | center (no detector, or nothing found)
    frames_sampled: int = 0
    frames_with_faces: int = 0
    faces_total: int = 0


def detector_available() -> bool:
    return cv2 is not None and MODEL_PATH.exists()


_DETECTORS: dict[tuple[int, int], object] = {}


def _detector(w: int, h: int):
    det = _DETECTORS.get((w, h))   # sampled frames share one size, so this is built once per process
    if det is None:
        det = cv2.FaceDetectorYN.create(str(MODEL_PATH), "", (w, h), SCORE_THRESHOLD, 0.3, 50)
        det.setInputSize((w, h))
        _DETECTORS[(w, h)] = det
    return det


def detect_faces(image: Path) -> list[Face]:
    """Faces in one image, normalised to 0..1. Empty when the detector is unavailable or nothing scores above threshold."""
    if not detector_available():
        return []
    img = cv2.imread(str(image))
    if img is None:
        return []
    h, w = img.shape[:2]
    _, found = _detector(w, h).detect(img)
    faces: list[Face] = []
    for row in (found if found is not None else []):
        x, y, fw, fh, score = float(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[-1])
        if fw <= 0 or fh <= 0:
            continue
        faces.append(Face(x=max(0.0, x / w), y=max(0.0, y / h), w=min(1.0, fw / w), h=min(1.0, fh / h), score=score))
    return faces


def sample_times(cut_in: float, cut_out: float, samples: int = DEFAULT_SAMPLES) -> list[float]:
    """Evenly spaced instants strictly inside the cut (the edges are often a transition or a black frame)."""
    dur = max(0.0, cut_out - cut_in)
    n = max(1, min(samples, int(dur // 0.5) or 1))
    return [round(cut_in + (i + 0.5) * dur / n, 3) for i in range(n)]


def locate_subject(video: Path, cut_in: float, cut_out: float, *, samples: int = DEFAULT_SAMPLES) -> Subject:
    """Sample frames across the cut and return where the faces are. Never raises: a failure is a 'center' answer."""
    if not detector_available():
        return Subject(method="center")
    xs: list[float] = []
    ys: list[float] = []
    sampled = faces_total = 0
    with tempfile.TemporaryDirectory(prefix="framing-") as tmp:
        for i, t in enumerate(sample_times(cut_in, cut_out, samples)):
            frame = ff.poster_frame(video, Path(tmp) / f"f{i}.jpg", at=t, max_width=FRAME_WIDTH)
            if frame is None:
                continue
            sampled += 1
            try:
                faces = detect_faces(frame)
            except Exception as exc:  # noqa: BLE001 - detector hiccup on one frame must not sink the request
                log.warning("face detection failed at %.2fs: %s", t, exc)
                continue
            if not faces:
                continue
            faces_total += len(faces)
            area = sum(f.w * f.h for f in faces)
            xs.append(sum(f.cx * f.w * f.h for f in faces) / area)
            ys.append(sum(f.cy * f.w * f.h for f in faces) / area)
    if not xs:
        return Subject(method="center", frames_sampled=sampled)
    return Subject(center_x=round(statistics.median(xs), 4), center_y=round(statistics.median(ys), 4), method="faces",
                   frames_sampled=sampled, frames_with_faces=len(xs), faces_total=faces_total)


def focus_for_center(center: float, kept_fraction: float) -> float:
    """Focus value (0..1, see template.cover_crop) that centres the kept window on `center`.

    kept_fraction = crop_w / scaled_w along the cropped axis. With nothing cropped any focus is equivalent -> 0.5."""
    if kept_fraction >= 1.0:
        return 0.5
    focus = (center - kept_fraction / 2) / (1.0 - kept_fraction)
    return round(min(max(focus, 0.0), 1.0), 4)
