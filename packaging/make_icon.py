"""Generate chess_player.ico (black knight on a light square) from pieces.png.

Usage: python packaging/make_icon.py chess_player/assets/pieces.png chess_player/assets/chess_player.ico
"""
import sys
from PIL import Image, ImageDraw

src, out = sys.argv[1], sys.argv[2]

sheet = Image.open(src).convert("RGBA")
W, H = sheet.size
cw, ch = W // 6, H // 2
knight = sheet.crop((4 * cw, ch, 5 * cw, 2 * ch))   # row 2 = black, col 5 = knight

# Make the white background transparent
px = knight.load()
for y in range(knight.height):
    for x in range(knight.width):
        r, g, b, a = px[x, y]
        if r > 240 and g > 240 and b > 240:
            px[x, y] = (r, g, b, 0)
knight = knight.crop(knight.getbbox())

S = 256
icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(icon).rounded_rectangle((0, 0, S - 1, S - 1), radius=40, fill="#F0D9B5")
knight.thumbnail((int(S * 0.8), int(S * 0.8)), Image.LANCZOS)
icon.alpha_composite(knight, ((S - knight.width) // 2, (S - knight.height) // 2))
icon.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(f"Wrote {out}")
