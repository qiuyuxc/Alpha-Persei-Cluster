package io.github.qiuyuxc.agdns;

/** Screen geometry independent of Android Views and their render transforms. */
public final class GmGeometry {
    private GmGeometry() { }
    public static float clamp(float value, float minimum, float maximum) {
        if (Float.isNaN(value) || Float.isInfinite(value)) return minimum;
        return Math.max(minimum, Math.min(Math.max(minimum, maximum), value));
    }
    public static float fraction(float value, float fallback) {
        return Float.isNaN(value) || Float.isInfinite(value) ? fallback : clamp(value, 0, 1);
    }
    public static float restore(float fraction, float minimum, float maximum) {
        return minimum + fraction(fraction, 0) * Math.max(0, maximum - minimum);
    }
    public static float save(float value, float minimum, float maximum) {
        return maximum > minimum ? clamp((value - minimum) / (maximum - minimum), 0, 1) : 0;
    }
    public static int windowOffset(float screenCoordinate, int parentOrigin, boolean absoluteWindow) {
        return Math.round(screenCoordinate) - (absoluteWindow ? 0 : parentOrigin);
    }
    public static int panelWidth(int width, float density) {
        return panelSize(width, density, 720, .86f);
    }
    public static int panelHeight(int height, float density) {
        return panelSize(height, density, 300, .80f);
    }
    public static int statusCardWidth(int panelWidth, float density) {
        if (density <= 0 || Float.isInfinite(density) || Float.isNaN(density)) return 0;
        return panelWidth / density >= 480 ? Math.round(118 * density) : 0;
    }
    private static int panelSize(int available, float density, int cap, float ratio) {
        int space = Math.max(1, available);
        float scale = density > 0 && !Float.isInfinite(density) ? density : 1;
        int inset = Math.min(Math.round(12 * scale), Math.max(0, (space - 1) / 2));
        return Math.max(1, Math.min(space - inset * 2,
                Math.min(Math.round(cap * scale), Math.round(space * ratio))));
    }
}
