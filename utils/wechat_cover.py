"""Cover geometry shared by draft creation and explicit image-only repair.

The current draft API describes cover_info.crop_percent_list coordinates:
https://developers.weixin.qq.com/doc/service/api/draftbox/draftmanage/api_draft_update.html
Legacy pic_crop fields remain for compatible draft readbacks. A square preview
cannot be accepted merely because the reviewed 900x383 wide image is complete.
"""
import math


def approved_cover_geometry():
    return {
        'pic_crop_235_1': '0_0_1_1',
        'pic_crop_1_1': '0.287222_0_0.712778_1',
        'cover_info': {'crop_percent_list': [
            {'ratio': '2.35_1', 'x1': '0', 'y1': '0', 'x2': '1', 'y2': '1'},
            {'ratio': '1_1', 'x1': '0.287222', 'y1': '0', 'x2': '0.712778', 'y2': '1'},
        ]},
    }


def full_width_cover_verified(article):
    """Require readback evidence and reject contradictory modern/legacy crops."""
    crops = []
    legacy = article.get('pic_crop_235_1')
    if legacy is not None:
        crops.append(str(legacy).split('_'))
    info = article.get('cover_info')
    if info is not None:
        if not isinstance(info, dict) or not isinstance(info.get('crop_percent_list'), list):
            return False
        wide = [entry for entry in info['crop_percent_list']
                if isinstance(entry, dict) and entry.get('ratio') == '2.35_1']
        if len(wide) > 1:
            return False
        for entry in wide:
            crops.append([entry.get(key) for key in ('x1', 'y1', 'x2', 'y2')])
    try:
        return bool(crops) and all(len(crop) == 4 and all(
            math.isfinite(float(value)) and abs(float(value) - target) < 1e-6
            for value, target in zip(crop, (0, 0, 1, 1))) for crop in crops)
    except (TypeError, ValueError):
        return False
