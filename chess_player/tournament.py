"""Tournament logic (pairings, standings) and tournament state files."""

import json
import os
from typing import Optional, Tuple
from .paths import DATA_DIR


# ──────────────────────────────────────────────────────────────────────────────
# Tournament state files — the running tournament is autosaved after every
# game and resumed on the next start; a stopped one can be saved to a file
# ──────────────────────────────────────────────────────────────────────────────

CURRENT_TOURNAMENT_FILE = os.path.join(DATA_DIR, ".current_tournament.json")
# The last finished tournament, kept so that it can be extended later
FINISHED_TOURNAMENT_FILE = os.path.join(DATA_DIR, ".finished_tournament.json")
TOURNAMENTS_DIR          = os.path.join(DATA_DIR, "tournaments")
TOURNAMENT_STATE_VERSION = 1

def check_tournament_state(d) -> dict:
    """Validate a tournament state dict (see run_tournament). Raises ValueError."""
    if not isinstance(d, dict) or d.get("kind") != "chess_player tournament":
        raise ValueError("not a chess_player tournament file")
    if d.get("version") != TOURNAMENT_STATE_VERSION:
        raise ValueError(f"unsupported tournament file version: {d.get('version')!r}")
    try:
        engines = [{"name": str(e["name"]), "path": str(e["path"])} for e in d["engines"]]
        t, inc = float(d["time"]), float(d["inc"])
        tour = Tournament.from_dict(d["tournament"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"invalid tournament file ({e})") from None
    if t <= 0 or inc < 0:
        raise ValueError("invalid time control in tournament file")
    if [e["name"] for e in engines] != tour.names:
        raise ValueError("engine list does not match the tournament")
    return {**d, "engines": engines, "time": t, "inc": inc}

def load_tournament_file(path: str, check_engines: bool = True) -> dict:
    """Load and validate a saved tournament. Raises ValueError."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"could not read {os.path.basename(path)}: {e}") from None
    d = check_tournament_state(d)
    if check_engines:
        for e in d["engines"]:
            if not os.path.isfile(e["path"]):
                raise ValueError(f"engine not found: {e['path']}")
    return d

def remove_current_tournament():
    try:
        os.remove(CURRENT_TOURNAMENT_FILE)
    except OSError:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Tournament logic (pairings, standings) — no engine or GUI code here
# ──────────────────────────────────────────────────────────────────────────────

_SHORT_REASON = {
    "checkmate": "mate", "stalemate": "stalemate",
    "insufficient material": "material", "threefold repetition": "3-fold",
    "fivefold repetition": "5-fold", "fifty moves": "50-move",
    "seventyfive moves": "75-move", "time forfeit": "time",
    "no move": "no move", "illegal move": "illegal", "malformed move": "malformed",
}

def _game_points(result: str) -> Tuple[float, float]:
    """(white points, black points) for a PGN result string."""
    return {"1-0": (1.0, 0.0), "0-1": (0.0, 1.0), "1/2-1/2": (0.5, 0.5)}.get(result, (0.0, 0.0))


class Tournament:
    """
    Round robin ("rr"):  `count` = number of rounds, i.e. full cycles in which
                         every engine meets every other engine once. Colors
                         are reversed in every second cycle.
    Swiss ("swiss"):     `count` = number of matches per engine (Swiss rounds).
                         Each round pairs engines with equal or similar scores
                         who have not met yet, balancing colors. With an odd
                         number of engines one gets a bye worth 1 point.

    Tie-break: Sonneborn-Berger (SB) = sum of the final scores of the opponents
    this engine beat, plus half the scores of the opponents it drew with.
    """

    def __init__(self, names: list, fmt: str, count: int):
        if len(names) < 2:
            raise ValueError("a tournament needs at least 2 engines")
        if fmt not in ("rr", "swiss"):
            raise ValueError(f"unknown tournament format: {fmt!r}")
        self.names = list(names)
        self.n     = len(names)
        self.fmt   = fmt
        self.count = max(1, int(count))
        self.events: list = []   # chronological: {"type": "game"|"bye", ...}
        self.current: Optional[Tuple[int, int, int]] = None   # (round, white, black)
        # Swiss pairings per round, fixed once made (they depend on results,
        # so a resumed tournament must not recompute a half-played round)
        self._pairings: dict = {}
        if fmt == "rr":
            self._rr = self._round_robin_schedule()
            self.total_rounds = len(self._rr)
            self.total_games  = sum(len(pairs) for pairs, _ in self._rr)
        else:
            self.total_rounds = self.count
            self.total_games  = self.count * (self.n // 2)

    def set_count(self, count: int):
        """Change the number of rounds (rr) / matches per engine (swiss) of a
        running tournament. Earlier rounds keep their pairings."""
        self.count = max(1, int(count))
        if self.fmt == "rr":
            rr = self._round_robin_schedule()
            self.total_games  = sum(len(pairs) for pairs, _ in rr)
            self._rr = rr
            self.total_rounds = len(rr)
        else:
            self.total_games  = self.count * (self.n // 2)
            self.total_rounds = self.count
            for r in [r for r in self._pairings if r >= self.count]:
                del self._pairings[r]

    def min_count(self, started_round: int) -> int:
        """Smallest count that keeps round `started_round` (0-based) and the
        rounds before it."""
        if self.fmt == "rr":
            per_cycle = len(self._rr) // self.count
            return started_round // per_cycle + 1
        return started_round + 1

    # ── Results ──

    @property
    def games(self) -> list:
        return [e for e in self.events if e["type"] == "game"]

    def record(self, rnd: int, white: int, black: int, result: str, reason: str = ""):
        self.events.append({"type": "game", "round": rnd, "white": white,
                            "black": black, "result": result, "reason": reason})

    def record_bye(self, rnd: int, player: int):
        self.events.append({"type": "bye", "round": rnd, "player": player})

    def has_game(self, rnd: int, white: int, black: int) -> bool:
        return any(e["type"] == "game" and (e["round"], e["white"], e["black"]) == (rnd, white, black)
                   for e in self.events)

    def has_bye(self, rnd: int) -> bool:
        return any(e["type"] == "bye" and e["round"] == rnd for e in self.events)

    # ── Persistence ──

    def to_dict(self) -> dict:
        return {"names": list(self.names), "format": self.fmt, "count": self.count,
                "events": [dict(e) for e in self.events],
                "pairings": {str(r): {"pairs": [list(p) for p in pairs], "bye": bye}
                             for r, (pairs, bye) in self._pairings.items()}}

    @classmethod
    def from_dict(cls, d: dict) -> "Tournament":
        """Inverse of to_dict. Raises ValueError/KeyError/TypeError on bad data."""
        t = cls([str(n) for n in d["names"]], d["format"], int(d["count"]))
        idx = range(t.n)
        for e in d.get("events", []):
            rnd = int(e["round"])
            if not 0 <= rnd < t.total_rounds:
                raise ValueError(f"round out of range: {rnd}")
            if e["type"] == "bye":
                if int(e["player"]) not in idx:
                    raise ValueError("bad bye player")
                t.record_bye(rnd, int(e["player"]))
            elif e["type"] == "game":
                w, b = int(e["white"]), int(e["black"])
                if w not in idx or b not in idx or w == b:
                    raise ValueError("bad game players")
                if e["result"] not in ("1-0", "0-1", "1/2-1/2"):
                    raise ValueError(f"bad result: {e['result']!r}")
                t.record(rnd, w, b, e["result"], str(e.get("reason", "")))
            else:
                raise ValueError(f"unknown event type: {e['type']!r}")
        for r, p in (d.get("pairings") or {}).items():
            pairs = [(int(a), int(b)) for a, b in p["pairs"]]
            bye = None if p.get("bye") is None else int(p["bye"])
            t._pairings[int(r)] = (pairs, bye)
        return t

    def score(self, i: int) -> float:
        pts = 0.0
        for e in self.events:
            if e["type"] == "bye":
                if e["player"] == i:
                    pts += 1.0
            elif i in (e["white"], e["black"]):
                wp, bp = _game_points(e["result"])
                pts += wp if e["white"] == i else bp
        return pts

    def sb(self, i: int) -> float:
        """Sonneborn-Berger tie-break score."""
        total = 0.0
        for g in self.games:
            if i not in (g["white"], g["black"]):
                continue
            wp, bp = _game_points(g["result"])
            mine, opp = (wp, g["black"]) if g["white"] == i else (bp, g["white"])
            total += mine * self.score(opp)
        return total

    def standings(self) -> list:
        rows = []
        for i, name in enumerate(self.names):
            w = d = l = 0
            for g in self.games:
                if i not in (g["white"], g["black"]):
                    continue
                wp, bp = _game_points(g["result"])
                mine = wp if g["white"] == i else bp
                if mine == 1.0:
                    w += 1
                elif mine == 0.5:
                    d += 1
                else:
                    l += 1
            rows.append({"idx": i, "name": name, "games": w + d + l,
                         "wins": w, "draws": d, "losses": l,
                         "points": self.score(i), "sb": self.sb(i)})
        rows.sort(key=lambda r: (-r["points"], -r["sb"], -r["wins"], r["idx"]))
        # Shared rank when points and tie-break are both equal
        for k, r in enumerate(rows):
            prev = rows[k - 1] if k else None
            same = prev and (prev["points"], prev["sb"]) == (r["points"], r["sb"])
            r["rank"] = prev["rank"] if same else k + 1
        return rows

    # ── Pairings ──

    def pairings(self, rnd: int) -> Tuple[list, Optional[int]]:
        """([(white, black), ...], bye player or None) for round `rnd` (0-based).
        Swiss pairings depend on earlier results: call once the previous round
        is complete."""
        if self.fmt == "rr":
            pairs, bye = self._rr[rnd]
            return list(pairs), bye
        if rnd not in self._pairings:
            self._pairings[rnd] = self._swiss_pairings(rnd)
        pairs, bye = self._pairings[rnd]
        return list(pairs), bye

    def _round_robin_schedule(self) -> list:
        """Circle method. Returns [(pairs, sitting_out_player)] per round."""
        arr = list(range(self.n)) + ([None] if self.n % 2 else [])
        m = len(arr)
        cycle = []
        balance = [0] * self.n   # whites minus blacks so far in the cycle
        for k in range(m - 1):
            pairs, bye = [], None
            for i in range(m // 2):
                a, b = arr[i], arr[m - 1 - i]
                if a is None or b is None:
                    bye = b if a is None else a
                    continue
                # White to whoever has had it less; alternate on a tie
                if balance[a] != balance[b]:
                    w, bl = (a, b) if balance[a] < balance[b] else (b, a)
                else:
                    w, bl = (a, b) if (k + i) % 2 == 0 else (b, a)
                balance[w] += 1
                balance[bl] -= 1
                pairs.append((w, bl))
            cycle.append((pairs, bye))
            arr = [arr[0], arr[-1]] + arr[1:-1]
        rounds = []
        for c in range(self.count):
            for pairs, bye in cycle:
                rounds.append(([(b, a) for a, b in pairs] if c % 2 else list(pairs), bye))
        return rounds

    def _colors(self, i: int) -> list:
        """Color history of player i: +1 = White, -1 = Black."""
        out = []
        for g in self.games:
            if g["white"] == i:
                out.append(1)
            elif g["black"] == i:
                out.append(-1)
        return out

    def _meetings(self) -> dict:
        met: dict = {}
        for g in self.games:
            key = (min(g["white"], g["black"]), max(g["white"], g["black"]))
            met[key] = met.get(key, 0) + 1
        return met

    def _swiss_pairings(self, rnd: int) -> Tuple[list, Optional[int]]:
        order = sorted(range(self.n), key=lambda i: (-self.score(i), -self.sb(i), i))
        bye = None
        if self.n % 2:
            had_bye = {e["player"] for e in self.events if e["type"] == "bye"}
            bottom_up = [p for p in reversed(order) if p not in had_bye] or list(reversed(order))
            bye = bottom_up[0]
            order.remove(bye)

        if rnd == 0:
            # Top half vs bottom half, alternating colors by board
            h = len(order) // 2
            pairs = [(order[i], order[i + h]) if i % 2 == 0 else (order[i + h], order[i])
                     for i in range(h)]
            return pairs, bye

        met = self._meetings()
        # Avoid rematches; relax only when no pairing exists (more rounds
        # than opponents), allowing at most `limit` earlier meetings per pair.
        result = None
        for limit in range(0, self.total_rounds + 1):
            self._budget = 20000
            result = self._pair_rec(order, met, limit)
            if result is not None:
                break
        if result is None:   # unreachable in practice; fall back to adjacent pairs
            result = [(order[i], order[i + 1]) for i in range(0, len(order) - 1, 2)]
        return [self._assign_colors(a, b, rnd) for a, b in result], bye

    def _pair_rec(self, order: list, met: dict, limit: int) -> Optional[list]:
        if not order:
            return []
        p, rest = order[0], order[1:]
        for j, q in enumerate(rest):
            if met.get((min(p, q), max(p, q)), 0) > limit:
                continue
            self._budget -= 1
            if self._budget < 0:
                return None
            sub = self._pair_rec(rest[:j] + rest[j + 1:], met, limit)
            if sub is not None:
                return [(p, q)] + sub
        return None

    def _assign_colors(self, a: int, b: int, rnd: int) -> Tuple[int, int]:
        """a is the higher-ranked player. Returns (white, black)."""
        ca, cb = self._colors(a), self._colors(b)
        if sum(ca) != sum(cb):
            return (a, b) if sum(ca) < sum(cb) else (b, a)
        la, lb = (ca[-1] if ca else 0), (cb[-1] if cb else 0)
        if la != lb:
            return (a, b) if la < lb else (b, a)
        return (a, b) if rnd % 2 == 0 else (b, a)

    # ── Display ──

    def snapshot(self, status: str, tc_text: str) -> dict:
        """Plain-data view for the GUI. status: starting|running|finished|stopped."""
        done = len(self.games)
        rnd = (self.current[0] if self.current else
               max((e["round"] for e in self.events), default=-1))
        fmt_name = "Round robin" if self.fmt == "rr" else "Swiss"
        results = []
        for e in reversed(self.events):
            if e["type"] == "bye":
                results.append({"type": "bye", "round": e["round"] + 1,
                                "name": self.names[e["player"]]})
            else:
                results.append({"type": "game", "round": e["round"] + 1,
                                "white": self.names[e["white"]],
                                "black": self.names[e["black"]],
                                "result": e["result"],
                                "reason": _SHORT_REASON.get(e["reason"], e["reason"])})
        current = None
        if self.current and status == "running":
            current = (self.names[self.current[1]], self.names[self.current[2]])
        return {
            "title":    f"{fmt_name} · {self.n} engines",
            "progress": (f"Round {max(rnd, 0) + 1}/{self.total_rounds} · "
                         f"game {min(done + (1 if current else 0), self.total_games)}"
                         f"/{self.total_games}"),
            "tc":       tc_text,
            "status":   status,
            "current":  current,
            "rows":     self.standings(),
            "results":  results,
        }
