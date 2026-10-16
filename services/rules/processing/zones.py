"""Original Edge ray casting, config order and full-frame/unzoned semantics."""


def point_in_polygon(x, y, polygon):
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def assign_zone(event, zones):
    if not zones:
        return "full_frame"
    x1, y1, x2, y2 = event["bbox"]
    x = (x1 + x2) / 2
    # Original enrichment assigned zones BEFORE rounding its displayed anchors.
    y = (
        (y1 + y2) / 2
        if event["class_name"] != "person" or y2 >= event["frame_h"] - 1
        else y2
    )
    for zone in zones:
        if point_in_polygon(x, y, zone.polygon):
            return zone.name
    return "unzoned"
