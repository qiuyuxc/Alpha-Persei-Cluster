import io.github.qiuyuxc.agconnect.TcpSockets;
import java.io.IOException;
import java.net.Socket;
import java.util.concurrent.atomic.AtomicReference;

public final class TcpSocketsTest {
    public static void main(String[] args) throws Exception {
        AtomicReference<Socket> seen = new AtomicReference<>();
        try (Socket socket = TcpSockets.open(candidate -> {
            seen.set(candidate);
            TestSupport.check(!candidate.isConnected(), "protection runs before TCP connect");
            TestSupport.check(candidate.getTcpNoDelay(), "socket preparation already completed");
            return true;
        })) {
            TestSupport.check(socket == seen.get(), "protected socket is returned to the caller");
        }
        TestSupport.check(seen.get().isClosed(), "caller can close the protected socket");
        try {
            TcpSockets.open(candidate -> { seen.set(candidate); return false; });
            throw new AssertionError("Rejected protection returned a socket");
        } catch (IOException expected) {
            TestSupport.check(expected.getMessage().contains("protection failed"), "protection rejection is reported");
            TestSupport.check(seen.get().isClosed(), "protection rejection releases the descriptor");
        }
        try {
            TcpSockets.open(candidate -> { seen.set(candidate); throw new IOException("injected protection error"); });
            throw new AssertionError("Protection exception returned a socket");
        } catch (IOException expected) {
            TestSupport.check(seen.get().isClosed(), "protection IO failure releases the descriptor");
        }
        try {
            TcpSockets.open(candidate -> { seen.set(candidate); throw new IllegalStateException("injected runtime error"); });
            throw new AssertionError("Protection exception returned a socket");
        } catch (IllegalStateException expected) {
            TestSupport.check(seen.get().isClosed(), "protection runtime failure releases the descriptor");
        }
        TestSupport.done("TCP preparation, protection ordering and failure cleanup");
    }
}
