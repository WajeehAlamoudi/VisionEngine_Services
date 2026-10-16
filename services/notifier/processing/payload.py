"""Match the original Edge webhook fields; no dependency on parent engine code."""


def build_payload(alert, device_id, branch_id):
    det = alert["detection"]
    return {
        "branch_id": branch_id,
        "device_id": device_id,
        "rule_name": alert["rule_name"],
        "severity": alert["severity"],
        "message": alert["message"],
        "track_id": det["track_id"],
        "class": det["class_name"],
        "confidence": round(det["confidence"], 4),
        "model_id": det["model_id"],
        "camera_id": det["camera_id"],
        "camera_name": det["camera_name"],
        "zone": det["zone"],
        "bbox_x1": int(det["bbox"][0]),
        "bbox_y1": int(det["bbox"][1]),
        "bbox_x2": int(det["bbox"][2]),
        "bbox_y2": int(det["bbox"][3]),
        "anchor_x": det["anchor_x"],
        "anchor_y": det["anchor_y"],
        "anchor_x_norm": det["anchor_x_norm"],
        "anchor_y_norm": det["anchor_y_norm"],
        "frame_w": det["frame_w"],
        "frame_h": det["frame_h"],
        "ts": det["capture_ts"],
        **({"attributes": det["attributes"]} if det["attributes"] else {}),
    }
