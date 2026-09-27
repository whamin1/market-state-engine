"""Count independent component categories, not activity bonuses or subparts."""
import math
from numbers import Real


ENTRY_COMPONENTS = ('price_position', 'body', 'volume', 'trend_continuity', 'range', 'liquidation')


def entry_evidence(result, side, minimum):
    components = result.get('score_components') or {}
    positive = []
    for name in ENTRY_COMPONENTS:
        item = components.get(name) if isinstance(components, dict) else None
        value = item.get(side.lower() + '_score') if isinstance(item, dict) else None
        if isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
            positive.append(name)
    return {'count': len(positive), 'minimum': minimum, 'components': positive,
            'allowed': len(positive) >= minimum}
