package com.happyhome.cc8assistant;

/** Pure bounded selection from actual Camera1 sizes; no claim of measured image quality. */
final class PreviewSizes {
  static int[] select(int[][] supported, boolean preferHD, int maxEdge, int maxPixels) {
    if (supported == null || supported.length > 256) return null;
    int[] best = null;
    long bestArea = 0;
    int edge = preferHD ? maxEdge : Math.min(640, maxEdge);
    long pixels = preferHD ? maxPixels : Math.min(640 * 480, maxPixels);
    for (int[] size : supported) {
      if (size == null || size.length != 2) continue;
      int w = size[0], h = size[1];
      long area = (long) w * h;
      if (w < 1 || h < 1 || (w & 1) != 0 || (h & 1) != 0 || Math.max(w, h) > edge || area > pixels)
        continue;
      if (area > bestArea || (area == bestArea && best != null && w > best[0])) {
        best = new int[] {w, h};
        bestArea = area;
      }
    }
    return best;
  }
}
