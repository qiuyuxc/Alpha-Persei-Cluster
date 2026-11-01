package io.github.qiuyuxc.agconnect;

import java.io.IOException;
import java.net.Socket;

/** Android's VPN protection reads the descriptor without creating the socket. */
public final class TcpSockets {
    public interface Protector { boolean protect(Socket socket) throws IOException; }
    private TcpSockets() { }

    public static Socket open(Protector protector) throws IOException {
        Socket socket = new Socket();
        try {
            // Creates the OS descriptor while the socket is still unconnected.
            socket.setTcpNoDelay(true);
            if (!protector.protect(socket)) throw new IOException("VPN socket protection failed");
            return socket;
        } catch (IOException | RuntimeException error) {
            try { socket.close(); } catch (IOException ignored) { }
            throw error;
        }
    }
}
