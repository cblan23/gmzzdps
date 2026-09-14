"""Bounded presentation cache; never stores or drops combat input events."""
from copy import deepcopy


class HudRenderCache:
    def __init__(self):
        self.renderer = None
        self.snapshot = None
        self.options = None
        self.result = None
        self.hits = 0
        self.misses = 0

    def render(self, renderer, snapshot, *, pixel_scale, font_size):
        options = (pixel_scale, font_size)
        if (renderer is self.renderer and self.result is not None
                and options == self.options and snapshot == self.snapshot):
            self.hits += 1
            return self.result
        # Cache an owned copy: retained Boss/row dictionaries can mutate later.
        owned = deepcopy(snapshot)
        result = renderer.render(snapshot, pixel_scale=pixel_scale, font_size=font_size)
        self.renderer, self.snapshot, self.options, self.result = renderer, owned, options, result
        self.misses += 1
        return result


def hud_render_delay(now, previous, requested=0):
    """Coalesce bursts while keeping the latest snapshot at execution time."""
    remaining = max(0.0, 1.0 / 30 - (now - previous)) if previous else 0.0
    import math
    return max(0, int(requested), math.ceil(remaining * 1000))
