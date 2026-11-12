"""
Crowd measurement for live camera frames.

Four things make a camera reading trustworthy enough to show a traveler:
1. A real object detector (YOLOv8n, COCO classes) instead of HOG + blob/contour heuristics.
   Falls back to OpenCV's HOG people detector if ultralytics is not installed, and says so.
2. Busyness relative to the camera's own normal range. 40 people is quiet for Times Square
   and packed for a cafe, so every camera keeps its observed min/max and is scored against them.
3. The place's local time (from its coordinates), not the server's clock.
4. A confidence level with reasons: frames disagree, the stream is dark, too few frames,
   the camera has no baseline yet, or the fallback detector was used.

Supply-chain note: YOLO .pt weights are Python pickles, so loading a tampered file runs
arbitrary code. The weights are pinned by SHA-256 and checked before every load; a mismatch
disables YOLO (fail closed to the HOG fallback) rather than loading an unknown file.
"""

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests

try:
    import cv2
    import numpy as np
    OPENCV_AVAILABLE = True
except ImportError:  # pragma: no cover
    OPENCV_AVAILABLE = False

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_PATH = Path(os.getenv("YOLO_MODEL_PATH", REPO_ROOT / "models" / "yolov8n.pt"))
# Official ultralytics asset v8.4.0/yolov8n.pt. Change only after verifying a new file.
YOLO_SHA256 = "f59b3d833e2ff32e194b5bb8e08d211dc7c5bdf144b90d2c8412c47ccfc83b36"
YOLO_URL = "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8n.pt"

PERSON_CLASS = 0
VEHICLE_CLASSES = {1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
MIN_CONFIDENCE = 0.25
IMG_SIZE = 1280           # city cams are wide shots; 640 misses most people
TINY_OBJECT_FRACTION = 0.05  # median box under 5% of frame height = far-away camera
MIN_GAP_MINUTES = 30      # readings closer than this replace, not add (baseline needs different times)

BASELINE_FILE = REPO_ROOT / "traffic_crowd_data" / "camera_baselines.json"
BASELINE_MIN_OBSERVATIONS = 5     # below this, the camera is still "calibrating"
BASELINE_MAX_OBSERVATIONS = 500   # keep the file bounded
DARK_BRIGHTNESS = 50              # mean gray level (0-255) below which a frame is "dark"

_model = None
_model_error: Optional[str] = None
_model_lock = threading.Lock()
_baseline_lock = threading.Lock()


# ---------------------------------------------------------------- detector

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_weights() -> None:
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MODEL_PATH.with_suffix(".part")
    with requests.get(YOLO_URL, stream=True, timeout=30) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    os.replace(tmp, MODEL_PATH)


def _load_yolo():
    """Load YOLOv8n once. Returns the model, or None (with _model_error set) to use the fallback."""
    global _model, _model_error
    if _model is not None or _model_error is not None:
        return _model
    with _model_lock:
        if _model is not None or _model_error is not None:
            return _model
        try:
            os.environ.setdefault("YOLO_OFFLINE", "1")  # no update checks at runtime
            from ultralytics import YOLO, settings
            settings.update({"sync": False})            # no usage analytics
            if not MODEL_PATH.exists():
                _download_weights()
            digest = _sha256(MODEL_PATH)
            if digest != YOLO_SHA256:
                _model_error = "YOLO weights failed the SHA-256 check; not loading them"
                return None
            _model = YOLO(str(MODEL_PATH))
        except ImportError:
            _model_error = "ultralytics not installed"
        except Exception as e:  # network, corrupt file, torch problems
            _model_error = f"YOLO unavailable: {str(e)[:120]}"
    return _model


def detector_name() -> str:
    return "YOLOv8n" if _load_yolo() is not None else "OpenCV HOG (fallback)"


def detect(frame) -> Dict[str, Any]:
    """Count people and vehicles in one BGR frame."""
    model = _load_yolo()
    if model is not None:
        result = model.predict(frame, conf=MIN_CONFIDENCE, imgsz=IMG_SIZE, verbose=False)[0]
        classes = [int(c) for c in result.boxes.cls.tolist()] if result.boxes is not None else []
        heights = [(b[3] - b[1]) / frame.shape[0] for b in result.boxes.xyxy.tolist()] if classes else []
        people = sum(1 for c in classes if c == PERSON_CLASS)
        vehicles = sum(1 for c in classes if c in VEHICLE_CLASSES)
        return {"people": people, "vehicles": vehicles, "detector": "YOLOv8n",
                "median_box_height": sorted(heights)[len(heights) // 2] if heights else None}
    # Fallback: HOG finds upright, mid-sized pedestrians only; it cannot count vehicles.
    h, w = frame.shape[:2]
    small = cv2.resize(frame, (800, int(h * 800 / w))) if w > 800 else frame
    hog = cv2.HOGDescriptor()
    hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
    boxes, _ = hog.detectMultiScale(small, winStride=(8, 8), padding=(16, 16), scale=1.05)
    return {"people": len(boxes), "vehicles": None, "detector": "OpenCV HOG (fallback)",
            "median_box_height": None}


def brightness(frame) -> float:
    return float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean())


# ---------------------------------------------------------------- per-camera baseline

def camera_id(url: str) -> str:
    """Stable id for a camera: the YouTube video id, else a hash of the URL."""
    p = urlparse(url or "")
    vid = parse_qs(p.query).get("v", [None])[0] if "youtube.com" in (p.hostname or "") else None
    if not vid and (p.hostname or "") == "youtu.be":
        vid = p.path.lstrip("/")
    if vid and re.fullmatch(r"[A-Za-z0-9_-]{6,20}", vid):
        return vid
    return hashlib.sha256((url or "").encode()).hexdigest()[:16]


def _read_baselines() -> Dict[str, Any]:
    try:
        with open(BASELINE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def update_baseline(cam: str, people: float, vehicles: Optional[float]) -> Dict[str, Any]:
    """Record one reading for a camera and return its range *before* this reading."""
    with _baseline_lock:
        data = _read_baselines()
        entry = data.get(cam) or {"people": [], "vehicles": []}
        before = dict(entry)
        try:
            last = datetime.fromisoformat(entry.get("updated", ""))
            recent = (datetime.now(timezone.utc) - last).total_seconds() < MIN_GAP_MINUTES * 60
        except ValueError:
            recent = False
        if recent and entry.get("people"):
            # Many checks in a few minutes would fake a "normal range"; keep one per time slot.
            entry["people"] = entry["people"][:-1]
            if entry.get("vehicles"):
                entry["vehicles"] = entry["vehicles"][:-1]
            before = {"people": entry["people"], "vehicles": entry.get("vehicles", [])}
        entry["people"] = (entry.get("people", []) + [round(people, 1)])[-BASELINE_MAX_OBSERVATIONS:]
        if vehicles is not None:
            entry["vehicles"] = (entry.get("vehicles", []) + [round(vehicles, 1)])[-BASELINE_MAX_OBSERVATIONS:]
        entry["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        data[cam] = entry
        BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = BASELINE_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, BASELINE_FILE)  # atomic: a crash never leaves half a file
    return before


def _relative(value: Optional[float], history: List[float]) -> Dict[str, Any]:
    if value is None:
        return {"level": "unknown", "reason": "not measured by this detector"}
    n = len(history)
    if n < BASELINE_MIN_OBSERVATIONS:
        return {"level": "calibrating", "observations": n,
                "reason": f"camera has {n}/{BASELINE_MIN_OBSERVATIONS} past readings; no normal range yet"}
    lo, hi = min(history), max(history)
    if hi - lo < 2:
        # e.g. always 0-1 people: the range is too narrow to rank busy vs quiet
        return {"level": "typical for this camera" if lo - 1 <= value <= hi + 1 else
                ("busier than usual" if value > hi else "quieter than usual"),
                "camera_normal_range": {"min": lo, "max": hi}, "observations": n,
                "reason": "this camera's past readings barely vary"}
    score = max(0.0, min(1.0, (value - lo) / (hi - lo)))
    level = ("quiet" if score < 0.25 else "moderate" if score < 0.5
             else "busy" if score < 0.75 else "very busy")
    if value > hi:
        level = "busier than ever seen on this camera"
    elif value < lo:
        level = "quieter than ever seen on this camera"
    return {"level": level, "score_0_to_1": round(score, 2),
            "camera_normal_range": {"min": lo, "max": hi}, "observations": n}


# ---------------------------------------------------------------- local time

def local_time(lat: float, lon: float) -> Optional[Dict[str, str]]:
    """Local time at a coordinate, via Open-Meteo's timezone lookup (free, no key)."""
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast",
                         params={"latitude": lat, "longitude": lon, "timezone": "auto",
                                 "current": "is_day"}, timeout=10)
        r.raise_for_status()
        tz_name = r.json().get("timezone")
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(tz_name))
        return {"timezone": tz_name, "local_time": now.strftime("%Y-%m-%d %H:%M"),
                "weekday": now.strftime("%A")}
    except Exception:
        return None


# ---------------------------------------------------------------- full analysis

def analyze_frames(frames: List[Any], cam: Optional[str] = None) -> Dict[str, Any]:
    """Detect in every frame, aggregate, score against the camera baseline, and rate confidence."""
    per_frame = []
    for fr in frames:
        d = detect(fr)
        d["brightness"] = round(brightness(fr), 1)
        per_frame.append(d)

    people = [d["people"] for d in per_frame]
    vehicles = [d["vehicles"] for d in per_frame if d["vehicles"] is not None]
    avg_people = round(mean(people), 1)
    avg_vehicles = round(mean(vehicles), 1) if vehicles else None
    avg_light = round(mean(d["brightness"] for d in per_frame), 1)
    used = per_frame[0]["detector"]

    result: Dict[str, Any] = {
        "detector": used,
        "frames_analyzed": len(per_frame),
        "people": {"average": avg_people, "per_frame": people},
        "vehicles": {"average": avg_vehicles, "per_frame": vehicles or None},
        "average_brightness_0_255": avg_light,
    }

    calibrated = False
    if cam:
        before = update_baseline(cam, avg_people, avg_vehicles)
        result["camera_id"] = cam
        result["relative_to_this_camera"] = {
            "people": _relative(avg_people, before.get("people", [])),
            "vehicles": _relative(avg_vehicles, before.get("vehicles", [])),
        }
        calibrated = len(before.get("people", [])) >= BASELINE_MIN_OBSERVATIONS

    # Confidence: start high and step down for each problem; report every reason.
    reasons = []
    penalty = 0
    if used != "YOLOv8n":
        reasons.append("fallback detector (HOG) misses small, seated and partly hidden people")
        penalty += 1
    if avg_light < DARK_BRIGHTNESS:
        reasons.append(f"stream is dark (brightness {avg_light}/255)")
        penalty += 2
    if len(people) < 3:
        reasons.append(f"only {len(people)} frame(s) analyzed")
        penalty += 1
    if len(people) >= 2 and avg_people >= 3:
        spread = pstdev(people) / avg_people
        if spread > 0.5:
            reasons.append(f"frames disagree (people per frame: {people})")
            penalty += 2
        elif spread > 0.25:
            reasons.append(f"frames vary somewhat (people per frame: {people})")
            penalty += 1
    sizes = [d["median_box_height"] for d in per_frame if d.get("median_box_height")]
    # Lower bound if objects are tiny, or cars are visible but not one person was found
    # (on a city street that means people are too small or hidden, not absent).
    tiny = (bool(sizes) and mean(sizes) < TINY_OBJECT_FRACTION) or (
        avg_people < 1 and (avg_vehicles or 0) >= 3)
    result["counts_are_lower_bound"] = tiny
    if tiny:
        reasons.append("wide or high camera view: people are only a few pixels tall, so counts "
                       "are a lower bound (compare with this camera's history, not other cameras)")
        penalty += 2
    if cam and not calibrated:
        reasons.append("no normal range for this camera yet, so 'busy' vs 'quiet' is not known")
        penalty += 1
    level = "high" if penalty == 0 else "medium" if penalty <= 1 else "low"
    result["confidence"] = {"level": level, "reasons": reasons or ["frames agree, good light, calibrated camera"]}
    return result
