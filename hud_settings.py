"""Compact settings view painted into the existing HUD window."""

from __future__ import annotations

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont


SETTINGS_PAGES = ("显示", "外观", "快捷键")
DISPLAY_SETTINGS = (
    ("names", "显示玩家名称"),
    ("rating", "队伍非凡评分预览"),
    ("deaths", "显示死亡次数"),
    ("totals", "显示对应总量"),
    ("team_dps", "显示全队秒伤"),
    ("highlight", "本人高亮"),
    ("time", "显示战斗时间"),
    ("boss_hp", "显示 Boss 血条"),
    ("enrage", "Boss 狂暴节奏预测"),
    ("upload", "自动上传战斗记录"),
)
APPEARANCE_SETTINGS = (
    ("audience", "观众显示指标"),
    ("warrior", "战士显示指标"),
    ("font", "字体大小"),
    ("scale", "HUD 缩放"),
    ("mask", "玩家行遮罩"),
)
HOTKEY_SETTINGS = (
    ("visibility", "隐藏 / 显示"),
    ("view", "团队 / 战斗页面"),
    ("unlock", "锁定后仅快捷键解锁"),
    ("lock_toggle", "同一快捷键锁定 / 解锁"),
)

SETTINGS_LOGICAL_WIDTH = 306
SETTINGS_PAGE_HEIGHTS = (294, 320, 376)


def _mixed_color(first: tuple[int, int, int], second: tuple[int, int, int], ratio: float):
    ratio = max(0.0, min(1.0, ratio))
    return tuple(round(a + (b - a) * ratio) for a, b in zip(first, second))


def settings_panel_position(
    anchor_bounds: tuple[int, int, int, int],
    panel_size: tuple[int, int],
    work_area: tuple[int, int, int, int],
    *,
    margin: int = 12,
) -> tuple[int, int]:
    """Put settings beside the live HUD with their top edges aligned."""
    anchor_x, anchor_y, anchor_width, anchor_height = (
        int(value) for value in anchor_bounds
    )
    panel_width, panel_height = (max(1, int(value)) for value in panel_size)
    left, top, right, bottom = (int(value) for value in work_area)
    inset = max(0, int(margin))
    anchor_center_x = anchor_x + max(1, anchor_width) / 2
    work_center_x = (left + right) / 2
    minimum_x = left + inset
    minimum_y = top + inset
    maximum_x = max(left + inset, right - inset - panel_width)
    maximum_y = max(top + inset, bottom - inset - panel_height)
    left_x = anchor_x - inset - panel_width
    right_x = anchor_x + max(1, anchor_width) + inset
    left_fits = left_x >= minimum_x
    right_fits = right_x <= maximum_x
    prefer_left = anchor_center_x >= work_center_x
    if prefer_left and (left_fits or not right_fits):
        x = left_x
    elif not prefer_left and (right_fits or not left_fits):
        x = right_x
    elif left_fits:
        x = left_x
    else:
        x = right_x
    y = anchor_y
    return (
        min(maximum_x, max(minimum_x, x)),
        min(maximum_y, max(minimum_y, y)),
    )


def render_hud_settings(values: dict, page: int, font_path: str, scale: float = 1.0):
    """Return a HUD-skinned settings image and clickable regions."""
    page = max(0, min(len(SETTINGS_PAGES) - 1, int(page)))
    scale = max(0.75, min(4.0, float(scale)))
    logical_height = SETTINGS_PAGE_HEIGHTS[page]
    image = Image.new(
        "RGBA",
        (round(SETTINGS_LOGICAL_WIDTH * scale), round(logical_height * scale)),
        (0, 0, 0, 0),
    )
    draw = ImageDraw.Draw(image)
    regions: dict[str, tuple[int, int, int, int]] = {}
    fonts: dict[int, ImageFont.FreeTypeFont] = {}

    def box(x0, y0, x1, y1):
        return tuple(round(value * scale) for value in (x0, y0, x1, y1))

    def font(size):
        pixel_size = max(9, round(size * scale))
        if pixel_size not in fonts:
            fonts[pixel_size] = ImageFont.truetype(font_path, pixel_size)
        return fonts[pixel_size]

    def text(
        x,
        y,
        value,
        *,
        size=13,
        color=(226, 239, 251, 255),
        anchor="lm",
        contour=False,
    ):
        draw.text(
            (round(x * scale), round(y * scale)),
            str(value),
            fill=color,
            font=font(size),
            anchor=anchor,
            stroke_width=max(1, round(0.65 * scale)) if contour else 0,
            stroke_fill=(0, 4, 10, 235),
        )

    def gradient_round_rect(
        bounds,
        *,
        radius,
        top,
        bottom,
        alpha=255,
        outline=None,
        width=1,
    ):
        x0, y0, x1, y1 = box(*bounds)
        local_width = max(1, x1 - x0 + 1)
        local_height = max(1, y1 - y0 + 1)
        surface = Image.new("RGBA", (local_width, local_height))
        surface_draw = ImageDraw.Draw(surface)
        for row in range(local_height):
            ratio = row / max(1, local_height - 1)
            surface_draw.line(
                (0, row, local_width, row),
                fill=_mixed_color(top, bottom, ratio) + (alpha,),
            )
        mask = Image.new("L", surface.size)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, local_width - 1, local_height - 1),
            radius=max(1, round(radius * scale)),
            fill=alpha,
        )
        surface.putalpha(mask)
        image.alpha_composite(surface, (x0, y0))
        if outline is not None:
            draw.rounded_rectangle(
                (x0, y0, x1, y1),
                radius=max(1, round(radius * scale)),
                outline=outline,
                width=max(1, round(width * scale)),
            )

    def button(
        action,
        bounds,
        label,
        *,
        active=False,
        muted=False,
        danger=False,
        size=11,
    ):
        if danger:
            top, bottom = (64, 13, 31), (25, 5, 17)
            outline = (209, 49, 87, 225)
            foreground = (255, 218, 226, 255)
            surface_alpha = 225
        elif active:
            top, bottom = (18, 108, 169), (6, 45, 83)
            outline = (79, 201, 255, 235)
            foreground = (240, 252, 255, 255)
            surface_alpha = 215
        elif muted:
            top, bottom = (22, 31, 44), (8, 14, 24)
            outline = (71, 101, 126, 145)
            foreground = (126, 150, 174, 255)
            surface_alpha = 150
        else:
            top, bottom = (30, 43, 61), (7, 13, 23)
            outline = (87, 126, 158, 175)
            foreground = (207, 224, 240, 255)
            surface_alpha = 170
        gradient_round_rect(
            bounds,
            radius=min(9, (bounds[3] - bounds[1]) / 2),
            top=top,
            bottom=bottom,
            alpha=surface_alpha,
            outline=outline,
        )
        text(
            (bounds[0] + bounds[2]) / 2,
            (bounds[1] + bounds[3]) / 2,
            label,
            size=size,
            color=foreground,
            anchor="mm",
            contour=active or danger,
        )
        regions[f"action:{action}"] = box(*bounds)

    panel_bounds = (4, 4, SETTINGS_LOGICAL_WIDTH - 4, logical_height - 4)
    shadow_mask = Image.new("L", image.size)
    ImageDraw.Draw(shadow_mask).rounded_rectangle(
        box(*panel_bounds),
        radius=round(17 * scale),
        fill=138,
    )
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow.putalpha(
        ImageChops.subtract(
            shadow_mask.filter(ImageFilter.GaussianBlur(max(1, 4 * scale))),
            shadow_mask,
        )
    )
    image.alpha_composite(shadow)
    gradient_round_rect(
        panel_bounds,
        radius=17,
        top=(15, 34, 53),
        bottom=(2, 8, 17),
        alpha=208,
        outline=(59, 161, 215, 225),
        width=1.2,
    )
    draw.rounded_rectangle(
        box(7, 7, SETTINGS_LOGICAL_WIDTH - 7, logical_height - 7),
        radius=round(14 * scale),
        outline=(177, 229, 252, 82),
        width=max(1, round(scale)),
    )
    draw.line(
        box(25, 8, SETTINGS_LOGICAL_WIDTH - 25, 8),
        fill=(182, 232, 255, 72),
        width=max(1, round(scale)),
    )

    text(
        16,
        27,
        "设置",
        size=13,
        color=(239, 249, 255, 255),
        contour=True,
    )

    expiry = str(values.get("expiry") or "读取中").strip()
    for prefix in ("到期时间：", "到期时间:"):
        if expiry.startswith(prefix):
            expiry = expiry[len(prefix):].strip()
            break
    gradient_round_rect(
        (56, 11, 258, 42),
        radius=10,
        top=(22, 50, 72),
        bottom=(4, 17, 30),
        alpha=168,
        outline=(72, 155, 196, 165),
    )
    draw.ellipse(
        box(63, 19, 77, 33),
        fill=(8, 49, 75, 215),
        outline=(82, 196, 244, 235),
        width=max(1, round(scale)),
    )
    draw.line(
        box(70, 22, 70, 26),
        fill=(202, 239, 255, 255),
        width=max(1, round(scale)),
    )
    draw.line(
        box(70, 26, 73, 28),
        fill=(202, 239, 255, 255),
        width=max(1, round(scale)),
    )
    text(84, 26.5, "有效期", size=8, color=(119, 153, 181, 255))
    draw.line(
        box(111, 20, 111, 32),
        fill=(65, 103, 130, 190),
        width=max(1, round(scale)),
    )
    text(
        118,
        26.5,
        expiry,
        size=8,
        color=(224, 240, 251, 255),
    )

    close_hovered = values.get("hover_action") == "settings_close"
    close_bounds = (264, 10, 296, 43)
    if close_hovered:
        close_top, close_bottom = (245, 61, 96), (117, 10, 47)
        close_outline = (255, 151, 170, 255)
        close_foreground = (255, 255, 255, 255)
        glow_alpha = 145
    else:
        close_top, close_bottom = (186, 38, 73), (72, 7, 35)
        close_outline = (255, 94, 127, 250)
        close_foreground = (255, 246, 249, 255)
        glow_alpha = 88
    close_glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(close_glow).rounded_rectangle(
        box(*close_bounds),
        radius=max(1, round(10 * scale)),
        fill=(255, 36, 83, glow_alpha),
    )
    image.alpha_composite(
        close_glow.filter(ImageFilter.GaussianBlur(max(1, 4 * scale)))
    )
    gradient_round_rect(
        close_bounds,
        radius=10,
        top=close_top,
        bottom=close_bottom,
        alpha=244,
        outline=close_outline,
        width=1.35,
    )
    draw.rounded_rectangle(
        box(267, 13, 293, 40),
        radius=max(1, round(8 * scale)),
        outline=(255, 255, 255, 58),
        width=max(1, round(scale)),
    )
    cross_width = max(2, round(2.2 * scale))
    draw.line(box(274, 20, 286, 33), fill=(62, 4, 20, 190), width=cross_width + 1)
    draw.line(box(286, 20, 274, 33), fill=(62, 4, 20, 190), width=cross_width + 1)
    draw.line(box(274, 20, 286, 33), fill=close_foreground, width=cross_width)
    draw.line(box(286, 20, 274, 33), fill=close_foreground, width=cross_width)
    regions["action:settings_close"] = box(*close_bounds)

    for index, label in enumerate(SETTINGS_PAGES):
        left = 12 + index * 95
        bounds = (left, 51, left + 89, 81)
        selected = index == page
        gradient_round_rect(
            bounds,
            radius=9,
            top=(18, 78, 117) if selected else (18, 29, 43),
            bottom=(5, 35, 65) if selected else (5, 11, 20),
            alpha=205 if selected else 122,
            outline=(91, 207, 250, 225) if selected else (72, 105, 132, 125),
        )
        if selected:
            draw.line(
                box(left + 14, 77, left + 75, 77),
                fill=(106, 215, 255, 255),
                width=max(2, round(2 * scale)),
            )
        text(
            left + 44.5,
            66,
            label,
            size=12,
            color=(235, 249, 255, 255) if selected else (145, 169, 191, 255),
            anchor="mm",
            contour=selected,
        )
        regions[f"action:settings_page:{index}"] = box(*bounds)

    def row_base(top, label, *, height=35, label_y=None):
        gradient_round_rect(
            (12, top, 294, top + height),
            radius=8,
            top=(20, 35, 51),
            bottom=(6, 14, 25),
            alpha=142,
            outline=(81, 126, 158, 135),
        )
        center_y = top + height / 2 if label_y is None else label_y
        draw.ellipse(
            box(20, center_y - 2, 24, center_y + 2),
            fill=(61, 183, 239, 235),
        )
        text(31, center_y, label, size=12, color=(224, 237, 249, 255))

    if page == 0:
        for index, (key, label) in enumerate(DISPLAY_SETTINGS):
            column = index % 2
            row = index // 2
            left = 12 + column * 144
            top = 90 + row * 39
            gradient_round_rect(
                (left, top, left + 138, top + 35),
                radius=8,
                top=(20, 35, 51),
                bottom=(6, 14, 25),
                alpha=142,
                outline=(81, 126, 158, 135),
            )
            draw.ellipse(
                box(left + 7, top + 15.5, left + 11, top + 19.5),
                fill=(61, 183, 239, 235),
            )
            text(
                left + 15,
                top + 17.5,
                label,
                size=9,
                color=(224, 237, 249, 255),
            )
            enabled = bool(values.get(key))
            button(
                f"settings_toggle:{key}",
                (left + 108, top + 5, left + 130, top + 30),
                "开" if enabled else "关",
                active=enabled,
                muted=not enabled,
                size=9,
            )
    elif page == 1:
        for index, (key, label) in enumerate(APPEARANCE_SETTINGS):
            top = 90 + index * 40
            row_base(top, label, height=36)
            button(f"settings_step:{key}:-1", (178, top + 4, 207, top + 32), "−", size=13)
            gradient_round_rect(
                (211, top + 4, 251, top + 32),
                radius=8,
                top=(7, 35, 58),
                bottom=(3, 17, 31),
                alpha=174,
                outline=(65, 157, 202, 175),
            )
            text(
                231,
                top + 18,
                str(values.get(key, "")),
                size=11,
                color=(109, 215, 255, 255),
                anchor="mm",
                contour=True,
            )
            button(f"settings_step:{key}:1", (255, top + 4, 284, top + 32), "+", size=13)
        text(
            16,
            301,
            "职业指标：观众 HPS / DPS · 战士 DPS / DT",
            size=9,
            color=(126, 151, 175, 255),
        )
    else:
        for index, (key, label) in enumerate(HOTKEY_SETTINGS):
            top = 90 + index * 68
            height = 64 if key != "lock_toggle" else 38
            row_base(top, label, height=height, label_y=top + 17.5)
            enabled = bool(values.get(f"{key}_enabled"))
            button(
                f"settings_toggle:{key}",
                (233, top + 5, 284, top + 30),
                "开启" if enabled else "关闭",
                active=enabled,
                muted=not enabled,
                size=10,
            )
            if key != "lock_toggle":
                capturing = values.get("capturing") == key
                key_text = (
                    "请按快捷键…"
                    if capturing
                    else str(values.get(f"{key}_key") or "点击录入快捷键")
                )
                button(
                    f"settings_capture:{key}",
                    (22, top + 34, 224, top + 59),
                    key_text,
                    active=capturing,
                    size=10,
                )
                button(
                    f"settings_clear:{key}",
                    (230, top + 34, 284, top + 59),
                    "清除",
                    muted=True,
                    size=10,
                )
        status = str(values.get("status") or "Esc 取消录入；字母键请配合 Ctrl 或 Alt")
        gradient_round_rect(
            (12, 343, 294, 364),
            radius=8,
            top=(28, 30, 42) if values.get("status") else (12, 25, 38),
            bottom=(12, 12, 20) if values.get("status") else (4, 11, 20),
            alpha=150,
            outline=(195, 79, 99, 185) if values.get("status") else (63, 108, 139, 125),
        )
        text(
            153,
            353.5,
            status[:35],
            size=9,
            color=(255, 159, 172, 255) if values.get("status") else (131, 158, 181, 255),
            anchor="mm",
        )
    return image, regions
