"""
Renders a Krillion day's shared answers as a PNG table for the daily post.

One row per prompt, one column per player (ordered by the day's total, which
heads the column), each cell holding that player's answer. The look follows
krillion.io's own: the deep-sea "abyss" background and "shallow" panels, foam
and drift text, and the VT323 pixel font the whole site is set in (bundled in
``assets/fonts`` under the SIL Open Font License).

Each tier uses the colour krillion.io gives that tier's text: 🌟 gold,
🏮 deep-cut purple, 🦑 rare red, 🐟 schooler cyan, 🤡 too-clever orange and
🫧 plankton slate. They stay distinct under colour blindness; the closest
pair, 🤡 orange and 🌟 gold, is told apart by the glow a 🌟 cell gets, like
the site's top-tier marker. A ⬛ miss (no answer, 0 points) is a hatched
empty panel. Cells hold only the answer;
the legend under the table pairs each colour with its emoji and points. The
emojis are Twemoji images (``assets/emoji``, CC-BY 4.0), so they look the same
whatever fonts the host has.
"""

import io
import pathlib

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

# Rendered at 2x so text stays crisp when Discord scales the image down.
SCALE = 2
ASSETS = pathlib.Path(__file__).resolve().parents[1] / "assets"
FONT_PATH = ASSETS / "fonts" / "VT323-Regular.ttf"

# krillion.io's palette.
ABYSS = "#050a14"
ABYSS_TOP = "#0b1526"
SHALLOW = "#101d33"
LINE_COLOUR = "#1b2c47"
FOAM = "#e8f1ff"
DRIFT = "#7d93b8"

# Points -> (tier emoji's Twemoji codepoint, colour). The colours are the
# site's own tier text colours (its --tier-* CSS variables).
TIERS = {
    100: ("1f31f", "#ffd166"),  # 🌟 One in a Krillion
    85: ("1f3ee", "#9d7bff"),  # 🏮 Deep Cut
    60: ("1f991", "#ff5252"),  # 🦑 Rare
    30: ("1f41f", "#4de3ff"),  # 🐟 Schooler
    15: ("1f921", "#ff9f43"),  # 🤡 Too Clever
    10: ("1fae7", "#8fa3c4"),  # 🫧 Plankton
}
GLOWING = 100
# A miss (0 points): ⬛, drawn as a hatched empty panel.
MISS_EMOJI = "2b1b"
HATCH = "#2b3e5e"
MISS_TEXT = "#a9b8d1"
HATCH_SPACING = 9
# Gold tinted into the navy panel turns olive, so gold cells sit on a warm
# dark base instead.
GOLD_BASE = "#17130a"
# How strongly a tier's colour tints its cell.
TINT = 0.16

PAD = 20
GAP = 6  # gap between cells
PROMPT_WIDTH = 250
COLUMN_WIDTH = 164
LINE = 20  # body line height
MAX_LINES = 3
CELL_PAD = 9
EMOJI = 20  # legend emoji size


def _font(size: int):
    """The site's VT323 pixel font, falling back to Pillow's default."""
    try:
        return ImageFont.truetype(str(FONT_PATH), size * SCALE)
    except OSError:
        return ImageFont.load_default(size * SCALE)


def _emoji(codepoint: str, size: int) -> Image.Image | None:
    """A bundled Twemoji image, resized, or None if it's missing."""
    try:
        with Image.open(ASSETS / "emoji" / f"{codepoint}.png") as img:
            return img.convert("RGBA").resize((size * SCALE, size * SCALE), Image.LANCZOS)
    except OSError:
        return None


def _hatch(image: Image.Image, box: list[int], radius: int) -> None:
    """Fill a rounded box with diagonal lines over the empty-panel colour."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    panel = Image.new("RGB", (w, h), SHALLOW)
    lines = ImageDraw.Draw(panel)
    step = HATCH_SPACING * SCALE
    for offset in range(-h, w, step):
        lines.line([(offset, h), (offset + h, 0)], fill=HATCH, width=SCALE)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    image.paste(panel, (x0, y0), mask)
    ImageDraw.Draw(image).rounded_rectangle(
        box, radius=radius, outline=LINE_COLOUR, width=SCALE
    )


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    return tuple(int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))


def _blend(colour: str, base: str, amount: float) -> tuple[int, int, int]:
    """``colour`` laid over ``base`` at ``amount`` opacity."""
    c, b = _rgb(colour), _rgb(base)
    return tuple(round(b[i] + (c[i] - b[i]) * amount) for i in range(3))


def _wrap(draw, text: str, font, width: int, max_lines: int = MAX_LINES) -> list[str]:
    """Word-wrap ``text`` to a pixel width, ellipsizing past ``max_lines``."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] += "…"
    # A single word longer than the width gets cut down too.
    for i, line in enumerate(lines):
        while draw.textlength(line, font=font) > width and len(line) > 1:
            line = line[:-2] + "…"
        lines[i] = line
    return lines or [""]


def render_day_table(
    day: int, played_on: str, prompts: list[str], players: list[dict]
) -> bytes:
    """
    Draw the table and return it as PNG bytes.

    ``players`` are already in column order, each ``{"name", "score",
    "rounds"}`` where ``rounds`` lists one ``{"answer", "score"}`` (or None)
    per prompt.
    """
    s = SCALE
    head_font = _font(26)
    body_font = _font(21)
    small_font = _font(16)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    inner = (COLUMN_WIDTH - 2 * CELL_PAD) * s
    prompt_inner = (PROMPT_WIDTH - 14) * s

    # Lay out every row first, so each row is as tall as its tallest cell.
    rows = []
    for slot, prompt in enumerate(prompts):
        prompt_lines = _wrap(probe, prompt, body_font, prompt_inner)
        cells = []
        for player in players:
            played = player["rounds"][slot] if slot < len(player["rounds"]) else None
            answer = (played or {}).get("answer")
            points = (played or {}).get("score") or 0
            lines = _wrap(probe, answer, body_font, inner) if answer else []
            cells.append((lines, points))
        # The prompt sits under its round number.
        tallest = max([len(prompt_lines) + 1] + [len(c[0]) for c in cells])
        rows.append((slot, prompt_lines, cells, tallest * LINE + 2 * CELL_PAD))

    header_height = 2 * LINE + 2 * CELL_PAD + 4
    width = PAD * 2 + PROMPT_WIDTH + len(players) * (COLUMN_WIDTH + GAP) - GAP

    # Legend: colour chip, emoji, points — wrapping when the table is narrow.
    legend = []  # (x, line, points, label)
    x, line = PAD, 0
    for points in [*TIERS, 0]:
        label = str(points)
        chip = 22 + EMOJI + 6 + int(probe.textlength(label, font=body_font) / s)
        if x > PAD and x + chip > width - PAD:
            x, line = PAD, line + 1
        legend.append((x, line, points, label))
        x += chip + 18
    legend_height = 30 + (line + 1) * 28
    height = (
        PAD + header_height + sum(r[3] + GAP for r in rows) + legend_height + PAD
    )

    # Background: the site's abyss, a touch lighter near the surface.
    image = Image.new("RGB", (width * s, height * s), ABYSS)
    draw = ImageDraw.Draw(image)
    top, bottom = _rgb(ABYSS_TOP), _rgb(ABYSS)
    for y in range(height * s):
        t = min(1.0, y / (height * s * 0.6))
        draw.line(
            [(0, y), (width * s, y)],
            fill=tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
        )

    def col_x(i: int) -> int:
        return PAD + PROMPT_WIDTH + i * (COLUMN_WIDTH + GAP)

    # Header row: "KRILLION #126" over the date, level with each player's
    # name over their total (gold for the day's best).
    y = PAD
    draw.text((PAD * s, (y + 2) * s), f"KRILLION #{day}", font=head_font, fill=FOAM)
    draw.text((PAD * s, (y + 2 + LINE + 6) * s), played_on, font=body_font, fill=DRIFT)
    best = max((p["score"] or 0) for p in players) if players else 0
    for i, player in enumerate(players):
        x = col_x(i) + CELL_PAD
        name = _wrap(probe, player["name"], head_font, inner, max_lines=1)[0]
        draw.text((x * s, (y + 2) * s), name, font=head_font, fill=FOAM)
        score = player["score"] or 0
        draw.text(
            (x * s, (y + 2 + LINE + 6) * s),
            f"{score} PTS",
            font=body_font,
            fill=TIERS[GLOWING][1] if score == best and best else DRIFT,
        )
    y += header_height
    draw.line(
        [(PAD * s, (y - 6) * s), ((width - PAD) * s, (y - 6) * s)], fill=LINE_COLOUR, width=s
    )

    # Glows go on their own layer, blurred, under the cells.
    glow = Image.new("RGB", image.size, (0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    boxes = []

    # Body.
    for slot, prompt_lines, cells, row_height in rows:
        draw.text(
            (PAD * s, (y + CELL_PAD) * s), f"ROUND {slot + 1}", font=small_font, fill=DRIFT
        )
        for n, line in enumerate(prompt_lines):
            draw.text(
                (PAD * s, (y + CELL_PAD + (n + 1) * LINE) * s),
                line,
                font=body_font,
                fill=FOAM,
            )
        for i, (lines, points) in enumerate(cells):
            x0, y0 = col_x(i), y
            box = [x0 * s, y0 * s, (x0 + COLUMN_WIDTH) * s, (y0 + row_height) * s]
            boxes.append((box, lines, points))
            if lines and points == GLOWING:
                glow_draw.rounded_rectangle(box, radius=6 * s, fill=_rgb(TIERS[GLOWING][1]))
        y += row_height + GAP

    glow = glow.filter(ImageFilter.GaussianBlur(8 * s))
    image = ImageChops.screen(image, ImageEnhance.Brightness(glow).enhance(0.7))
    draw = ImageDraw.Draw(image)

    for box, lines, points in boxes:
        x0, y0 = box[0] // s, box[1] // s
        # Answers sit vertically centred in their cell.
        rows_free = (box[3] - box[1]) // s - 2 * CELL_PAD - max(len(lines), 1) * LINE
        text_y = y0 + CELL_PAD + rows_free // 2
        if not points:
            # A miss: a hatched panel, with whatever they typed (if anything)
            # in muted text, since it didn't match an answer.
            _hatch(image, box, 6 * s)
            for n, line in enumerate(lines):
                draw.text(
                    ((x0 + CELL_PAD) * s, (text_y + n * LINE) * s),
                    line,
                    font=body_font,
                    fill=MISS_TEXT,
                )
            continue
        _, colour = TIERS.get(points, ("", DRIFT))
        draw.rounded_rectangle(
            box,
            radius=6 * s,
            fill=_blend(colour, GOLD_BASE if points == GLOWING else SHALLOW, TINT),
            outline=_rgb(colour),
            width=s,
        )
        for n, line in enumerate(lines):
            draw.text(
                ((x0 + CELL_PAD) * s, (text_y + n * LINE) * s),
                line,
                font=body_font,
                fill=FOAM,
            )

    # Legend: one chip per tier, best first.
    y += 18
    for x, line, points, label in legend:
        top = y + line * 28
        chip = [x * s, top * s, (x + 16) * s, (top + 16) * s]
        if points == 0:
            _hatch(image, chip, 3 * s)
            codepoint = MISS_EMOJI
        else:
            codepoint, colour = TIERS[points]
            draw.rounded_rectangle(
                chip,
                radius=3 * s,
                fill=_blend(colour, GOLD_BASE if points == GLOWING else SHALLOW, 0.35),
                outline=_rgb(colour),
                width=s,
            )
        emoji = _emoji(codepoint, EMOJI)
        if emoji is not None:
            image.paste(emoji, ((x + 22) * s, (top - 2) * s), emoji)
        draw.text(((x + 22 + EMOJI + 6) * s, (top - 4) * s), label, font=body_font, fill=DRIFT)

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
