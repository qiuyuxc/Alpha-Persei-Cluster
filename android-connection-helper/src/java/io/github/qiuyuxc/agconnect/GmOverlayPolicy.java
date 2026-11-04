package io.github.qiuyuxc.agconnect;

/** The entry is optional and never controls the VPN connection. */
public final class GmOverlayPolicy {
    private GmOverlayPolicy() { }
    public static boolean available(boolean connected, boolean enabled, boolean granted, boolean locked) {
        return connected && enabled && granted && !locked;
    }
    public static boolean visible(boolean connected, boolean enabled, boolean granted,
                                  boolean panelOpen, boolean locked) {
        return available(connected, enabled, granted, locked) && !panelOpen;
    }
}
