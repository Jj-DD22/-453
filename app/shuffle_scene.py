"""Find the same visible excerpt anywhere in two B-roll cuts."""

import cv2
import numpy as np


LANDMARK_PHASES = (.04, .28, .39, .81)


def scene_landmarks(images):
    """Describe the supplied phase images (144×256 RGB or grayscale)."""
    sift = cv2.SIFT_create(nfeatures=270)
    result = []
    for image in images:
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image
        # The lower overlay and usual subtitle band carry no source scene identity.
        crop = gray[5:round(len(gray) * .73), 3:-3]
        keypoints, descriptors = sift.detectAndCompute(crop, None)
        points = np.float32([k.pt for k in keypoints]).reshape(-1, 2)
        if descriptors is not None:
            # OpenCV's SIFT bins are integer-valued even in its float representation.
            descriptors = descriptors.astype(np.uint8)
        result.append((points, descriptors, crop.shape))
    return result


def _geometric_match(left, right):
    a, desc, (height, width) = left
    b, other, _ = right
    if min(len(a), len(b)) < 8:
        return 0
    pairs = cv2.BFMatcher(cv2.NORM_L2).knnMatch(desc, other, k=2)
    unique = {}
    for first, second in pairs:
        if first.distance < .76 * second.distance:
            previous = unique.get(first.trainIdx)
            if previous is None or first.distance < previous.distance:
                unique[first.trainIdx] = first
    if len(unique) < 8:
        return 0
    matches = unique.values()
    src = a[[m.queryIdx for m in matches]]
    dst = b[[m.trainIdx for m in matches]]
    transform, mask = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC,
                                                  ransacReprojThreshold=3, maxIters=350)
    if transform is None or not (.45 <= np.hypot(*transform[0, :2]) <= 2.25):
        return 0
    inliers = mask.ravel().astype(bool)
    total = int(inliers.sum())
    if total < 8:
        return 0
    # Text in one corner and a shared logo are poor evidence that the subject
    # and location have repeated. Require multiple areas on both images.
    for xy in (src[inliers], dst[inliers]):
        dx, dy = np.ptp(xy, axis=0) / (width, height)
        if dx < .16 or dy < .13 or dx * dy < .045:
            return 0
    return total


def same_landmark_scene(first, second):
    """Look for the same physical scene at different positions within two cuts."""
    evidence = []
    for i, left in enumerate(first):
        for j, right in enumerate(second):
            support = _geometric_match(left, right)
            if support >= 8:
                if any(i != a and j != b and max(support, count) >= 11
                       for a, b, count in evidence):
                    return True
                # Only one distinct sample exists in a very short final cut.
                if min(len(first), len(second)) == 1 and support >= 24:
                    return True
                evidence.append((i, j, support))
    return False


def scene_frames(rgb):
    """Describe each 36×64 frame at 6 fps, with both horizontal orientations."""
    rgb = np.asarray(rgb)
    result = np.empty((len(rgb), 2, 3), dtype=np.uint64)
    for i, image in enumerate(rgb):
        # Leave the bottom sticker and video borders out of the scene identity.
        upper = image[3:46, 2:-2]
        gray = cv2.cvtColor(upper, cv2.COLOR_RGB2GRAY)
        low = cv2.resize(gray, (24, 24), interpolation=cv2.INTER_AREA).astype(np.float32)
        edges = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
        colour = np.rint(upper.mean(axis=(0, 1))).astype(np.uint64)
        packed = (int(colour[0]) << 16) | (int(colour[1]) << 8) | int(colour[2])
        for direction in range(2):
            coeffs = cv2.dct(low)[:8, :8].ravel()[1:]
            bits = np.append(coeffs > np.median(coeffs), False)
            shape_hash = np.packbits(bits).view(np.uint64)[0]
            edge_hash = np.packbits(edges[:, :-1] > edges[:, 1:]).view(np.uint64)[0]
            result[i, direction] = (shape_hash, edge_hash, packed)
            low, edges = low[:, ::-1], edges[:, ::-1]
    return result


def scene_windows(signatures, starts, duration, sample_rate=6):
    """Take one time-bounded signature per start. Very short cuts use one sample."""
    if not len(signatures) or duration <= 0 or sample_rate <= 0:
        raise ValueError("打乱镜头没有可比较的画面。")
    starts = np.asarray(starts, dtype=float)
    first = np.ceil(starts * sample_rate - 1e-7).astype(np.int64)
    last = np.ceil((starts + duration) * sample_rate - 1e-7).astype(np.int64) - 1
    count = max(1, int(np.floor(duration * sample_rate + 1e-7)))
    indices = np.minimum(first[:, None] + np.arange(count), last[:, None])
    return signatures[np.clip(indices, 0, len(signatures) - 1)]


def _near_frames(cuts, ref):
    # Compare a consistent orientation through the entire run. Two complementary
    # grayscale hashes plus colour prevent a shared product or backdrop alone
    # from declaring a match.
    candidate = cuts[:, :, None, :, :]
    reference = ref[None, None, :, 0, None, :]
    near = np.bitwise_count(candidate[..., 0] ^ reference[..., 0]) <= 11
    near &= np.bitwise_count(candidate[..., 1] ^ reference[..., 1]) <= 14
    for shift in (0, 8, 16):
        a = ((candidate[..., 2] >> shift) & 255).astype(np.int16)
        b = ((reference[..., 2] >> shift) & 255).astype(np.int16)
        near &= np.abs(a - b) <= 24
    return near


def _matching_runs(near):
    count, a, b, _ = near.shape
    length = min(5, a, b)
    offsets = np.arange(length)
    # At 6 fps, five samples span about two thirds of a second. Permit one
    # intervening sample in either clip for a mildly retimed copy.
    patterns = [(offsets, offsets)]
    if length >= 3:
        for skip in range(1, length):
            stretched = offsets + (offsets >= skip)
            patterns.extend(((offsets, stretched), (stretched, offsets)))
    found = np.zeros(count, dtype=bool)
    for left, right in patterns:
        h, w = a - left[-1], b - right[-1]
        if h <= 0 or w <= 0:
            continue
        match = near[:, :h, :w].copy()
        for x, y in zip(left[1:], right[1:]):
            match &= near[:, x:x+h, y:y+w]
        found |= match.any(axis=(1, 2, 3))
        if found.all():
            break
    return found


def repeated_scenes(candidates, reference):
    """Return a mask of cuts sharing a visible run with this possibly shorter reference."""
    count, length = candidates.shape[:2]
    if length == 0 or len(reference) == 0:
        raise ValueError("打乱镜头没有可比较的画面。")
    result = np.zeros(count, dtype=bool)
    chunk = max(1, 100_000 // (length * len(reference)))
    for start in range(0, count, chunk):
        stop = min(start + chunk, count)
        result[start:stop] = _matching_runs(_near_frames(candidates[start:stop], reference))
    return result
