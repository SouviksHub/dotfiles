import io

import numpy as np
from PIL import Image

from sentinel.db import DB
from sentinel.rules import Rules
from sentinel.watchdog import Condition, Watchdog, best_match, is_covered, to_gray, MOVED_CORR

RNG = np.random.default_rng(7)


def jpeg(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def scene(seed: int) -> np.ndarray:
    """A 'shop' of random shelves/boxes, so edge structure is distinctive."""
    rng = np.random.default_rng(seed)
    img = np.full((360, 640), 90.0)
    for _ in range(40):
        y, x = rng.integers(0, 330), rng.integers(0, 600)
        img[y:y + rng.integers(10, 60), x:x + rng.integers(10, 90)] = rng.integers(0, 255)
    return img


SHOP = scene(1)


def test_same_view_under_different_lighting_matches():
    ref = to_gray(jpeg(SHOP))
    darker = to_gray(jpeg(SHOP * 0.55 + 10))           # evening lighting
    noisy = to_gray(jpeg(SHOP + RNG.normal(0, 6, SHOP.shape)))  # sensor noise
    assert best_match(darker, [ref]) > 0.8
    assert best_match(noisy, [ref]) > 0.6


def test_moved_camera_detected():
    ref = to_gray(jpeg(SHOP))
    other_view = to_gray(jpeg(scene(2)))
    panned = to_gray(jpeg(np.roll(SHOP, 200, axis=1)))
    assert best_match(other_view, [ref]) < MOVED_CORR
    assert best_match(panned, [ref]) < MOVED_CORR


def test_covered_lens_detected():
    assert is_covered(to_gray(jpeg(np.full((360, 640), 8.0) + RNG.normal(0, 2, (360, 640)))))
    assert is_covered(to_gray(jpeg(np.full((360, 640), 250.0))))   # torch shone into lens
    assert not is_covered(to_gray(jpeg(SHOP)))


def test_condition_debounce_fires_once_then_clears():
    c = Condition(needed=3)
    assert [c.update(True) for _ in range(5)] == [None, None, "raised", None, None]
    assert c.update(False) == "cleared"
    assert c.update(False) is None


class FakeFrigate:
    def __init__(self):
        self.fps, self.frame, self.fail = 5.0, jpeg(SHOP), False

    def stats(self):
        if self.fail:
            raise RuntimeError("connection refused")
        return {"cameras": {"front": {"camera_fps": self.fps}}}

    def latest_frame(self, camera):
        return self.frame


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def photo(self, caption, jpeg_bytes):
        self.sent.append(caption)

    def message(self, text):
        self.sent.append(text)


def make(tmp_path):
    fr, tg = FakeFrigate(), FakeTelegram()
    rules = Rules.from_dict({"timezone": "Asia/Dhaka"})
    w = Watchdog(frigate=fr, telegram=tg, rules=rules, db=DB(tmp_path / "s.db"),
                 ref_dir=tmp_path / "refs", backup_dir=tmp_path / "bk")
    return w, fr, tg


def test_watchdog_end_to_end(tmp_path):
    w, fr, tg = make(tmp_path)
    assert w.check_once() == []                       # first frame becomes the reference
    assert len(list((tmp_path / "refs").glob("front-*.jpg"))) == 1

    fr.fps = 0                                        # camera unplugged
    w.check_once(); w.check_once(); w.check_once()
    assert sum("OFFLINE" in m for m in tg.sent) == 1  # one alert, not a flood
    fr.fps = 5
    assert w.check_once() == []
    assert any(m.startswith("Resolved: front") for m in tg.sent)

    fr.frame = jpeg(np.full((360, 640), 5.0))         # lens covered
    for _ in range(3):
        problems = w.check_once()
    assert problems == ["front covered"] and any("COVERED" in m for m in tg.sent)

    fr.frame = jpeg(np.roll(SHOP, 200, axis=1))       # camera turned away
    for _ in range(3):
        problems = w.check_once()
    assert problems == ["front moved"] and any("MOVED" in m for m in tg.sent)

    fr.fail = True                                    # whole recorder down
    w.check_once(); w.check_once()
    assert any("not responding" in m for m in tg.sent)


def test_reference_rotation_and_backup(tmp_path):
    w, fr, _ = make(tmp_path)
    for _ in range(6):
        w.add_reference("front")
    assert len(list((tmp_path / "refs").glob("front-*.jpg"))) <= 4
    dst = w.backup_db()
    assert dst.exists() and dst.stat().st_size > 0
