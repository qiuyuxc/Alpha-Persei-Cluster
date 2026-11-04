import io.github.qiuyuxc.agdns.GmGeometry;

public final class GmGeometryTest {
    private static int cases;
    private static void eq(float actual, float expected) {
        if (Float.isNaN(actual) || Math.abs(actual - expected) > .001f) throw new AssertionError(actual + " != " + expected);
        cases++;
    }
    public static void main(String[] args) {
        eq(GmGeometry.clamp(-500, 24, 1000), 24);
        eq(GmGeometry.clamp(5000, 24, 1000), 1000);
        eq(GmGeometry.clamp(400, 24, 1000), 400);
        eq(GmGeometry.clamp(Float.NaN, 24, 1000), 24);
        eq(GmGeometry.clamp(Float.POSITIVE_INFINITY, 24, 1000), 24);
        eq(GmGeometry.fraction(Float.NaN, .3f), .3f);
        eq(GmGeometry.fraction(Float.NEGATIVE_INFINITY, .3f), .3f);
        eq(GmGeometry.fraction(-.1f, .3f), 0);
        eq(GmGeometry.fraction(2, .3f), 1);
        eq(GmGeometry.restore(.5f, 24, 1024), 524);
        eq(GmGeometry.save(524, 24, 1024), .5f);
        eq(GmGeometry.restore(.5f, 16, 416), 216);
        eq(GmGeometry.save(216, 16, 416), .5f);
        eq(GmGeometry.save(24, 24, 24), 0);
        eq(GmGeometry.restore(.8f, 24, 24), 24);
        eq(GmGeometry.windowOffset(124, 100, false), 24);
        eq(GmGeometry.windowOffset(124, 100, true), 124);
        eq(GmGeometry.windowOffset(24, 0, false), 24);
        eq(GmGeometry.windowOffset(524, 24, false), 500);
        eq(GmGeometry.windowOffset(524, 24, true), 524);
        eq(GmGeometry.panelWidth(2400,3),2064);
        eq(GmGeometry.panelHeight(1080,3),864);
        eq(GmGeometry.statusCardWidth(2064,3),354);
        eq(GmGeometry.statusCardWidth(960,3),0);
        eq(GmGeometry.statusCardWidth(600,1),118);
        eq(GmGeometry.statusCardWidth(600,Float.NaN),0);
        int[][] screens = {{1080,2400},{2400,1080},{2560,1600},{360,640},{320,220},{64,64},{1,1}};
        for (int[] size : screens) for (float density : new float[]{1,2.75f,3,Float.NaN,0}) {
            int w = GmGeometry.panelWidth(size[0],density), h = GmGeometry.panelHeight(size[1],density);
            if (w < 1 || w > size[0] || h < 1 || h > size[1]) throw new AssertionError("Panel outside display");
            cases++;
        }
        System.out.println("GM window geometry: " + cases + " cases passed");
    }
}
