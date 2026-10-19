"""Live HUD skin measured from the user's approved 1254-pixel artwork.

Coordinates below are source-art pixels, never unrelated widget units. Static
art is sampled from the supplied PNG; labels and bars receive live values.
This module has no capture, identity, network, persistence or battle logic.
"""
from __future__ import annotations

import math
import json
import colorsys
import os
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageStat


REFERENCE_BOUNDS = (146, 126, 1130, 1130)
REFERENCE_WIDTH = REFERENCE_BOUNDS[2] - REFERENCE_BOUNDS[0]
HUD_LOGICAL_WIDTH = 306
REFERENCE_SCALE = HUD_LOGICAL_WIDTH / REFERENCE_WIDTH
DEATH_COLUMN_WIDTH = 138
HUD_LOGICAL_WIDTH_WITHOUT_DEATHS = round((REFERENCE_WIDTH - DEATH_COLUMN_WIDTH) * REFERENCE_SCALE)
HUD_DEFAULT_VISIBLE_ROWS = 12
# Keep the established 12-row viewport as the default, but let the lower-right
# grip add roughly one quarter of the current window height. The data list is
# not capped by this value; overflow remains available through the scrollbar.
HUD_MAX_VISIBLE_ROWS = 16
HUD_ROW_HEIGHT = 75.4 * REFERENCE_SCALE
BOSS_BAR_EXTRA_HEIGHT = 8
HUD_BASE_SOURCE_HEIGHT = 1004
SUMMARY_FONT_HEIGHT = 40
TEAM_SUMMARY_AVATAR_SIZE = (109, 107)
INLINE_RATING_GAP = 12
# Keep the view switch clear of the timer at every supported
# font scale.  The previous 300px source-art anchor left only a few rendered
# pixels between ``00:00`` and the first pill.
TITLE_RAIL_TAB_LEFT = 326
TITLE_RAIL_TAB_WIDTH = 170
TITLE_RAIL_TAB_GAP = 12
ADMIN_INDICATOR_ELEVATED = (58, 211, 116)
ADMIN_INDICATOR_STANDARD = (239, 76, 91)
ROW_CENTERS = (319, 395, 470, 546, 621, 697, 772, 848, 923, 997)
ACTION_BOXES = {
    "settings": (839, 1038, 922, 1126),
    "lock": (922, 1038, 1004, 1126),
    "pin": (1004, 1038, 1089, 1126),
}
# Keep the update action immediately left of the settings artwork.
FOOTER_ACTION_BOXES = {
    "update": (756, 1038, 839, 1126),
    **ACTION_BOXES,
}


@dataclass(frozen=True)
class HudRenderResult:
    image: Image.Image
    hit_regions: dict[str, tuple[int, int, int, int]]
    actor_regions: tuple[tuple[tuple[int, int, int, int], int], ...]
    row_height: int
    visible_rows: int


def clamp_visible_rows(value: object) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        count = HUD_DEFAULT_VISIBLE_ROWS
    return min(HUD_MAX_VISIBLE_ROWS, max(1, count))


def equipment_type_badge(pvp_count: object) -> tuple[str, bool]:
    """Return the compact equipment label and whether it is a PVP warning."""

    try:
        count = max(0, int(pvp_count or 0))
    except (TypeError, ValueError, OverflowError):
        count = 0
    if count:
        return f"竞技装备 ×{count}", True
    return "冒险装备", False


def _color(hex_value: str):
    value = hex_value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def boss_placeholder_anchors(name_right, bar_right, hp_width, percent_width):
    """Left anchors centering both unavailable fields after the target name."""
    gap = 18
    group_width = hp_width + gap + percent_width
    left = name_right + (bar_right - name_right - group_width) / 2
    return left, left + hp_width + gap


def rating_field_layout(caption_width, value_width=169):
    """A compact label/value group; all values share one centered field."""
    padding, ink_gap = 13, 8
    overlap = padding * 2 - ink_gap
    group_width = caption_width + value_width - overlap
    caption_left = 552 + (385 - group_width) / 2
    value_center = caption_left + caption_width - overlap + value_width / 2
    return caption_left, value_center


def text_top_for_center(tile, center_y):
    """Center visible ink, not transparent font ascent/descent padding."""
    bounds = tile.info.get('hud_ink_bounds', (0, 0, tile.width, tile.height))
    return center_y - (bounds[1] + bounds[3]) / 2


def attached_total_left(stat_right, stat_padding=13, total_padding=7):
    # Two source pixels (~0.6 DIP) keep the outlines from colliding, without
    # an empty inter-column gap between DPS and its parenthesized total.
    return stat_right - stat_padding - total_padding + 2


def _dilate(mask, radius):
    """Padded-mask dilation using C-level image ops, not an O(r²) rank filter."""
    result = mask
    for horizontal in (True, False):
        remaining, step = int(radius), 1
        while remaining > 0:
            delta = min(step, remaining)
            dx, dy = (delta, 0) if horizontal else (0, delta)
            result = ImageChops.lighter(result, ImageChops.lighter(ImageChops.offset(result, dx, dy), ImageChops.offset(result, -dx, -dy)))
            remaining -= delta
            step *= 2
    return result


class MainHudRenderer:
    """A source-art sprite skin, including the source's numeric glyph shapes."""

    def __init__(self, asset_dir: Path, profession_colors: Mapping[int, str], *, supersample=2):
        self.asset_dir = Path(asset_dir)
        self.profession_colors = dict(profession_colors)
        self.reference = Image.open(self.asset_dir / "main_hud_reference.png").convert("RGBA")
        if self.reference.size != (1254, 1254):
            raise ValueError("The approved HUD atlas must remain 1254 × 1254")
        # The PNG contains almost-invisible export noise outside its artwork.
        # Removing only alpha<8 avoids carrying those isolated dots into DWM.
        self.reference.putalpha(self.reference.getchannel("A").point(lambda n: 0 if n < 8 else n))
        fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        self.font_paths = {
            "name": fonts / "msyhbd.ttc",
            "small": fonts / "msyh.ttc",
        }
        self._fonts = {}
        self._labels: OrderedDict[tuple, Image.Image] = OrderedDict()
        self._professions = {}
        self._skins = {}
        self._glyphs = self._source_digits()
        self.source_font_characters = frozenset(self._glyphs)
        self._boss = self._crop((154, 182, 258, 287))
        # Trim neighboring bar pixels at the edges, not the crest itself.
        emblem_mask = Image.new("L", self._boss.size)
        ImageDraw.Draw(emblem_mask).polygon(
            [(48, 0), (65, 16), (87, 12), (84, 29), (103, 51),
             (84, 66), (89, 87), (67, 85), (54, 104), (37, 85),
             (15, 88), (17, 65), (0, 51), (17, 29), (12, 15), (33, 16)], fill=255)
        self._boss.putalpha(ImageChops.multiply(self._boss.getchannel("A"), emblem_mask))
        # The bar was made taller; preserve the source emblem while scaling
        # its overlap as well, so the left cap cannot protrude around it.
        self._boss = self._boss.resize((128, 128), Image.Resampling.LANCZOS)
        self._boss_portraits = {}
        try:
            portrait_manifest = json.loads(
                (self.asset_dir / 'bosses/hud/manifest.json').read_text(encoding='utf-8-sig')
            )
            self._boss_portrait_templates = portrait_manifest.get('templates', {})
            self._boss_portrait_filenames = frozenset(
                portrait_manifest.get('portraits', {})
            )
        except (OSError, ValueError, AttributeError):
            self._boss_portrait_templates = {}
            self._boss_portrait_filenames = frozenset()
        # The user explicitly retains the project's current logo, not the
        # example portrait in the design. Its bounds still follow the design.
        self.logo_path = self.asset_dir / "app_logo.png"
        avatar = Image.open(self.logo_path).convert("RGBA")
        avatar.putalpha(avatar.getchannel("A").point(lambda n: 0 if n < 8 else n))
        avatar_bounds = avatar.getchannel("A").getbbox()
        if avatar_bounds:
            avatar = avatar.crop(avatar_bounds)
        # Add the requested gold frame only to the app logo; professions
        # remain shape-only coloured symbols without circular substrates.
        avatar.thumbnail((93 * 3, 93 * 3), Image.Resampling.LANCZOS)
        self._avatar = Image.new("RGBA", (103 * 3, 101 * 3))
        logo_mask = Image.new('L', avatar.size)
        ImageDraw.Draw(logo_mask).ellipse((0, 0, avatar.width - 1, avatar.height - 1), fill=255)
        avatar.putalpha(ImageChops.multiply(avatar.getchannel('A'), logo_mask))
        self._avatar.alpha_composite(avatar, ((309 - avatar.width) // 2, (303 - avatar.height) // 2))
        rim = ImageDraw.Draw(self._avatar)
        rim.ellipse((5, 2, 303, 300), outline=(124, 57, 7, 255), width=11)
        rim.ellipse((9, 6, 299, 296), outline=(255, 182, 27, 255), width=6)
        rim.ellipse((16, 13, 292, 289), outline=(255, 241, 148, 255), width=3)
        rim.arc((9, 6, 299, 296), 205, 300, fill=(255, 252, 204, 255), width=5)
        self._avatar = self._avatar.resize((103, 101), Image.Resampling.LANCZOS)
        self._skull = self._crop((950, 287, 1016, 354))
        self._self_arrow = self._crop((146, 588, 197, 651))
        self._team_caption = self._crop((279, 1055, 426, 1110))
        self._warning = self._crop((798, 151, 835, 184))
        self._actions = {name: self._crop(box) for name, box in ACTION_BOXES.items()}
        self._actions["update"] = self._update_sprite()
        self._equipment_word_badge = self._equipment_badge("word")
        self._unlocked = self._unlock_sprite()
        self._pin_on = self._pin_sprite(True)
        self._pin_off = self._pin_sprite(False)

    def _boss_portrait(self, template_id, icon_name=''):
        filename = self._boss_portrait_templates.get(str(template_id or 0), '')
        if not filename:
            safe_icon = Path(str(icon_name or '')).name
            if (
                safe_icon == str(icon_name or '')
                and safe_icon in self._boss_portrait_filenames
            ):
                filename = safe_icon
        if not filename or Path(filename).name != filename:
            return self._boss
        if filename not in self._boss_portraits:
            try:
                with Image.open(self.asset_dir / 'bosses/hud' / filename) as source:
                    portrait = source.convert('RGBA')
                bounds = portrait.getchannel('A').getbbox()
                if bounds:
                    portrait = portrait.crop(bounds)
                self._boss_portraits[filename] = portrait.resize((128, 128), Image.Resampling.LANCZOS)
            except (OSError, ValueError):
                self._boss_portraits[filename] = self._boss
        return self._boss_portraits[filename]

    def _crop(self, box):
        return self.reference.crop(box)

    def _font(self, size, role="name"):
        key = (round(size), role)
        if key not in self._fonts:
            path = self.font_paths[role]
            self._fonts[key] = ImageFont.truetype(str(path), max(8, round(size)))
        return self._fonts[key]

    def _source_digits(self):
        # Ink rectangles measured from the white rate labels. This reusable
        # alphabet renders arbitrary live values, not a baked example number.
        boxes = {
            "1": (567, 303, 583, 342), ",": (585, 303, 596, 342),
            "0": (595, 303, 618, 342), "4": (618, 303, 639, 342),
            "8": (639, 303, 660, 342), "2": (669, 303, 690, 342),
            "/": (731, 303, 747, 342), "s": (746, 303, 765, 342),
            "3": (616, 453, 638, 492), "9": (638, 453, 660, 492),
            "6": (689, 453, 711, 492), "7": (652, 679, 674, 718),
            "5": (576, 978, 599, 1017),
        }
        result = {}
        for char, box in boxes.items():
            red, green, blue, _alpha = self._crop(box).split()
            ink = ImageChops.darker(ImageChops.darker(red, green), blue)
            # Source edges transition from nearly black outline to white
            # glyph. Recover their coverage instead of thresholding to 1-bit.
            ink = ink.point(lambda n: max(0, min(255, round((n - 105) * 255 / 140))))
            if char.isdigit():
                # One tabular digit cell and one cap height for every digit;
                # reference glyphs were sampled from different example rows.
                bounds = ink.point(lambda n: 255 if n > 90 else 0).getbbox()
                if bounds:
                    glyph = ink.crop(bounds)
                    glyph = glyph.resize((max(1, round(glyph.width * 29 / glyph.height)), 29), Image.Resampling.LANCZOS)
                    cell = Image.new('L', (22, 39))
                    cell.paste(glyph, ((22 - glyph.width) // 2, 3))
                    ink = cell
            result[char] = ink
        # Time uses the same numeral outlines, with a neutral colon glyph.
        colon = Image.new("L", (11, 39))
        draw = ImageDraw.Draw(colon)
        draw.ellipse((3, 12, 7, 16), fill=255)
        draw.ellipse((3, 26, 7, 30), fill=255)
        result[":"] = colon
        return result

    def _unlock_sprite(self):
        """Keep the supplied button/body; open only its shackle for the state."""
        sprite = self._actions["lock"].copy()
        # Relative to the lock tile, the unlabelled area on the left carries
        # the original background at the same vertical position.
        shackle_box = (22, 17, 51, 41)
        backdrop = sprite.crop((10, 17, 13, 41)).resize((29, 24))
        sprite.paste(backdrop, shackle_box)
        shackle = self._actions["lock"].crop(shackle_box)
        r, g, b, _a = shackle.split()
        mask = ImageChops.darker(ImageChops.darker(r, g), b).point(lambda n: max(0, min(255, (n - 75) * 2)))
        shackle.putalpha(mask)
        shackle = shackle.rotate(-28, resample=Image.Resampling.BICUBIC, expand=True)
        sprite.alpha_composite(shackle, (24, 12))
        return sprite

    def _update_sprite(self):
        sprite = self._actions['pin'].copy()
        middle = sprite.crop((11, 15, 13, 73)).resize((59, 58))
        sprite.paste(middle, (13, 15))
        size = sprite.size
        sprite = sprite.resize((size[0] * 3, size[1] * 3), Image.Resampling.LANCZOS)
        draw = ImageDraw.Draw(sprite)
        ink = (239, 245, 250, 255)
        draw.arc((22 * 3, 24 * 3, 62 * 3, 64 * 3), 45, 330, fill=ink, width=5 * 3)
        draw.polygon([(64 * 3, 40 * 3), (49 * 3, 40 * 3), (64 * 3, 25 * 3)], fill=ink)
        return sprite.resize(size, Image.Resampling.LANCZOS)

    def _pin_sprite(self, active):
        """Replace only the former × glyph, retaining the approved button rim."""
        sprite = self._actions['pin'].copy()
        # Reconstruct the empty centre from a clean strip inside the original
        # tile; no part of the old close glyph remains underneath the pin.
        middle = sprite.crop((11, 15, 13, 73)).resize((59, 58))
        sprite.paste(middle, (13, 15))
        size = sprite.size
        sprite = sprite.resize((size[0] * 3, size[1] * 3), Image.Resampling.LANCZOS)
        draw = ImageDraw.Draw(sprite)
        ink = (113, 207, 255, 255) if active else (239, 245, 250, 255)
        points = [(31, 28), (54, 28), (51, 43), (59, 50), (59, 54), (25, 54), (25, 50), (33, 43)]
        draw.polygon([(x * 3, y * 3) for x, y in points], fill=ink)
        draw.rounded_rectangle((27 * 3, 21 * 3, 57 * 3, 29 * 3), radius=6, fill=ink)
        draw.polygon([(40 * 3, 53 * 3), (45 * 3, 53 * 3), (45 * 3, 65 * 3), (42 * 3, 73 * 3), (40 * 3, 65 * 3)], fill=ink)
        if active:
            draw.line((19 * 3, 77 * 3, 65 * 3, 77 * 3), fill=(70, 180, 255, 255), width=5)
        return sprite.resize(size, Image.Resampling.LANCZOS)




    def _equipment_badge(self, kind):
        """Small equipment markers used beside the live team rating."""

        scale = 3
        extent = 30 * scale
        image = Image.new("RGBA", (extent, extent))
        draw = ImageDraw.Draw(image)
        # A warm water drop mirrors the in-game "bean" marker more directly
        # than the previous green enhancement hexagon.
        drop = (
            (15, 2), (18, 7), (22, 12), (25, 17), (25, 21),
            (23, 25), (20, 28), (15, 29), (10, 28), (7, 25),
            (5, 21), (5, 17), (8, 12), (12, 7),
        )
        draw.polygon(
            [(x * scale, y * scale) for x, y in drop],
            fill=(255, 203, 42, 255),
            outline=(126, 78, 5, 255),
            width=2 * scale,
        )
        draw.ellipse(
            (9 * scale, 9 * scale, 14 * scale, 16 * scale),
            fill=(255, 250, 193, 225),
        )
        draw.arc(
            (7 * scale, 7 * scale, 23 * scale, 27 * scale),
            28,
            154,
            fill=(255, 231, 91, 255),
            width=2 * scale,
        )
        return image.resize((30, 30), Image.Resampling.LANCZOS)

    def _strip_skin(self, name, width):
        """Stretch a text-free strip of the actual art, retaining its caps."""
        width = max(40, int(width))
        key = (name, width)
        if key in self._skins:
            return self._skins[key]
        if name == 'self':
            tile = self._self_highlight(width)
            self._skins[key] = tile
            return tile
        specs = {
            "boss": ((236, 194, 1097, 273), 21, 21, 520),
            "self": ((191, 578, 1093, 663), 24, 24, 538),
            "team": ((248, 1039, 682, 1125), 23, 25, 428),
        }
        box, left_cap, right_cap, sample_x = specs[name]
        height = box[3] - box[1]
        tile = Image.new("RGBA", (width, height))
        middle = self._crop((sample_x, box[1], sample_x + 2, box[3])).resize((max(1, width - left_cap - right_cap), height))
        left = self._crop((box[0], box[1], box[0] + left_cap, box[3]))
        right = self._crop((box[2] - right_cap, box[1], box[2], box[3]))
        if name in {"boss", "team"}:
            # The original left cap sits under the crest/avatar; mirroring the
            # clean right cap prevents copying adjacent names into the skin.
            left = right.transpose(Image.Transpose.FLIP_LEFT_RIGHT).resize((left_cap, height))
        tile.alpha_composite(left, (0, 0))
        tile.alpha_composite(middle, (left_cap, 0))
        tile.alpha_composite(right, (width - right_cap, 0))
        self._skins[key] = tile
        return tile

    def _self_highlight(self, width):
        """Soft source-colour glass highlight, with no tiled cap artefacts."""
        height = 85
        tile = Image.new('RGBA', (width, height))
        bounds = (5, 7, width - 6, height - 8)
        mask = Image.new('L', tile.size)
        ImageDraw.Draw(mask).rounded_rectangle(bounds, radius=34, fill=255)
        halo = Image.new('RGBA', tile.size, (0, 139, 255, 0))
        halo.putalpha(mask.filter(ImageFilter.GaussianBlur(4.0)).point(lambda n: round(n * 0.40)))
        tile.alpha_composite(halo)
        plate = Image.new('RGBA', tile.size)
        draw = ImageDraw.Draw(plate)
        # Average a clean, text-free strip from the approved self row. This
        # retains its blue vertical shading without stretching image noise,
        # pieces of the example crest or the example nickname into the rim.
        for y in range(height):
            sample = self._crop((516, 578 + y, 550, 579 + y))
            r, g, b, _a = ImageStat.Stat(sample).mean
            draw.line((0, y, width, y), fill=(round(r * 0.74), round(g * 0.82), round(b * 0.86), 255))
        plate.putalpha(mask.point(lambda n: round(n * 0.90)))
        tile.alpha_composite(plate)
        rim = Image.new('RGBA', tile.size)
        rd = ImageDraw.Draw(rim)
        rd.rounded_rectangle(bounds, radius=34, outline=(15, 171, 245, 210), width=2)
        rd.rounded_rectangle((8, 10, width - 9, height - 11), radius=31, outline=(114, 218, 255, 90), width=1)
        tile.alpha_composite(rim.filter(ImageFilter.GaussianBlur(0.4)))
        return tile

    def _pill(self, width, height, *, accent=(65, 83, 112), red=False, blue=False):
        key = ("pill", round(width), round(height), accent, red, blue)
        if key in self._skins:
            return self._skins[key]
        width, height = max(15, round(width)), max(15, round(height))
        result = Image.new("RGBA", (width, height))
        draw = ImageDraw.Draw(result)
        radius = min(height / 2 - 1, 22)
        draw.rounded_rectangle((1, 1, width - 2, height - 2), radius, fill=(6, 11, 20, 252), outline=accent + (235,), width=2)
        draw.rounded_rectangle((4, 4, width - 5, height - 5), max(1, radius - 3), outline=(183, 203, 224, 110), width=1)
        mask = Image.new("L", result.size)
        ImageDraw.Draw(mask).rounded_rectangle((6, 6, width - 7, height - 7), max(1, radius - 5), fill=245)
        gradient = Image.new("RGBA", result.size)
        gd = ImageDraw.Draw(gradient)
        for y in range(height):
            amount = y / max(1, height - 1)
            if red:
                color = (round(83 - 44 * amount), 2, round(27 - 11 * amount))
            elif blue:
                color = (
                    round(10 - 4 * amount),
                    round(55 - 29 * amount),
                    round(92 - 42 * amount),
                )
            else:
                color = (
                    round(30 - 22 * amount),
                    round(41 - 27 * amount),
                    round(59 - 36 * amount),
                )
            gd.line((0, y, width, y), fill=color + (255,))
        gradient.putalpha(mask)
        result.alpha_composite(gradient)
        self._skins[key] = result
        return result

    def _ink(self, text, height, numeric=False, regular=False):
        text = str(text or "--")
        if numeric and all(char in self._glyphs for char in text):
            pieces = [self._glyphs[c] for c in text]
            base = Image.new("L", (sum(part.width for part in pieces), 39))
            x = 0
            for part in pieces:
                base.paste(part, (x, 0))
                x += part.width
            factor = height / 29.0
            return base.resize((max(1, round(base.width * factor)), max(1, round(39 * factor))), Image.Resampling.LANCZOS)
        # Chinese and characters absent from the source use a real font; no
        # screenshot can supply glyphs for every possible new player name.
        font = self._font(48, "small" if regular else "name")
        bbox = font.getbbox(text)
        ink = Image.new("L", (max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])))
        ImageDraw.Draw(ink).text((-bbox[0], -bbox[1]), text, font=font, fill=255)
        if not any(c.isalnum() for c in text) and ink.getbbox():
            ink = ink.crop(ink.getbbox())
        # Missing values are literal '--', not a pair of height-normalized
        # giant bars. Punctuation retains the same em size as nearby digits.
        ink_height = ink.height if any(c.isalnum() for c in text) else font.getbbox("0")[3] - font.getbbox("0")[1]
        factor = height / max(1, ink_height)
        return ink.resize((max(1, round(ink.width * factor)), max(1, round(ink.height * factor))), Image.Resampling.LANCZOS)

    def _label(self, text, height, *, color=(249, 250, 253), numeric=False, contour=False, flat=False, regular=False, padding=None, stroke_radius=3):
        # Missing data is invisible in the HUD. Keep the domain model's None
        # / unknown distinction, and never fabricate zero to fill a column.
        if text is None or str(text).strip() in {'', '--', '-- / --', '--/--'}:
            return Image.new('RGBA', (1, 1))
        pad = (13 if contour else 7) if padding is None else int(padding)
        key = (str(text), round(height, 2), color, numeric, contour, flat, regular, pad, stroke_radius)
        if key in self._labels:
            self._labels.move_to_end(key)
            return self._labels[key]
        ink = self._ink(text, height, numeric, regular)
        mask = Image.new("L", (ink.width + pad * 2, ink.height + pad * 2))
        mask.paste(ink, (pad, pad))
        result = Image.new("RGBA", mask.size)

        def layer(coverage, rgb):
            sheet = Image.new("RGBA", mask.size, rgb + (0,))
            sheet.putalpha(coverage)
            result.alpha_composite(sheet)

        if contour:
            teal = color[1] > 180 and color[0] < 90
            # Rounded contour follows the source's letter-shaped dark pills.
            # A square dilation alone produces the boxy previous appearance.
            def contour_mask(radius):
                return _dilate(mask, radius // 2).filter(ImageFilter.GaussianBlur(3.2)).point(lambda n: max(0, min(255, round((n - 78) * 255 / 110))))
            outer = contour_mask(23)
            layer(outer, (2, 88, 96) if teal else (65, 84, 109))
            inner = contour_mask(19)
            layer(inner, (7, 19, 24) if teal else (6, 13, 23))
            middle = contour_mask(13)
            layer(middle, (7, 23, 29) if teal else (19, 29, 42))
        edge = _dilate(mask, stroke_radius).filter(ImageFilter.GaussianBlur(0.65))
        layer(edge, (0, 3, 8))
        if flat:
            layer(mask, color)
        else:
            fill = Image.new("RGBA", mask.size)
            draw = ImageDraw.Draw(fill)
            for y in range(mask.height):
                shade = 1 - 0.11 * max(0, y - pad) / max(1, ink.height)
                draw.line((0, y, mask.width, y), fill=tuple(round(c * shade) for c in color) + (255,))
            fill.putalpha(mask)
            result.alpha_composite(fill)
        result.info['hud_ink_bounds'] = mask.point(lambda n: 255 if n > 96 else 0).getbbox() or (0, 0, result.width, result.height)
        self._labels[key] = result
        while len(self._labels) > 256:
            self._labels.popitem(last=False)
        return result

    def _fitted_label(self, text, max_width, height, *, truncate=True, **options):
        label = self._label(text, height, **options)
        if label.width <= max_width:
            return label
        text = str(text or "--")
        # Names truncate; reliable numbers remain complete at a smaller size.
        if truncate and not options.get("numeric"):
            while text and label.width > max_width:
                text = text[:-1]
                label = self._label(text + "…", height, **options)
        if label.width > max_width:
            factor = max_width / label.width
            bounds = label.info.get('hud_ink_bounds')
            label = label.resize((max(1, round(max_width)), max(1, round(label.height * factor))), Image.Resampling.LANCZOS)
            if bounds:
                label.info['hud_ink_bounds'] = tuple(value * factor for value in bounds)
        return label

    def _footer_summary_pair(self, caption_text, value_text, max_width, font_factor):
        caption = self._label(caption_text, SUMMARY_FONT_HEIGHT * font_factor)
        value = self._label(
            value_text, SUMMARY_FONT_HEIGHT * font_factor,
            color=(134, 198, 255), flat=True,
        )
        gap = 4
        total_width = caption.width + gap + value.width
        if total_width > max_width:
            factor = (max_width - gap) / (caption.width + value.width)
            caption = caption.resize(
                (max(1, int(caption.width * factor)), max(1, round(caption.height * factor))),
                Image.Resampling.LANCZOS,
            )
            value = value.resize(
                (max(1, int(value.width * factor)), max(1, round(value.height * factor))),
                Image.Resampling.LANCZOS,
            )
        return caption, value

    def _profession(self, profession_id):
        profession_id = int(profession_id or 0)
        if profession_id in self._professions:
            return self._professions[profession_id]
        rgb = _color(self.profession_colors.get(profession_id, "#71869a"))
        hue, saturation, value = colorsys.rgb_to_hsv(*(channel / 255 for channel in rgb))
        if profession_id in self.profession_colors:
            # Keep the established hue mapping, but match the reference's
            # saturated class-coloured aura instead of pale outlined disks.
            rgb = tuple(round(channel * 255) for channel in colorsys.hsv_to_rgb(hue, max(0.84, saturation), max(0.94, value)))
        extent, aa = 94, 2
        high = extent * aa
        image = Image.new("RGBA", (high, high))
        path = self.asset_dir / "professions" / f"{profession_id}.png"
        if path.is_file():
            source = Image.open(path).convert("RGBA")
            bounds = source.getchannel("A").getbbox()
            if bounds:
                source = source.crop(bounds)
                source.thumbnail((80 * aa, 80 * aa), Image.Resampling.LANCZOS)
                coverage = source.getchannel("A")
                glyph_mask = Image.new("L", image.size)
                glyph_mask.paste(coverage, ((high - source.width) // 2, (high - source.height) // 2))
                # Glow follows the real symbol, with no circular substrate.
                for blur, opacity in ((4.1, 0.94), (1.5, 1.0)):
                    glow = Image.new("RGBA", image.size, rgb + (0,))
                    glow.putalpha(glyph_mask.filter(ImageFilter.GaussianBlur(blur * aa)).point(lambda n, strength=opacity: round(n * strength)))
                    image.alpha_composite(glow)
                edge = Image.new('RGBA', image.size, tuple(round(c * 0.12) for c in rgb) + (0,))
                edge.putalpha(_dilate(glyph_mask, 1).filter(ImageFilter.GaussianBlur(0.5)))
                image.alpha_composite(edge)
                # Colour the pattern itself: a bright upper edge and a rich
                # class-colour lower edge retain the game's inner geometry.
                glyph = Image.new("RGBA", image.size)
                gd = ImageDraw.Draw(glyph)
                for y in range(high):
                    light = 0.60 - 0.30 * y / max(1, high - 1)
                    shade = tuple(round(c + (255 - c) * light) for c in rgb)
                    gd.line((0, y, high, y), fill=shade + (255,))
                glyph.putalpha(glyph_mask)
                image.alpha_composite(glyph)
        image = image.resize((extent, extent), Image.Resampling.LANCZOS)
        self._professions[profession_id] = image
        return image

    def _equipment_refresh_button(self, row, font_factor, hover):
        token = str(row.get("equipment_refresh_token") or "").strip()
        if not token or row.get("is_ai") or row.get("is_self"):
            return None
        pending = bool(row.get("equipment_refresh_pending"))
        action = "refresh_equipment:" + token
        tile = Image.new("RGBA", (90, 42))
        draw = ImageDraw.Draw(tile)
        draw.rounded_rectangle(
            (1, 1, 88, 40), radius=9,
            fill=(37, 58, 76, 225) if pending else (21, 145, 201, 250)
            if hover == action else (18, 103, 156, 245),
            outline=(106, 135, 159, 220) if pending else (104, 219, 255, 255),
            width=2,
        )
        text = self._fitted_label(
            "刷新中" if pending else "刷新", 78, 25 * font_factor,
            color=(157, 182, 201) if pending else (241, 253, 255),
            regular=True, stroke_radius=0, padding=4,
        )
        ink = text.info.get("hud_ink_bounds", (0, 0, text.width, text.height))
        tile.alpha_composite(text, (
            round((90 - (ink[2] - ink[0])) / 2 - ink[0]),
            round(text_top_for_center(text, 21)),
        ))
        region = ("equipment_refresh_pending:" + token) if pending else ("action:" + action)
        return tile, region


    def render(self, snapshot: Mapping[str, object], *, pixel_scale=1.0, font_size=14):
        pixel_scale = max(0.75, min(4.0, float(pixel_scale)))
        font_factor = max(0.8, min(1.4, float(font_size) / 14))
        rows = [dict(row) for row in snapshot.get("rows", ()) if isinstance(row, Mapping)]
        application_rows = [
            dict(row)
            for row in snapshot.get("application_rows", ())
            if isinstance(row, Mapping)
        ]
        capacity = clamp_visible_rows(
            snapshot.get("visible_rows", HUD_DEFAULT_VISIBLE_ROWS)
        )
        # A user-sized viewport keeps its height even with no party or fewer
        # members. Otherwise each paint immediately undoes an outward drag.
        count = capacity if "visible_rows" in snapshot else max(1, min(capacity, len(rows)))
        start = min(max(0, len(rows) - count), max(0, int(snapshot.get("start_index", 0))))
        shown_rows = rows[start:start + count]
        deaths = bool(snapshot.get("show_deaths", True))
        rating = bool(snapshot.get("rating_preview", False))
        totals = bool(snapshot.get("show_totals", True)) and not rating
        interaction_blocked = bool(snapshot.get("interaction_blocked", False))
        width_removed = 0 if deaths else DEATH_COLUMN_WIDTH
        prediction = snapshot.get("prediction")
        show_boss = bool(snapshot.get("show_boss", True))
        boss_sources = [value for value in snapshot.get('bosses', ()) if isinstance(value, Mapping)][:2] or [snapshot]
        boss_extra = BOSS_BAR_EXTRA_HEIGHT + (len(boss_sources) - 1) * 110 if show_boss else 0
        show_time = bool(snapshot.get("show_time", True))
        show_prediction = bool(show_boss and isinstance(prediction, Mapping) and prediction.get("message"))
        # The administrator indicator is always present in the top band, even
        # when the user hides the combat timer.
        top_removed = 0 + (0 if show_boss else 88)
        footer_offset = round((10 - count) * 75.4)
        update_status_text = str(snapshot.get("update_status_text", "") or "").strip()
        update_status_height = 76 if update_status_text else 0
        source_width = REFERENCE_WIDTH - width_removed
        source_height = HUD_BASE_SOURCE_HEIGHT - footer_offset - top_removed + boss_extra + update_status_height
        frame = Image.new("RGBA", (source_width, source_height), (0, 0, 0, 1))
        regions = {}
        actors = []

        def paste(image, x, y):
            frame.alpha_composite(image, (round(x - 146), round(y - 126 - top_removed)))

        def paste_text(tile, x, center_y):
            paste(tile, x, text_top_for_center(tile, center_y))

        def label(text, x, y, height, *, anchor="left", max_width=None, **opts):
            tile = self._fitted_label(text, max_width, height * font_factor, **opts) if max_width else self._label(text, height * font_factor, **opts)
            left = x - (tile.width if anchor == "right" else tile.width / 2 if anchor == "center" else 0)
            paste_text(tile, left, y)
            return tile

        track_left, track_right = 258, 1083 - width_removed
        timer_right = 177
        if show_time:
            timer = self._label(str(snapshot.get("time", "00:00")), 36 * font_factor, numeric=True)
            timer_right += timer.width
            # Timer remains at the design's top-left even when boss is hidden.
            timer_y = 129 + (88 if not show_boss else 0)
            # Only outlined text: no pill, tile or background behind the clock.
            paste_text(timer, 177, timer_y + 29)

        # Keep the compact title-rail navigation
        # it immediately to the right of the clock so the content switch does
        # not consume another row or alter the approved window dimensions.
        if "pve_hud_view" in snapshot:
            pve_view = (
                "team"
                if str(snapshot.get("pve_hud_view") or "recent_battle") == "team"
                else "recent_battle"
            )
            hover_action = str(snapshot.get("hover_action", "") or "")
            for view, caption, action, left, accent in (
                (
                    "team",
                    "团队构成",
                    "pve_team",
                    TITLE_RAIL_TAB_LEFT,
                    (80, 189, 246),
                ),
                (
                    "recent_battle",
                    "DPS数据",
                    "pve_recent_battle",
                    TITLE_RAIL_TAB_LEFT
                    + TITLE_RAIL_TAB_WIDTH
                    + TITLE_RAIL_TAB_GAP,
                    (111, 224, 173),
                ),
            ):
                right = left + TITLE_RAIL_TAB_WIDTH
                selected = pve_view == view
                hovered = hover_action == action
                tile = self._pill(
                    right - left,
                    46,
                    accent=accent,
                    red=False,
                    blue=view == "team" and selected,
                )
                if selected:
                    tile = tile.copy()
                    ImageDraw.Draw(tile).rounded_rectangle(
                        (1, 1, tile.width - 2, tile.height - 2),
                        radius=22,
                        fill=accent + (255,),
                        outline=(235, 250, 255, 255),
                        width=3,
                    )
                elif not hovered:
                    tile = tile.copy()
                    tile.putalpha(
                        tile.getchannel("A").point(lambda value: value * 3 // 5)
                    )
                paste(tile, left, 135)
                label(
                    caption,
                    (left + right) / 2,
                    158,
                    28,
                    anchor="center",
                    max_width=right - left - 10,
                    color=(250, 252, 255) if selected else (226, 236, 246),
                    regular=False,
                    flat=True,
                    padding=3,
                    stroke_radius=2 if selected else 1,
                    truncate=False,
                )
                regions[f"action:{action}"] = (left, 135, right, 181)

            database_left, database_right = 690, 850
            database_hovered = hover_action == "web_database"
            database_tile = self._pill(
                database_right - database_left,
                46,
                accent=(215, 179, 109),
                red=False,
                blue=False,
            )
            if not database_hovered:
                database_tile = database_tile.copy()
                database_tile.putalpha(
                    database_tile.getchannel("A").point(lambda value: value * 4 // 5)
                )
            paste(database_tile, database_left, 135)
            label(
                "网页数据库",
                (database_left + database_right) / 2,
                158,
                25,
                anchor="center",
                max_width=database_right - database_left - 10,
                color=(255, 229, 169) if database_hovered else (239, 217, 174),
                regular=False,
                flat=True,
                padding=3,
                stroke_radius=1,
                truncate=False,
            )
            regions["action:web_database"] = (
                database_left,
                135,
                database_right,
                181,
            )

        indicator_color = (
            ADMIN_INDICATOR_ELEVATED
            if bool(snapshot.get("admin_elevated", False))
            else ADMIN_INDICATOR_STANDARD
        )
        indicator = Image.new("RGBA", (48, 48))
        indicator_draw = ImageDraw.Draw(indicator)
        indicator_draw.ellipse((3, 3, 45, 45), fill=indicator_color + (28,))
        indicator_draw.ellipse((11, 11, 37, 37), fill=indicator_color + (255,))
        indicator_draw.ellipse((15, 14, 24, 23), fill=(255, 255, 255, 105))
        # Align the lamp with the death-count column instead of pinning it to
        # the extreme corner, with a small downward visual inset.
        paste(indicator, 995 - width_removed, 134 + top_removed)

        if show_boss:
            for boss_index, boss_snapshot in enumerate(boss_sources):
                boss_offset = boss_index * 110
                right = 1097 - width_removed
                if bool(boss_snapshot.get("boss_mode_only", False)):
                    mode_name = str(
                        boss_snapshot.get("boss_name") or "团队战斗"
                    ).strip()
                    label(
                        mode_name,
                        171,
                        232 + boss_offset,
                        31,
                        max_width=max(120, right - 195),
                        color=(220, 239, 250),
                        regular=True,
                        stroke_radius=2,
                    )
                    continue
                skin = self._strip_skin("boss", right - 236)
                skin = skin.resize((skin.width, skin.height + BOSS_BAR_EXTRA_HEIGHT), Image.Resampling.LANCZOS)
                try:
                    ratio = max(0.0, min(1.0, float(boss_snapshot.get("boss_ratio", 0) or 0)))
                except (TypeError, ValueError, OverflowError):
                    ratio = 0
                boss_available = bool(
                    boss_snapshot.get("boss_available", False)
                )
                # Draw a low-contrast remaining-HP fill below the original rim.
                # The hue/rim/gloss all come from the approved red bar texture.
                hp_known = boss_snapshot.get("boss_ratio") is not None
                if boss_available:
                    paste(skin, 236, 194 + boss_offset)
                hp_fill = Image.new("RGBA", skin.size)
                fd = ImageDraw.Draw(hp_fill)
                if hp_known:
                    # Fill and warning marker share the exact same inner track.
                    spent_left = round(track_left - 236 + (track_right - track_left) * ratio)
                    if ratio > 0:
                        fd.rectangle((22, 12, spent_left, 64 + BOSS_BAR_EXTRA_HEIGHT), fill=(206, 7, 48, 120))
                    if spent_left < skin.width - 14:
                        fd.rectangle((spent_left, 12, skin.width - 14, 64 + BOSS_BAR_EXTRA_HEIGHT), fill=(5, 7, 13, 238))
                    paste(hp_fill, 236, 194 + boss_offset)
                elif boss_available:
                    # A CurrentHP-only packet cannot prove a percentage.  A
                    # full-width red fill reads as 100% HP, so keep the track
                    # dark until a real MaxHP makes the remaining ratio known.
                    # The exact CurrentHP number is still rendered below.
                    fd.rectangle(
                        (22, 12, skin.width - 14, 64 + BOSS_BAR_EXTRA_HEIGHT),
                        fill=(5, 7, 13, 238),
                    )
                    paste(hp_fill, 236, 194 + boss_offset)
                hp = str(boss_snapshot.get("boss_hp", "-- / --"))
                if '--' in hp:
                    hp = ''
                percent = str(boss_snapshot.get("boss_percent", "--"))
                # Boss copy and the team-DPS footer use the same real font/size;
                # both are larger than the per-player statistic/name typography.
                percent_tile = self._label(percent, (SUMMARY_FONT_HEIGHT - 4) * font_factor, color=(255, 117, 131))
                percent_right = right - 13
                hp_right = percent_right - percent_tile.width - 7
                boss_name = str(boss_snapshot.get('boss_name', '暂无目标') or '暂无目标')
                live_no_boss = bool(boss_snapshot.get('live_no_boss'))
                bar_center_y = 194 + boss_offset + skin.height / 2
                available = bool(boss_snapshot.get('boss_available', boss_name != '暂无目标'))
                try:
                    boss_level = int(boss_snapshot.get('boss_level', 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    boss_level = 0
                level_tile = None
                if available and 1 <= boss_level <= 999:
                    level_tile = self._label(f'Lv.{boss_level}', 28 * font_factor,
                        regular=True, color=(200, 214, 228))
                name_left = 281 + (level_tile.width + 10 if level_tile is not None else 0)
                name_budget = min(
                    420 if live_no_boss else 210,
                    max(0, hp_right - name_left - (20 if live_no_boss else 260)),
                )
                name_tile = None
                if available and boss_name and name_budget > 0:
                    name_tile = self._fitted_label(boss_name, name_budget, 28 * font_factor,
                        regular=True, color=(247, 207, 109) if live_no_boss else (200, 214, 228))
                hp_budget = hp_right - name_left - (name_tile.width + 12 if name_tile is not None else 12)
                hp_tile = self._fitted_label(hp, max(130, hp_budget), (SUMMARY_FONT_HEIGHT - 4) * font_factor, truncate=False)
                if level_tile is not None:
                    paste_text(level_tile, 281, bar_center_y)
                if name_tile is not None:
                    paste_text(name_tile, name_left, bar_center_y)
                if available:
                    hp_left, percent_left = hp_right - hp_tile.width, percent_right - percent_tile.width
                else:
                    hp_left, percent_left = boss_placeholder_anchors(281, percent_right, hp_tile.width, percent_tile.width)
                paste_text(hp_tile, hp_left, bar_center_y)
                paste_text(percent_tile, percent_left, bar_center_y)
                paste(
                    self._boss_portrait(
                        boss_snapshot.get('boss_template_id'),
                        boss_snapshot.get('boss_icon')
                        if live_no_boss or boss_snapshot.get('live_hud_boss')
                        else '',
                    ),
                    153,
                    170 + boss_offset + BOSS_BAR_EXTRA_HEIGHT / 2,
                )

        if show_prediction:
            state = str(prediction.get("state", "danger"))
            accent = {"ample": (44, 211, 153), "normal": (78, 167, 238), "critical": (237, 184, 51), "danger": (255, 25, 70)}.get(state, (147, 166, 184))
            label_left_limit = max(track_left, timer_right + 12 if show_time else track_left)
            label_right_limit = 1097 - width_removed
            available = label_right_limit - label_left_limit
            message = self._fitted_label(prediction.get("message", ""), min(310, available - 56), 27 * font_factor, truncate=False, color=(255, 130, 145) if state == "danger" else accent, regular=True)
            pwidth = min(available, max(88, message.width + 51))
            try:
                marker = float(prediction.get("marker", 0))
                marker = min(1.0, max(0.0, marker)) if math.isfinite(marker) else 0.0
            except (TypeError, ValueError, OverflowError):
                marker = 0.0
            marker_x = track_left + (track_right - track_left) * marker
            # Only the label is constrained by the clock/window edges. The
            # actual marker reaches every point of the blood bar, including 0.
            px = min(label_right_limit - pwidth, max(label_left_limit, marker_x - pwidth / 2))
            panel = self._pill(pwidth, 62, accent=accent, red=state == "danger")
            paste(panel, px, 132)
            paste(self._warning, px + 17, 146)
            paste_text(message, px + 43, 163)
            # Keep the artwork's small pointer above HP. Near the left edge a
            # short leader connects it to the label without covering the clock.
            leader_x = min(px + pwidth - 14, max(px + 14, marker_x))
            if abs(leader_x - marker_x) > 1:
                ImageDraw.Draw(frame).line(
                    ((round(leader_x - 146), 188 - 126 - top_removed),
                     (round(marker_x - 146), 193 - 126 - top_removed)),
                    fill=accent + (255,), width=2,
                )
            pointer = Image.new("RGBA", (29, 17))
            ImageDraw.Draw(pointer).polygon(((0, 0), (28, 0), (14, 16)), fill=accent + (255,))
            paste(pointer, marker_x - 14, 189)
            regions["prediction"] = (px, 132, px + pwidth, 194)
            regions["prediction_marker"] = (marker_x - 14, 189, marker_x + 15, 206)

        # Keep the DPS and its attached total together, but move the whole pair
        # close to the skull instead of leaving a conspicuous empty column.
        # These are source-art pixels: the 30-pixel follow-up adjustment is
        # roughly nine logical HUD pixels at 100% scale.
        stat_right = 818
        total_right = 937
        rate_height = 29 * font_factor
        statistic_rows = [
            row for row in rows
            if not row.get("row_kind") and row.get("metric") != "rating"
        ]
        if not rating and statistic_rows:
            largest = max(self._label(str(row.get('stat_text') or '--'), rate_height, numeric=True, padding=13).width for row in statistic_rows)
            column_width = 223 if totals else 381
            if largest > column_width:
                rate_height *= (column_width - 26) / max(1, largest - 26)
        if not totals and not rating:
            stat_right = total_right
        for index, row in enumerate(shown_rows):
            cy = (ROW_CENTERS[index] if index < len(ROW_CENTERS) else ROW_CENTERS[-1] + (index - 9) * 75.4) + boss_extra
            if row.get("row_kind") == "tabs":
                active_tab = str(
                    row.get("active_tab", "recent_battle")
                    or "recent_battle"
                )
                tab_left = 211
                tab_right = 1093 - width_removed
                tab_gap = 12
                tab_width = (tab_right - tab_left - tab_gap) / 2
                tab_top = cy - 30
                tab_bottom = cy + 30
                hover_action = str(snapshot.get("hover_action", "") or "")
                draw = ImageDraw.Draw(frame)
                for tab_name, caption, left in (
                    (
                        "recent_battle",
                        str(row.get("recent_battle_text") or "实时战斗/战斗记录"),
                        tab_left,
                    ),
                    (
                        "team_rating",
                        str(row.get("team_rating_text") or "队伍非凡评分"),
                        tab_left + tab_width + tab_gap,
                    ),
                ):
                    right = left + tab_width
                    selected = active_tab == tab_name
                    hovered = hover_action == f"{tab_name}_tab"
                    local_box = (
                        round(left - 146),
                        round(tab_top - 126 - top_removed),
                        round(right - 146),
                        round(tab_bottom - 126 - top_removed),
                    )
                    draw.rounded_rectangle(
                        local_box,
                        radius=14,
                        fill=(
                            (22, 63, 88, 205)
                            if selected
                            else (13, 28, 42, 155)
                            if hovered
                            else (7, 17, 29, 118)
                        ),
                        outline=(
                            (90, 186, 235, 230)
                            if selected
                            else (83, 111, 136, 120)
                        ),
                        width=2,
                    )
                    if selected:
                        draw.line(
                            (
                                round(left + 18 - 146),
                                round(tab_bottom - 4 - 126 - top_removed),
                                round(right - 18 - 146),
                                round(tab_bottom - 4 - 126 - top_removed),
                            ),
                            fill=(105, 207, 255, 255),
                            width=3,
                        )
                    label(
                        caption,
                        (left + right) / 2,
                        cy,
                        26,
                        anchor="center",
                        max_width=tab_width - 24,
                        regular=not selected,
                        color=(222, 242, 255) if selected else (157, 180, 202),
                        contour=True,
                        truncate=False,
                    )
                    regions[f"action:{tab_name}_tab"] = (
                        left,
                        tab_top,
                        right,
                        tab_bottom,
                    )
                continue
            if row.get("row_kind") == "team_application":
                draw = ImageDraw.Draw(frame)
                left, right = 203, 1082 - width_removed
                top, bottom = cy - 30, cy + 30
                draw.rounded_rectangle(
                    (
                        round(left - 146),
                        round(top - 126 - top_removed),
                        round(right - 146),
                        round(bottom - 126 - top_removed),
                    ),
                    radius=12,
                    fill=(18, 151, 80, 242),
                    outline=(102, 255, 166, 255),
                    width=2,
                )
                label(
                    str(row.get("application_text") or "入队申请"),
                    (left + right) / 2,
                    cy,
                    27,
                    anchor="center",
                    max_width=right - left - 32,
                    regular=True,
                    color=(247, 255, 250),
                    contour=True,
                    truncate=False,
                )
                continue
            if row.get("row_kind") == "section":
                live_team_dps = str(row.get("live_team_dps") or "").strip()
                if not live_team_dps:
                    label(
                        str(row.get("section_text") or "实时战斗/战斗记录"),
                        626 - width_removed / 2,
                        cy,
                        27,
                        anchor="center",
                        max_width=650 - width_removed,
                        regular=False,
                        color=(214, 226, 240),
                        flat=True,
                        padding=3,
                        stroke_radius=1,
                        truncate=False,
                    )
                    continue

                section_left, section_right = 203, 1082 - width_removed
                section_width = section_right - section_left
                badge_width = max(340, min(500, section_width * 0.56))
                badge_left = section_right - badge_width
                heading_right = badge_left - 14
                label(
                    str(row.get("section_text") or "实时战斗/战斗记录"),
                    (section_left + heading_right) / 2,
                    cy,
                    27,
                    anchor="center",
                    max_width=max(80, heading_right - section_left - 8),
                    regular=False,
                    color=(214, 226, 240),
                    flat=True,
                    padding=3,
                    stroke_radius=1,
                    truncate=False,
                )

                draw = ImageDraw.Draw(frame)
                badge_top, badge_bottom = cy - 27, cy + 27
                draw.rounded_rectangle(
                    (
                        round(badge_left - 146),
                        round(badge_top - 126 - top_removed),
                        round(section_right - 146),
                        round(badge_bottom - 126 - top_removed),
                    ),
                    radius=14,
                    fill=(7, 22, 35, 218),
                    outline=(78, 194, 239, 235),
                    width=2,
                )
                dot_x = badge_left + 22 - 146
                dot_y = cy - 126 - top_removed
                draw.ellipse(
                    (dot_x - 11, dot_y - 11, dot_x + 11, dot_y + 11),
                    fill=(255, 49, 72, 58),
                )
                draw.ellipse(
                    (dot_x - 6, dot_y - 6, dot_x + 6, dot_y + 6),
                    fill=(255, 61, 80, 255),
                )
                value_tile = self._label(
                    live_team_dps,
                    24 * font_factor,
                    numeric=True,
                    color=(111, 238, 255),
                    flat=True,
                    padding=3,
                    stroke_radius=1,
                )
                value_left = section_right - 14 - value_tile.width
                paste_text(value_tile, value_left, cy)
                label(
                    str(row.get("live_dps_caption") or "实时团队秒伤"),
                    badge_left + 43,
                    cy,
                    26,
                    max_width=max(70, value_left - badge_left - 55),
                    regular=False,
                    color=(238, 247, 255),
                    flat=True,
                    padding=3,
                    stroke_radius=1,
                    truncate=False,
                )
                continue
            if row.get("row_kind") == "recent_boss":
                draw = ImageDraw.Draw(frame)
                left, right = 203, 1082 - width_removed
                top, bottom = cy - 30, cy + 30
                draw.rounded_rectangle(
                    (
                        round(left - 146),
                        round(top - 126 - top_removed),
                        round(right - 146),
                        round(bottom - 126 - top_removed),
                    ),
                    radius=12,
                    fill=(7, 18, 29, 138),
                    outline=(91, 119, 145, 175),
                    width=2,
                )
                portrait_icon = str(row.get("boss_icon") or "").strip()
                portrait = (
                    self._boss_portrait(
                        row.get("boss_template_id"), portrait_icon
                    )
                    if portrait_icon
                    else self._boss_portrait(row.get("boss_template_id"))
                ).resize((58, 58), Image.Resampling.LANCZOS)
                paste(portrait, 212, cy - 29)
                boss_name = str(row.get("boss_name") or "Boss")
                # The portrait is the only element outside the health bar.
                # Give the bar the full remaining row and place both identity
                # and health copy inside it so the record reads as one target.
                bar_left, bar_right = 278, 1062 - width_removed
                bar_top, bar_bottom = cy - 24, cy + 24
                local_bar = (
                    round(bar_left - 146),
                    round(bar_top - 126 - top_removed),
                    round(bar_right - 146),
                    round(bar_bottom - 126 - top_removed),
                )
                draw.rounded_rectangle(
                    local_bar,
                    radius=10,
                    fill=(4, 7, 12, 245),
                    outline=(178, 55, 72, 245),
                    width=2,
                )
                ratio_value = row.get("boss_ratio")
                try:
                    ratio = max(0.0, min(1.0, float(ratio_value)))
                except (TypeError, ValueError, OverflowError):
                    ratio = 0.0
                if ratio_value is not None and ratio > 0:
                    fill_right = bar_left + (bar_right - bar_left) * ratio
                    draw.rounded_rectangle(
                        (
                            round(bar_left + 2 - 146),
                            round(bar_top + 2 - 126 - top_removed),
                            round(max(bar_left + 4, fill_right) - 146),
                            round(bar_bottom - 2 - 126 - top_removed),
                        ),
                        radius=8,
                        fill=(225, 18, 57, 235),
                    )
                # With no verified MaxHP there is no defensible fill length.
                # The dark base track already communicates an unknown ratio;
                # never paint it full red merely because CurrentHP is known.
                hp = str(row.get("boss_hp") or "").strip()
                percent = str(row.get("boss_percent") or "").strip()
                health_text = "  ".join(value for value in (hp, percent) if value)
                name_left = bar_left + 16
                name_budget = min(240, max(120, (bar_right - bar_left) * 0.34))
                health_right = bar_right - 14
                health_budget = max(
                    120,
                    health_right - name_left - name_budget - 18,
                )
                label(
                    boss_name,
                    name_left,
                    cy,
                    27,
                    max_width=name_budget,
                    color=(246, 238, 240),
                    contour=True,
                )
                label(
                    health_text or "血量未记录",
                    health_right,
                    cy,
                    23,
                    anchor="right",
                    max_width=health_budget,
                    regular=True,
                    color=(243, 221, 225) if health_text else (142, 159, 176),
                    contour=True,
                    truncate=False,
                )
                continue
            if row.get("row_kind") == "message":
                # Center the empty state in the remaining viewport, not in
                # the first unused player row. Keep clear of the footer art.
                empty_top = cy - 37
                empty_bottom = 1029 - footer_offset + boss_extra
                message_y = (empty_top + max(empty_top, empty_bottom)) / 2
                label(
                    str(row.get("message_text") or "等待战斗数据"),
                    626 - width_removed / 2,
                    message_y,
                    31,
                    anchor="center",
                    max_width=850 - width_removed,
                    color=(244, 194, 103) if interaction_blocked else (183, 200, 216),
                    contour=True,
                    truncate=False,
                )
                continue
            self_row = bool(
                row.get("highlight_self_row", row.get("is_self"))
            ) and bool(snapshot.get("highlight_self", True))
            if self_row:
                paste(self._strip_skin("self", 902 - width_removed), 191, cy - 43)
                paste(self._self_arrow, 146, cy - 33)
            elif int(snapshot.get("row_mask_opacity", 0) or 0) > 0:
                mask_tile = Image.new("RGBA", (898 - width_removed, 75), (5, 12, 21, round(max(0, min(100, int(snapshot["row_mask_opacity"]))) * 1.4)))
                paste(mask_tile, 191, cy - 37)
            paste(self._profession(row.get("profession_id", 0)), 191, cy - 47)
            row_rating = rating or row.get("metric") == "rating"
            is_ai = bool(row.get("is_ai", False))
            refresh = self._equipment_refresh_button(
                row, font_factor, str(snapshot.get("hover_action", "") or ""),
            )
            if refresh and refresh[1] in regions:
                refresh = None
            inline_rating = str(row.get("inline_rating_text", "") or "").strip()
            if row_rating:
                inline_rating = (
                    "人机"
                    if is_ai
                    else str(row.get("rating_text", "--") or "--")
                )
            rating_color = (
                (116, 210, 235)
                if is_ai
                else (255, 226, 92)
                if self_row and inline_rating and inline_rating != "--"
                else (247, 207, 109)
                if inline_rating and inline_rating != "--"
                else (180, 192, 205)
            )
            rating_tile = None
            rating_width = 180 if self_row else 150 if refresh else 170
            if inline_rating:
                rating_tile = self._fitted_label(
                    f"（{inline_rating}）",
                    rating_width,
                    (34 if self_row else 30) * font_factor,
                    truncate=False,
                    color=rating_color,
                    contour=not self_row,
                    padding=6 if self_row else 13,
                )
            # The name and parenthesized rating together still fit before the
            # statistic column; keep the original full name budget so adding a
            # score does not turn ordinary two-to-four-character names into an
            # ellipsis.
            name_budget = 274
            if refresh:
                identity_limit = 740 - width_removed if row_rating else stat_right - (223 if totals else 381) - 10
                name_budget = max(36, identity_limit - 280 - (rating_width + INLINE_RATING_GAP if rating_tile else 0) - 102)
            name_tile = self._fitted_label(
                row.get("name", "玩家"),
                name_budget,
                min(36 * font_factor, 28) if refresh and name_budget < 150 else 36 * font_factor,
                contour=not self_row,
                padding=6 if refresh else 13,
            )
            paste_text(name_tile, 280, cy)
            name_ink = name_tile.info.get("hud_ink_bounds", (0, 0, name_tile.width, name_tile.height))
            identity_right = 280 + name_ink[2]
            if rating_tile is not None:
                name_ink = name_tile.info.get(
                    "hud_ink_bounds", (0, 0, name_tile.width, name_tile.height)
                )
                rating_ink = rating_tile.info.get(
                    "hud_ink_bounds", (0, 0, rating_tile.width, rating_tile.height)
                )
                rating_left = (
                    280 + name_ink[2] + INLINE_RATING_GAP - rating_ink[0]
                )
                paste_text(rating_tile, rating_left, cy)
                identity_right = rating_left + rating_ink[2]
            if refresh:
                button, region = refresh
                left = identity_right + 8
                paste(button, left, cy - 21)
                regions[region] = (left, cy - 21, left + button.width, cy + 21)
            if row_rating and bool(row.get("equipment_profile_ready", False)):
                pvp_count = max(0, int(row.get("pvp_equipment_count", 0) or 0))
                active_words = max(0, int(row.get("active_word_count", 0) or 0))
                total_words = max(0, int(row.get("total_word_count", 0) or 0))
                badge_right = 1060 - width_removed
                word_text = f"{active_words}/{total_words}"
                word_tile = self._fitted_label(
                    word_text,
                    78,
                    28 * font_factor,
                    truncate=False,
                    numeric=True,
                    color=(255, 221, 73),
                    contour=not self_row,
                    padding=7,
                )
                word_ink = word_tile.info.get(
                    "hud_ink_bounds", (0, 0, word_tile.width, word_tile.height)
                )
                paste_text(
                    word_tile,
                    badge_right - word_ink[2],
                    cy,
                )
                word_icon_x = badge_right - word_tile.width - 35
                paste(self._equipment_word_badge, word_icon_x, cy - 15)

                equipment_text, equipment_is_pvp = equipment_type_badge(pvp_count)
                equipment_color = (
                    (255, 145, 161) if equipment_is_pvp else (105, 202, 255)
                )
                pvp_tile = self._fitted_label(
                    equipment_text,
                    150,
                    27 * font_factor,
                    truncate=False,
                    regular=True,
                    color=equipment_color,
                    contour=not self_row,
                    padding=7,
                )
                pvp_right = word_icon_x - 18
                pvp_ink = pvp_tile.info.get(
                    "hud_ink_bounds", (0, 0, pvp_tile.width, pvp_tile.height)
                )
                pvp_badge_width = min(
                    158, max(104, pvp_ink[2] - pvp_ink[0] + 24)
                )
                pvp_badge_height = 43
                pvp_badge_left = pvp_right - pvp_badge_width
                paste(
                    self._pill(
                        pvp_badge_width,
                        pvp_badge_height,
                        accent=(221, 61, 99)
                        if equipment_is_pvp
                        else (45, 151, 226),
                        red=equipment_is_pvp,
                        blue=not equipment_is_pvp,
                    ),
                    pvp_badge_left,
                    cy - pvp_badge_height / 2,
                )
                pvp_text_left = (
                    pvp_badge_left
                    + (pvp_badge_width - (pvp_ink[2] - pvp_ink[0])) / 2
                    - pvp_ink[0]
                )
                paste_text(pvp_tile, pvp_text_left, cy)
            if row_rating:
                pass
            else:
                metric = str(row.get("metric", "dps"))
                color = {"hps": (0, 247, 199), "dt": (255, 221, 93)}.get(metric, (249, 250, 253))
                rate = str(row.get("stat_text", "--") or "--")
                label(rate, stat_right, cy, rate_height / font_factor, anchor="right", max_width=223 if totals else 381, numeric=True, color=color, contour=not self_row, padding=13)
                if totals and not row.get("hide_total"):
                    text = str(row.get("total_text", "--") or "--")
                    total = self._fitted_label(text, 150, 30 * font_factor, truncate=False, regular=True, color=(231, 236, 244), stroke_radius=2)
                    # No capsule/background behind total damage/healing/taken.
                    paste_text(total, attached_total_left(stat_right), cy)
            if deaths and not row.get("hide_deaths"):
                if not self_row:
                    paste(self._skull, 924, cy - 32)
                else:
                    # Only the white skull is visible on the blue self row.
                    skull = self._skull.crop((13, 13, 54, 57))
                    r, g, b, _a = skull.split()
                    coverage = ImageChops.darker(ImageChops.darker(r, g), b).point(lambda n: max(0, min(255, (n - 60) * 2)))
                    skull.putalpha(coverage)
                    paste(skull, 937, cy - 19)
                death_value = str(max(0, int(row.get("deaths", 0) or 0)))
                death_tile = self._label(death_value, 36 * font_factor, numeric=True)
                if not self_row:
                    paste(self._pill(max(48, death_tile.width + 3), 61), 1019 - max(48, death_tile.width + 3) / 2, cy - 31)
                paste_text(death_tile, 1019 - death_tile.width / 2, cy)
            if bool(row.get("interactive", True)) and int(row.get("actor_id", 0) or 0):
                actors.append(((146, cy - 38, 1130 - width_removed, cy + 37), int(row.get("actor_id", 0))))

        if not shown_rows:
            label("等待队伍资料" if rating else "等待战斗数据", 626 - width_removed / 2, 319 + boss_extra, 31, anchor="center", color=(183, 200, 216), contour=True)

        if len(rows) > count:
            # A narrow thumb communicates that only the player list scrolls.
            top = ROW_CENTERS[0] + boss_extra - 38
            track_height = count * 75.4
            thumb_height = max(12, track_height * count / len(rows))
            thumb_top = top + (track_height - thumb_height) * start / (len(rows) - count)
            x = 1120 - width_removed - 146
            draw = ImageDraw.Draw(frame)
            draw.line((x, top - 126 - top_removed, x, top + track_height - 126 - top_removed), fill=(130, 155, 180, 50), width=3)
            draw.line((x, thumb_top - 126 - top_removed, x, thumb_top + thumb_height - 126 - top_removed), fill=(155, 190, 218, 190), width=5)
            regions["scrollbar:rows"] = (
                1104 - width_removed,
                top,
                1130 - width_removed,
                top + track_height,
            )

        # The entire list viewport accepts the mouse wheel.  Synthetic rows
        # such as the accordion heading and waiting message are deliberately
        # not actor hit regions, so exposing a dedicated viewport region keeps
        # scrolling available when the pointer is over those rows as well.
        if len(rows) > count:
            list_top = ROW_CENTERS[0] + boss_extra - 38
            regions["scroll:rows"] = (
                146,
                list_top,
                1130 - width_removed,
                list_top + count * 75.4,
            )

        # Team applications are transient notices, not list data.  Paint them
        # last over the DPS/rating viewport so the underlying rows, scroll
        # position and configured viewport height never change.  The notice
        # queue itself is unbounded; only the physical viewport clips it.
        if not interaction_blocked:
            # Row 0 is the local player and must remain readable. Application
            # notices begin in the DPS/rating area below it; the queue remains
            # unbounded and only the available viewport rows are clipped.
            for index, row in enumerate(application_rows[: max(0, count - 1)]):
                row_index = index + 1
                cy = (
                    ROW_CENTERS[row_index]
                    if row_index < len(ROW_CENTERS)
                    else ROW_CENTERS[-1] + (row_index - 9) * 75.4
                ) + boss_extra
                draw = ImageDraw.Draw(frame)
                left, right = 203, 1082 - width_removed
                top, bottom = cy - 30, cy + 30
                draw.rounded_rectangle(
                    (
                        round(left - 146),
                        round(top - 126 - top_removed),
                        round(right - 146),
                        round(bottom - 126 - top_removed),
                    ),
                    radius=12,
                    fill=(18, 151, 80, 255),
                    outline=(102, 255, 166, 255),
                    width=2,
                )
                label(
                    str(row.get("application_text") or "入队申请"),
                    (left + right) / 2,
                    cy,
                    27,
                    anchor="center",
                    max_width=right - left - 32,
                    regular=True,
                    color=(247, 255, 250),
                    contour=True,
                    truncate=False,
                )

        fy = 1039 - footer_offset + boss_extra + update_status_height
        if update_status_text:
            left, right = 169, 1097 - width_removed
            top = fy - update_status_height + 4
            bottom = top + 58
            update_action = str(snapshot.get("update_status_action", "") or "")
            draw = ImageDraw.Draw(frame)
            draw.rounded_rectangle(
                (left - 146, top - 126 - top_removed,
                 right - 146, bottom - 126 - top_removed),
                radius=12, fill=(13, 28, 42, 245),
                outline=(65, 137, 180, 220), width=2,
            )
            label(
                update_status_text, left + 16, (top + bottom) / 2,
                28, max_width=right - left - 32, regular=True,
                color=(120, 216, 255) if update_action else (214, 234, 247),
            )
            if update_action == "download_update" and not interaction_blocked:
                regions["action:download_update"] = (left, top, right, bottom)
        if bool(snapshot.get("show_team_dps", True)):
            team_width = 434 - max(0, width_removed - 65)
            team_width = min(team_width, min(box[0] for box in FOOTER_ACTION_BOXES.values()) - width_removed - 248 - 10)
            team_strip = self._strip_skin("team", team_width)
            team_strip = team_strip.resize(
                (team_strip.width, team_strip.height + 6),
                Image.Resampling.LANCZOS,
            )
            paste(team_strip, 248, fy - 3)
            team_avatar = self._avatar.resize(
                TEAM_SUMMARY_AVATAR_SIZE,
                Image.Resampling.LANCZOS,
            )
            paste(team_avatar, 169, fy - 13)
            caption_text = str(
                snapshot.get("dps_summary_caption", "全队秒伤") or "全队秒伤"
            )
            value = str(snapshot.get("team_dps", "0") or "0")
            caption, value_tile = self._footer_summary_pair(
                caption_text, value,
                248 + team_width - 275 - 9, font_factor,
            )
            paste_text(caption, 275, fy + 43)
            paste_text(value_tile, 275 + caption.width + 4, fy + 43)
        hover = str(snapshot.get("hover_action", ""))
        for name, box in FOOTER_ACTION_BOXES.items():
            tile = self._actions[name]
            if name == 'pin':
                tile = self._pin_on if bool(snapshot.get('topmost', False)) else self._pin_off
            if name == "lock" and not bool(snapshot.get("locked", False)):
                tile = self._unlocked
            disabled = bool(
                (interaction_blocked and name != "settings")
                or (
                    name == 'settings'
                    and snapshot.get('settings_disabled', False)
                )
            )
            if disabled:
                tile = tile.copy()
                tile.putalpha(tile.getchannel('A').point(lambda n: n // 3))
            if name == hover and not disabled:
                tile = tile.copy()
                glow = Image.new("RGBA", tile.size, (178, 218, 255, 0))
                glow.putalpha(tile.getchannel("A").point(lambda n: n // 12))
                tile.alpha_composite(glow)
            tile = tile.resize((box[2] - box[0], box[3] - box[1]), Image.Resampling.LANCZOS)
            paste(tile, box[0] - width_removed, box[1] - footer_offset + boss_extra + update_status_height)
            if not disabled:
                regions["action:" + name] = (box[0] - width_removed, box[1] - footer_offset + boss_extra + update_status_height, box[2] - width_removed, box[3] - footer_offset + boss_extra + update_status_height)

        if not bool(snapshot.get("locked", False)):
            right, bottom = 1128 - width_removed, 1128 - footer_offset + boss_extra + update_status_height
            regions['resize:height'] = (right - 30, bottom - 30, right, bottom)
            draw = ImageDraw.Draw(frame)
            for span in (10, 20):
                draw.line((right - span - 146, bottom - 5 - 126 - top_removed,
                           right - 5 - 146, bottom - span - 126 - top_removed),
                          fill=(145, 174, 201, 180), width=2)

        # Window movement is intentionally limited to the narrow header band.
        # Older builds treated every non-action pixel (player rows, the Boss
        # bar and empty history space included) as a drag handle, so a tiny
        # mouse movement while clicking ordinary data could move the whole HUD.
        # Add this region last so prediction/action hit targets keep priority
        # when they overlap the header visually.
        regions["drag:window"] = (
            146,
            126 + top_removed,
            1130 - width_removed,
            191 + top_removed,
        )

        if interaction_blocked:
            # Settings remain available while connecting. Keep every other
            # actor/action/drag/resize target blocked until capture is ready.
            regions = {
                key: box for key, box in regions.items() if key == "action:settings"
            }
            actors.clear()

        scale = REFERENCE_SCALE * pixel_scale
        # Preserve exact aspect and one common scale for every source-art box.
        image = frame.resize((round(source_width * scale), round(source_height * scale)), Image.Resampling.LANCZOS)

        def physical(box):
            return (round((box[0] - 146) * scale), round((box[1] - 126 - top_removed) * scale), round((box[2] - 146) * scale), round((box[3] - 126 - top_removed) * scale))

        return HudRenderResult(image, {key: physical(box) for key, box in regions.items()}, tuple((physical(box), actor) for box, actor in actors), max(1, round(75.4 * scale)), count)
