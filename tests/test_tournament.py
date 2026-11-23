"""Tournament logic tests. Run from the repository root:  python -m unittest"""

import unittest

from chess_player.tournament import Tournament


def play_round(t: Tournament, rnd: int, result: str = "1-0"):
    pairs, bye = t.pairings(rnd)
    if bye is not None and t.fmt == "swiss":
        t.record_bye(rnd, bye)
    for w, b in pairs:
        t.record(rnd, w, b, result)


class RoundRobinTest(unittest.TestCase):
    def test_every_pair_meets_once_per_cycle(self):
        t = Tournament(list("abcd"), "rr", 1)
        pairs = [frozenset(p) for r in range(t.total_rounds) for p in t.pairings(r)[0]]
        self.assertEqual(len(pairs), 6)
        self.assertEqual(len(set(pairs)), 6)

    def test_odd_number_sits_out(self):
        t = Tournament(list("abc"), "rr", 1)
        self.assertEqual(t.total_games, 3)
        self.assertTrue(all(t.pairings(r)[1] is not None for r in range(t.total_rounds)))


class SwissTest(unittest.TestCase):
    def test_no_rematch_while_avoidable(self):
        t = Tournament(list("abcdef"), "swiss", 3)
        for r in range(3):
            play_round(t, r)
        meetings = [frozenset((g["white"], g["black"])) for g in t.games]
        self.assertEqual(len(meetings), len(set(meetings)))

    def test_bye_rotates(self):
        t = Tournament(list("abcde"), "swiss", 3)
        for r in range(3):
            play_round(t, r)
        byes = [e["player"] for e in t.events if e["type"] == "bye"]
        self.assertEqual(len(byes), len(set(byes)))


class ExtendTest(unittest.TestCase):
    def test_set_count_keeps_played_rounds(self):
        for fmt, n in (("rr", 3), ("rr", 4), ("swiss", 5)):
            t = Tournament([str(i) for i in range(n)], fmt, 2)
            before = [t.pairings(r) for r in range(2)]
            for r in range(2):
                play_round(t, r)
            t.set_count(4)
            self.assertEqual([t.pairings(r) for r in range(2)], before, fmt)
            self.assertEqual(t.count, 4)

    def test_min_count(self):
        t = Tournament(list("abc"), "rr", 3)   # 3 rounds per cycle
        self.assertEqual(t.min_count(0), 1)
        self.assertEqual(t.min_count(3), 2)
        s = Tournament(list("abcd"), "swiss", 5)
        self.assertEqual(s.min_count(2), 3)

    def test_round_trip_with_larger_count(self):
        t = Tournament(list("abcd"), "swiss", 2)
        for r in range(2):
            play_round(t, r, "1/2-1/2")
        t2 = Tournament.from_dict({**t.to_dict(), "count": 3})
        self.assertEqual(t2.total_rounds, 3)
        self.assertEqual(len(t2.games), 4)
        self.assertEqual(len(t2.pairings(2)[0]), 2)


if __name__ == "__main__":
    unittest.main()
