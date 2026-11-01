import io.github.qiuyuxc.agconnect.LocalForwarder;
import io.github.qiuyuxc.agconnect.TcpSockets;
import java.net.Socket;
import java.util.concurrent.atomic.AtomicLong;

/** A loopback endpoint for the native packet/TLS integration test. */
public final class LocalForwarderHarness {
    public static void main(String[] args) throws Exception {
        AtomicLong connections = new AtomicLong(), failures = new AtomicLong();
        try (LocalForwarder forwarder = new LocalForwarder(Integer.parseInt(args[0]), Integer.parseInt(args[1]),
                () -> TcpSockets.open(socket -> true), connections, failures)) {
            System.out.println("READY " + forwarder.port()); System.out.flush();
            System.in.read();
        }
        System.out.println("STOPPED connections=" + connections.get() + " failures=" + failures.get());
    }
}
