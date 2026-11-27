"""Board drawing: piece images, squares, move animation."""

import chess
import os
import threading
import time
from typing import Optional
from ..paths import RESOURCE_DIR



class BoardMixin:
    # ── Piece image loading ──

    def _load_pieces(self):
        """Load piece sprites from pieces.png (2 rows × 6 cols: white top, black bottom)."""
        try:
            from PIL import Image, ImageChops, ImageDraw
        except ImportError:
            return None

        png = os.path.join(RESOURCE_DIR, 'pieces.png')
        if not os.path.exists(png):
            return None

        sheet = Image.open(png).convert('RGBA')
        W, H = sheet.size

        # Remove white background: flood-fill from each corner with a magenta marker,
        # then zero-out the alpha wherever that marker ended up.
        rgb = sheet.convert('RGB')
        MAGIC = (255, 0, 255)
        for corner in [(0, 0), (W - 1, 0), (0, H - 1), (W - 1, H - 1)]:
            ImageDraw.floodfill(rgb, corner, MAGIC, thresh=5)

        r, g, b = rgb.split()
        is_bg = ImageChops.multiply(
            ImageChops.multiply(r.point(lambda v: 255 if v == MAGIC[0] else 0),
                                g.point(lambda v: 255 if v == MAGIC[1] else 0)),
            b.point(lambda v: 255 if v == MAGIC[2] else 0))
        transparent = sheet.copy()
        transparent.putalpha(ImageChops.multiply(sheet.getchannel('A'),
                                                 ImageChops.invert(is_bg)))

        cw, ch = W // 6, H // 2
        order = [chess.KING, chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT, chess.PAWN]
        src = {}
        for row, is_white in enumerate([True, False]):
            for col, pt in enumerate(order):
                cell = transparent.crop((col * cw, row * ch, (col + 1) * cw, (row + 1) * ch))
                # Crop to the tight bounding box of non-transparent pixels so that
                # empty cell margins (which vary per piece) don't affect centering.
                bbox = cell.getbbox()
                src[(pt, is_white)] = cell.crop(bbox) if bbox else cell
        return src

    def _pieces_at(self, sq: int):
        """PhotoImages for square size `sq`, scaled from the sprite sheet."""
        if not self._piece_src:
            return None
        if sq in self._piece_cache:
            return self._piece_cache[sq]
        from PIL import Image, ImageTk
        sz = max(8, round(sq * 54 / 68))   # piece size relative to the square
        imgs = {}
        for key, piece in self._piece_src.items():
            p = piece.copy()
            # Scale proportionally to fit within sz×sz, centred on a transparent canvas
            p.thumbnail((sz, sz), Image.LANCZOS)
            canvas = Image.new('RGBA', (sz, sz), (0, 0, 0, 0))
            canvas.paste(p, ((sz - p.width) // 2, (sz - p.height) // 2), p)
            imgs[key] = ImageTk.PhotoImage(canvas)
        # Keep only a few sizes (resizing the window passes through many)
        if len(self._piece_cache) >= 4:
            for k in [k for k in self._piece_cache if k not in (self.SQ, self._sq)][:2]:
                del self._piece_cache[k]
        self._piece_cache[sq] = imgs
        return imgs

    # ── Board drawing ──

    def _draw_board(self, board: chess.Board, last_move: Optional[chess.Move] = None):
        self._cur_board, self._cur_last = board.copy(stack=False), last_move
        c  = self._canvas
        SQ = self._sq
        X0, Y0 = self._bx, self._by
        c.delete("all")
        hi = {last_move.from_square, last_move.to_square} if last_move else set()

        for rank in range(8):
            for file in range(8):
                sq    = chess.square(file, rank)
                x1    = X0 + file * SQ
                y1    = Y0 + (7 - rank) * SQ
                light = (file + rank) % 2 == 1
                col   = self.HI if sq in hi else (self.LIGHT if light else self.DARK)
                c.create_rectangle(x1, y1, x1 + SQ, y1 + SQ, fill=col, outline="")

                piece = board.piece_at(sq)
                if piece:
                    cx, cy = x1 + SQ // 2, y1 + SQ // 2
                    img = self._piece_imgs and self._piece_imgs.get((piece.piece_type, piece.color))
                    if img:
                        c.create_image(cx, cy, image=img, anchor='center')
                    else:
                        self._draw_piece(c, cx, cy, SQ, piece)

        self._draw_coords(c, SQ, X0, Y0)
        self._update_captures(board)

    # Captured-piece glyphs, by the color of the captured piece
    _CAP_ORDER = (chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT, chess.PAWN)
    _CAP_GLYPH = {(chess.QUEEN, True): "♕", (chess.ROOK, True): "♖",
                  (chess.BISHOP, True): "♗", (chess.KNIGHT, True): "♘",
                  (chess.PAWN, True): "♙",
                  (chess.QUEEN, False): "♛", (chess.ROOK, False): "♜",
                  (chess.BISHOP, False): "♝", (chess.KNIGHT, False): "♞",
                  (chess.PAWN, False): "♟"}

    def _update_captures(self, board: chess.Board):
        """Next to each clock: the opponent's pieces this player is up, as the
        net difference per piece type (a traded pair cancels out)."""
        if not hasattr(self, "_wc"):
            return
        w_txt = b_txt = ""
        for pt in self._CAP_ORDER:
            d = len(board.pieces(pt, chess.WHITE)) - len(board.pieces(pt, chess.BLACK))
            if d > 0:
                w_txt += self._CAP_GLYPH[(pt, False)] * d
            elif d < 0:
                b_txt += self._CAP_GLYPH[(pt, True)] * -d
        self._wc.config(text=w_txt)
        self._bc.config(text=b_txt)

    # ── Move animation ──

    def animate_move(self, board: chess.Board, move: chess.Move,
                     stop_event: Optional[threading.Event] = None):
        """Called from the game thread. Blocks (~0.2 s) while the GUI slides the piece."""
        self._anim_done.clear()
        self.update(animate_move=(board.copy(), move))
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline:
            if stop_event and stop_event.is_set():
                return
            if self._anim_done.wait(timeout=0.025):
                return

    def _start_animation(self, board_before: chess.Board, move: chess.Move):
        self._anim_board = board_before
        self._anim_move  = move
        self._anim_start = time.monotonic()
        self._animating  = True
        self._anim_step()

    def _anim_step(self):
        t = min(1.0, (time.monotonic() - self._anim_start) / 0.2)
        ease = t * t * (3 - 2 * t)   # smoothstep
        self._draw_board_anim(self._anim_board, self._anim_move, ease)
        if t < 1.0:
            self.root.after(16, self._anim_step)
        else:
            self._animating = False
            self._anim_done.set()

    def _draw_coords(self, c, SQ: int, X0: int, Y0: int):
        for i in range(8):
            c.create_text(X0 + 3, Y0 + (7 - i) * SQ + SQ // 2, text=str(i + 1),
                          font=self._font(8), fill="#666", anchor="w")
            c.create_text(X0 + i * SQ + SQ // 2, Y0 + 8 * SQ - 2, text="abcdefgh"[i],
                          font=self._font(8), fill="#666", anchor="s")

    def _draw_board_anim(self, board: chess.Board, move: chess.Move, t: float):
        """Board with the moving piece interpolated at progress t (0→from, 1→to)."""
        c  = self._canvas
        SQ = self._sq
        X0, Y0 = self._bx, self._by
        c.delete("all")
        hi     = {move.from_square, move.to_square}
        moving = board.piece_at(move.from_square)

        for rank in range(8):
            for file in range(8):
                sq    = chess.square(file, rank)
                x1    = X0 + file * SQ
                y1    = Y0 + (7 - rank) * SQ
                light = (file + rank) % 2 == 1
                col   = self.HI if sq in hi else (self.LIGHT if light else self.DARK)
                c.create_rectangle(x1, y1, x1 + SQ, y1 + SQ, fill=col, outline="")
                piece = board.piece_at(sq)
                if piece and sq != move.from_square:
                    cx2, cy2 = x1 + SQ // 2, y1 + SQ // 2
                    img = self._piece_imgs and self._piece_imgs.get((piece.piece_type, piece.color))
                    if img:
                        c.create_image(cx2, cy2, image=img, anchor='center')
                    else:
                        self._draw_piece(c, cx2, cy2, SQ, piece)

        self._draw_coords(c, SQ, X0, Y0)

        if moving:
            ff = chess.square_file(move.from_square)
            fr = chess.square_rank(move.from_square)
            tf = chess.square_file(move.to_square)
            tr = chess.square_rank(move.to_square)
            px = X0 + (ff + 0.5) * SQ + t * ((tf - ff) * SQ)
            py = Y0 + (7 - fr + 0.5) * SQ + t * ((fr - tr) * SQ)
            img = self._piece_imgs and self._piece_imgs.get((moving.piece_type, moving.color))
            if img:
                c.create_image(int(px), int(py), image=img, anchor='center')
            else:
                self._draw_piece(c, int(px), int(py), SQ, moving)

    def _draw_piece(self, c, cx, cy, SQ, piece):
        """Draw a chess piece shape using canvas primitives, centred at (cx, cy)."""
        w  = piece.color == chess.WHITE
        fg = "#ededed" if w else "#1a1a1a"
        ol = "#333333" if w else "#888888"
        u  = SQ / 80.0       # 1 drawing-unit in pixels

        def O(x, y, rx, ry, outline=ol):
            c.create_oval(cx+x*u-rx*u, cy+y*u-ry*u,
                          cx+x*u+rx*u, cy+y*u+ry*u,
                          fill=fg, outline=outline, width=1)

        def R(x1, y1, x2, y2, border=True):
            c.create_rectangle(cx+x1*u, cy+y1*u, cx+x2*u, cy+y2*u,
                               fill=fg, outline=ol if border else fg, width=1)

        def P(*pts, smooth=False):
            scaled = [v for x, y in pts for v in (cx+x*u, cy+y*u)]
            c.create_polygon(scaled, fill=fg, outline=ol, width=1, smooth=smooth)

        pt = piece.piece_type

        if pt == chess.PAWN:
            O(0, 13, 12, 5)                  # base
            R(-3, -8, 3, 14, border=False)   # neck gap fill
            O(0,  1,  8, 5)                  # collar
            O(0, -14,  7, 7)                 # head

        elif pt == chess.ROOK:
            O(0, 13, 13, 5)                  # base
            R(-10, -18, 10, 12)              # body
            R(-10, -17, 10, 12, border=False) # seal body-to-base gap
            for bx in (-10, -3, 4):          # three merlons
                R(bx, -27, bx+6, -16)
            R(-10, -17, 10, -16, border=False) # fill gap between merlons and body

        elif pt == chess.KNIGHT:
            O(0, 13, 13, 5)   # base
            # Horse-head silhouette polygon
            P((-8, 13), (8, 13), (10, 4), (10, -3), (5, -11),
              (13, -19), (8, -28), (2, -26), (-1, -30),
              (-6, -23), (-2, -17), (-10, -6))
            O(0, 13, 13, 4, outline=fg)      # hide join at base

        elif pt == chess.BISHOP:
            O(0, 13, 13, 5)                  # base
            R(-3,  -8,  3, 14, border=False) # stem fill
            O(0,   1,   7,  9)               # body bulge
            P((-5, -3), (5, -3), (2, -22), (-2, -22))  # pointed head
            O(0, -23,   4,  4)               # top ball

        elif pt == chess.QUEEN:
            O(0, 13, 14, 5)                  # base
            R(-8, -10, 8, 14, border=False)  # body fill
            O(0,  0,   9, 11)                # body
            P((-9, -10), (9, -10), (7, -22), (-7, -22))  # crown band
            for bx in (-8, -4, 0, 4, 8):    # five crown balls
                O(bx, -24, 3, 3)

        elif pt == chess.KING:
            O(0, 13, 14, 5)                  # base
            R(-8, -10, 8, 14, border=False)  # body fill
            O(0,  0,   9, 11)                # body
            P((-9, -10), (9, -10), (7, -20), (-7, -20))  # crown band
            R(-2, -30,  2, -18)              # cross vertical
            R(-7, -27,  7, -23)              # cross horizontal
