"""Render docs/demo.gif from a real pgbrake session (outputs copied from an actual run on PG 17)."""
from PIL import Image, ImageDraw, ImageFont

W, H = 960, 400
FONT = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 17)
BOLD = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 17)
BG, FG, DIM = (24, 26, 33), (220, 223, 228), (120, 126, 140)
AGENT, BRAKE, HELD, OK, YOU = (130, 170, 255), (250, 180, 90), (240, 100, 100), (120, 210, 140), (200, 160, 255)
LH = 26

# (prompt, prompt_color, text, text_color, typed?)
SCRIPT = [
    ("# ", DIM, "your coding agent is 'cleaning up' the users table...", DIM, False),
    ("agent> ", AGENT, "DELETE FROM users WHERE created_at < '2025-01-01'", FG, True),
    ("brake> ", BRAKE, "HELD  DELETE would touch 365 rows (limit 100)  apv_1cd94c25", HELD, False),
    ("agent> ", AGENT, "DROP TABLE users", FG, True),
    ("brake> ", BRAKE, "HELD  schema change (DropStmt)  apv_9060aaab", HELD, False),
    ("agent> ", AGENT, "DELETE FROM users WHERE id = 42", FG, True),
    ("brake> ", BRAKE, "OK    1 row affected; undo with chg_fd645098", OK, False),
    ("", FG, "", FG, False),
    ("you$ ", YOU, "pgbrake undo chg_fd645098", FG, True),
    ("", FG, 'undid delete on "users": 1 row(s) restored', OK, False),
    ("you$ ", YOU, "psql -Atc \"select email from users where id=42\"", FG, True),
    ("", FG, "u42@x.com", FG, False),
]


def frame(lines, cursor=False):
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, W, 34], fill=(36, 39, 48))
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([16 + i * 22, 11, 28 + i * 22, 23], fill=c)
    d.text((W // 2 - 150, 8), "pgbrake: undo for AI agents on Postgres", font=FONT, fill=DIM)
    y = 52
    for p, pc, t, tc in lines:
        d.text((22, y), p, font=BOLD, fill=pc)
        x = 22 + d.textlength(p, font=BOLD)
        d.text((x, y), t, font=FONT, fill=tc)
        last_end = x + d.textlength(t, font=FONT)
        y += LH
    if cursor and lines:
        d.rectangle([last_end + 2, y - LH + 3, last_end + 11, y - 4], fill=FG)
    return im


frames, durs = [], []
done = []
for p, pc, t, tc, typed in SCRIPT:
    if typed:
        step = 6
        for k in range(0, len(t) + 1, step):
            frames.append(frame(done + [(p, pc, t[:k], tc)], cursor=True)); durs.append(45)
        frames.append(frame(done + [(p, pc, t, tc)], cursor=True)); durs.append(400)
    done.append((p, pc, t, tc))
    frames.append(frame(done)); durs.append(700 if t else 200)
durs[-1] = 3500
pal = frames[-1].quantize(colors=48)
frames = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
frames[0].save("docs/demo.gif", save_all=True, append_images=frames[1:], duration=durs, loop=0, optimize=True)
frames[-1].save("docs/demo_last.png")
print(len(frames))
