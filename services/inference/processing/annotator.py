"""Draw detections on a copy; never alter the original captured image."""

import cv2


def annotate(image, detections):
    output = image.copy()
    for detection in detections:
        x1, y1, x2, y2 = map(int, detection["bbox"])
        cv2.rectangle(output, (x1, y1), (x2, y2), (0, 220, 0), 2)
        label = f"{detection['class_name']} {detection['confidence']:.2f}"
        cv2.putText(
            output,
            label,
            (x1, max(14, y1 - 5)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 220, 0),
            1,
            cv2.LINE_AA,
        )
    return output
