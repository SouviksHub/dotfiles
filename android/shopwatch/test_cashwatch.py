"""Scenario tests for the counter rules with synthetic keypoints.

    python -m unittest test_cashwatch.py
"""
import unittest

import numpy as np

from cashwatch import Counter

W, H = 640, 360
SPEC = {
    "staff_zone":    [[0.0, 0.0], [0.45, 0.0], [0.45, 1.0], [0.0, 1.0]],
    "customer_zone": [[0.6, 0.0], [1.0, 0.0], [1.0, 1.0], [0.6, 1.0]],
    "drawer_zone":   [[0.35, 0.75], [0.5, 0.75], [0.5, 0.95], [0.35, 0.95]],
    "exchange_zone": [[0.45, 0.4], [0.6, 0.4], [0.6, 0.7], [0.45, 0.7]],
}
NEUTRAL, DRAWER, EXCH, POCKET, UP = (230, 180), (270, 310), (330, 200), (160, 225), None


def body(cx, wrists, hands_up=False):
    k = np.zeros((17, 3), np.float32)
    k[:, 2] = 0.9
    k[0, :2] = (cx, 90)
    k[5, :2], k[6, :2] = (cx - 20, 120), (cx + 20, 120)
    k[11, :2], k[12, :2] = (cx - 15, 220), (cx + 15, 220)
    if hands_up:
        k[9, :2], k[10, :2] = (cx - 20, 60), (cx + 20, 60)
    else:
        k[9, :2], k[10, :2] = (cx - 30, 180), wrists
    return {"box": (cx - 40, 60, 80, 260), "score": 0.9, "kps": k}


class Sim:
    def __init__(self, spec=SPEC):
        self.events = []
        self.c = Counter(spec, W, H, lambda k, d, s, tid: self.events.append((k, s)))
        self.t = 1000.0

    def run(self, seconds, staff_wrist=NEUTRAL, customer=True, hands_up=False, fps=6):
        for _ in range(int(seconds * fps)):
            people = [body(150, staff_wrist, hands_up)]
            if customer:
                people.append(body(500, (520, 180)))
            self.c.update(self.t, people)
            self.t += 1 / fps

    def kinds(self, severity=None):
        return [k for k, s in self.events if severity is None or s == severity]


class CounterRules(unittest.TestCase):
    def test_honest_sale_no_alert(self):
        s = Sim()
        s.run(2)                       # customer arrives
        s.run(1, EXCH)                 # takes the cash
        s.run(1)
        s.run(1.5, DRAWER)             # puts it in the drawer, takes change
        s.run(1, EXCH)                 # hands over the change
        s.run(3)
        s.run(1, POCKET)               # later touches own phone pocket
        s.run(25, customer=False)      # customer leaves, transaction closes
        self.assertEqual(s.kinds("high"), [])
        self.assertIn("TXN", s.kinds())
        self.assertTrue(any(k == "TXN" for k in s.kinds("low")))

    def test_cash_to_pocket(self):
        s = Sim()
        s.run(2)
        s.run(1, EXCH)                 # takes the cash
        s.run(1)
        s.run(1, POCKET)               # straight to the pocket
        self.assertIn("CASH_TO_POCKET", s.kinds("high"))

    def test_drawer_to_pocket(self):
        s = Sim()
        s.run(2, customer=False)
        s.run(1.5, DRAWER, customer=False)
        s.run(2, customer=False)
        s.run(1, POCKET, customer=False)
        self.assertIn("DRAWER_TO_POCKET", s.kinds("high"))

    def test_no_sale_open(self):
        s = Sim()
        s.run(25, customer=False)      # clears the customer history window
        s.run(1.5, DRAWER, customer=False)
        s.run(22, customer=False)
        self.assertIn("NO_SALE_OPEN", s.kinds("medium"))

    def test_drawer_with_customer_is_not_no_sale(self):
        s = Sim()
        s.run(3)
        s.run(1.5, DRAWER)
        s.run(22)
        self.assertNotIn("NO_SALE_OPEN", s.kinds())

    def test_hands_up_robbery(self):
        s = Sim()
        s.run(1)
        s.run(3, hands_up=True)
        self.assertIn("HANDS_UP", s.kinds("high"))

    def test_brief_hand_raise_ignored(self):
        s = Sim()
        s.run(1, hands_up=True)        # stretching, < 2 s
        s.run(2)
        self.assertNotIn("HANDS_UP", s.kinds())

    def test_sensor_drawer_left_open(self):
        s = Sim(dict(SPEC, drawer_sensor=True))
        s.run(2)
        s.c.sensor(s.t, True)
        s.run(65)
        self.assertIn("DRAWER_LEFT_OPEN", s.kinds("medium"))

    def test_single_frame_glitch_not_pocket(self):
        s = Sim()
        s.run(2)
        s.run(1, EXCH)
        s.run(1 / 6, POCKET)           # one noisy frame: debounce rejects it
        s.run(1)
        self.assertNotIn("CASH_TO_POCKET", s.kinds())


if __name__ == "__main__":
    unittest.main()
