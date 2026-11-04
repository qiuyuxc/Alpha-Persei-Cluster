import io.github.qiuyuxc.agconnect.GmThemePolicy;

public final class GmThemePolicyTest {
    public static void main(String[] args) {
        int cases=0;
        for (boolean system : new boolean[]{false,true}) {
            if (GmThemePolicy.dark("light",system)) throw new AssertionError("Phone overrode day selection"); cases++;
            if (!GmThemePolicy.dark("dark",system)) throw new AssertionError("Phone overrode night selection"); cases++;
            if (GmThemePolicy.dark("system",system)!=system) throw new AssertionError("Follow-system failed"); cases++;
            if (GmThemePolicy.dark("bad",system)!=system) throw new AssertionError("Invalid mode fallback failed"); cases++;
            if (GmThemePolicy.dark(GmThemePolicy.toggle("system",system),system)==system) throw new AssertionError("Manual toggle failed"); cases++;
            if (!"dark".equals(GmThemePolicy.toggle("light",system))) throw new AssertionError("Day to night failed"); cases++;
            if (!"light".equals(GmThemePolicy.toggle("dark",system))) throw new AssertionError("Night to day failed"); cases++;
        }
        if (!"system".equals(GmThemePolicy.normalize(null))) throw new AssertionError("Missing preference failed"); cases++;
        System.out.println("GM manual theme: "+cases+" cases passed");
    }
}
