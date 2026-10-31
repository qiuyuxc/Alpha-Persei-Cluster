package io.github.qiuyuxc.agconnect;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.ConnectivityManager;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.net.VpnService;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.ParcelFileDescriptor;
import android.os.SystemClock;
import android.system.ErrnoException;
import android.system.Os;
import android.system.OsConstants;
import android.system.StructPollfd;
import android.util.Log;
import java.io.Closeable;
import java.io.IOException;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Consumer;

/** DNS-only split route, scoped to the game UID. All game transport stays direct. */
public final class ConnectionService extends VpnService {
    public static final String GAME = "com.yongshi.tenojo.ys";
    public static final String START = "io.github.qiuyuxc.agconnect.START";
    public static final String STOP = "io.github.qiuyuxc.agconnect.STOP";
    private static final String CHANNEL = "connection";
    private static final int NOTIFICATION = 1;
    private static final String TAG = "AGConnect";

    public static final class Status {
        public final boolean running;
        public final String title, detail;
        Status(boolean running, String title, String detail) {
            this.running = running; this.title = title; this.detail = detail;
        }
    }
    public static volatile Status status = new Status(false, "未连接", "准备完成后，填写服务器地址并开启连接。");
    public static final AtomicLong redirected = new AtomicLong();
    public static final AtomicLong dnsFailures = new AtomicLong();
    private final Handler main = new Handler(Looper.getMainLooper());
    private ConnectivityManager connectivity;
    private boolean watching;
    private TunnelRunner runner;

    private final ConnectivityManager.NetworkCallback callback = new ConnectivityManager.NetworkCallback() {
        @Override public void onAvailable(Network network) { refresh(); }
        @Override public void onLost(Network network) { refresh(); }
        @Override public void onCapabilitiesChanged(Network network, NetworkCapabilities caps) { refresh(); }
        @Override public void onLinkPropertiesChanged(Network network, LinkProperties props) { refresh(); }
        private void refresh() { main.post(() -> refreshNetwork()); }
    };

    @Override public void onCreate() {
        super.onCreate();
        connectivity = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel channel = new NotificationChannel(CHANNEL, "进服连接", NotificationManager.IMPORTANCE_LOW);
            ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).createNotificationChannel(channel);
        }
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null || !START.equals(intent.getAction())) {
            stopConnection("未连接", "连接已停止。");
            return START_NOT_STICKY;
        }
        try {
            // Foreground promotion must precede network and TUN setup.
            foreground("正在准备连接");
            closeRunner();
            if (VpnService.prepare(this) != null)
                throw new IllegalStateException("请打开助手并允许系统 VPN 连接请求。");
            ServerAddress address = ServerAddress.parse(intent.getStringExtra("server"));
            getPackageManager().getApplicationInfo(GAME, 0);
            PhysicalNetwork physical = selectNetwork();
            if (physical == null)
                throw new IllegalStateException("当前没有可用网络。请连接 Wi-Fi 或移动网络后重试。");
            requireCompatibleDns(physical);
            Builder builder = new Builder().setSession("深空进服 · " + address.host)
                    .setMtu(1500).addAddress(Ipv4Packet.INTERFACE_ADDRESS, 32)
                    .addDnsServer(Ipv4Packet.DNS_ADDRESS).addRoute(Ipv4Packet.DNS_ADDRESS, 32)
                    .allowFamily(OsConstants.AF_INET6).setBlocking(false)
                    .setConfigureIntent(openApp()).setUnderlyingNetworks(new Network[]{physical.network});
            builder.addAllowedApplication(GAME);
            if (Build.VERSION.SDK_INT >= 29) builder.setMetered(false);
            ParcelFileDescriptor descriptor = builder.establish();
            if (descriptor == null) throw new IllegalStateException("系统未授予 VPN 权限，请重新开启连接。");
            redirected.set(0); dnsFailures.set(0);
            runner = new TunnelRunner(descriptor, address, physical);
            runner.start();
            if (!watching) {
                connectivity.registerNetworkCallback(new NetworkRequest.Builder()
                        .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                        .addCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN).build(), callback);
                watching = true;
            }
            connected(address);
        } catch (android.content.pm.PackageManager.NameNotFoundException error) {
            stopConnection("无法开启", "未找到深空之眼。请先安装兼容的用户 CA 版客户端。");
        } catch (Exception error) {
            Log.e(TAG, "VPN setup failed", error);
            stopConnection("无法开启", error instanceof IllegalArgumentException || error instanceof IllegalStateException
                    ? error.getMessage() : "系统无法建立连接，请检查 VPN 设置后重试。");
        }
        return START_NOT_STICKY;
    }

    private void connected(ServerAddress address) {
        status = new Status(true, "连接已开启", "服务器 " + address.host + "\n请完全退出游戏后重新打开；实际登录结果以游戏为准。");
        foreground("连接已开启 · " + address.host);
    }

    private PendingIntent openApp() {
        return PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    private void foreground(String message) {
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL) : new Notification.Builder(this);
        PendingIntent stop = PendingIntent.getService(this, 1, new Intent(this, ConnectionService.class).setAction(STOP),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification notification = builder.setSmallIcon(R.drawable.ic_connection)
                .setContentTitle("深空连接助手").setContentText(message)
                .setContentIntent(openApp()).setOngoing(true).setOnlyAlertOnce(true)
                .addAction(new Notification.Action.Builder(null, "停止连接", stop).build()).build();
        if (Build.VERSION.SDK_INT >= 34) startForeground(NOTIFICATION, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        else startForeground(NOTIFICATION, notification);
    }

    private static final class PhysicalNetwork {
        final Network network;
        final List<InetSocketAddress> dns;
        final boolean strictDns;
        PhysicalNetwork(Network network, LinkProperties props) {
            this.network = network;
            dns = new ArrayList<>();
            for (InetAddress address : props.getDnsServers()) {
                if (!address.getHostAddress().equals(Ipv4Packet.DNS_ADDRESS))
                    dns.add(new InetSocketAddress(address, 53));
                if (dns.size() == 3) break;
            }
            strictDns = Build.VERSION.SDK_INT >= 28 && props.getPrivateDnsServerName() != null;
        }
        boolean same(PhysicalNetwork other) {
            return other != null && network.equals(other.network) && dns.equals(other.dns) && strictDns == other.strictDns;
        }
    }

    private PhysicalNetwork selectNetwork() {
        Network active = connectivity.getActiveNetwork();
        PhysicalNetwork best = null;
        int bestScore = -1;
        for (Network network : connectivity.getAllNetworks()) {
            NetworkCapabilities caps = connectivity.getNetworkCapabilities(network);
            LinkProperties props = connectivity.getLinkProperties(network);
            if (caps == null || props == null || caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)
                    || !caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN)
                    || !caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)) continue;
            int score = (network.equals(active) ? 100 : 0)
                    + (caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED) ? 40 : 0)
                    + (caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) ? 20 : 0)
                    + (caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) ? 10 : 0);
            if (score > bestScore) { best = new PhysicalNetwork(network, props); bestScore = score; }
        }
        return best;
    }

    private void requireCompatibleDns(PhysicalNetwork physical) {
        if (physical.strictDns)
            throw new IllegalStateException("请先将系统“私人 DNS / 私有 DNS”改为“关闭”，再开启连接。指定私人 DNS 会绕过助手。");
        if (physical.dns.isEmpty())
            throw new IllegalStateException("当前网络未提供 DNS，请检查网络设置或更换网络。");
    }

    private void refreshNetwork() {
        if (runner == null) return;
        try {
            PhysicalNetwork next = selectNetwork();
            PhysicalNetwork previous = runner.physical;
            if (next != null) requireCompatibleDns(next);
            if (next == null && previous == null || next != null && next.same(previous)) return;
            runner.physical = next;
            runner.closeSockets();
            setUnderlyingNetworks(next == null ? new Network[0] : new Network[]{next.network});
            if (next == null) {
                status = new Status(true, "等待网络", "网络已断开，恢复后会继续连接。");
                foreground("等待 Wi-Fi 或移动网络恢复");
            } else connected(runner.address);
        } catch (Exception error) {
            stopConnection("连接已停止", error.getMessage());
        }
    }

    private void closeRunner() {
        TunnelRunner previous = runner;
        runner = null;
        if (previous != null) previous.close();
    }

    private void stopWatching() {
        if (watching) {
            watching = false;
            connectivity.unregisterNetworkCallback(callback);
        }
    }

    private void stopConnection(String title, String detail) {
        closeRunner();
        stopWatching();
        status = new Status(false, title, detail == null ? "请重新开启连接。" : detail);
        stopForeground(true);
        stopSelf();
    }

    @Override public void onRevoke() {
        main.post(() -> stopConnection("连接已停止", "VPN 权限被系统收回，可能已开启其他 VPN。"));
    }

    @Override public void onDestroy() {
        closeRunner();
        stopWatching();
        if (status.running) status = new Status(false, "未连接", "连接已停止。");
        stopForeground(true);
        super.onDestroy();
    }

    private final class TunnelRunner implements Closeable {
        final ParcelFileDescriptor descriptor;
        final ServerAddress address;
        final DnsTunnel tunnel;
        final ThreadPoolExecutor workers = new ThreadPoolExecutor(4, 4, 0, TimeUnit.MILLISECONDS,
                new ArrayBlockingQueue<>(64), new ThreadPoolExecutor.AbortPolicy());
        final Set<Closeable> sockets = Collections.newSetFromMap(new ConcurrentHashMap<Closeable, Boolean>());
        final Object writes = new Object();
        volatile PhysicalNetwork physical;
        volatile boolean closed;

        TunnelRunner(ParcelFileDescriptor descriptor, ServerAddress address, PhysicalNetwork physical) {
            this.descriptor = descriptor; this.address = address; this.physical = physical;
            tunnel = new DnsTunnel(this::resolve, this::send);
        }

        void start() { new Thread(this::read, "game-dns-tun").start(); }

        private void fail(Exception error) {
            if (closed) return;
            Log.e(TAG, "DNS tunnel failed", error);
            main.post(() -> {
                if (runner == this) stopConnection("连接中断", "系统网络接口已关闭，请重新开启连接。");
            });
        }

        private void read() {
            byte[] buffer = new byte[65535];
            StructPollfd poll = new StructPollfd();
            poll.fd = descriptor.getFileDescriptor(); poll.events = (short) OsConstants.POLLIN;
            try {
                while (!closed) {
                    Os.poll(new StructPollfd[]{poll}, 250);
                    if (closed) break;
                    if ((poll.revents & (OsConstants.POLLERR | OsConstants.POLLHUP | OsConstants.POLLNVAL)) != 0)
                        throw new IOException("TUN closed");
                    if ((poll.revents & OsConstants.POLLIN) != 0) {
                        try {
                            int size = Os.read(poll.fd, buffer, 0, buffer.length);
                            if (size <= 0) throw new IOException("TUN EOF");
                            tunnel.accept(Arrays.copyOf(buffer, size), SystemClock.elapsedRealtime());
                        } catch (ErrnoException error) {
                            if (error.errno != OsConstants.EAGAIN && error.errno != OsConstants.EINTR) throw error;
                        }
                    }
                    tunnel.tick(SystemClock.elapsedRealtime());
                }
            } catch (Exception error) { fail(error); }
        }

        private void send(byte[] packet) {
            synchronized (writes) {
                if (closed) return;
                try {
                    if (Os.write(descriptor.getFileDescriptor(), packet, 0, packet.length) != packet.length)
                        throw new IOException("short TUN write");
                } catch (ErrnoException error) {
                    // Dropping a full-queue packet lets DNS/TCP perform its normal retry.
                    if (error.errno != OsConstants.EAGAIN && error.errno != OsConstants.EINTR) fail(error);
                } catch (IOException error) { fail(error); }
            }
        }

        private void resolve(byte[] query, Consumer<byte[]> reply) {
            byte[] local = DnsMessage.localResponse(query, address);
            if (local != null) { redirected.incrementAndGet(); reply.accept(local); return; }
            PhysicalNetwork current = physical;
            if (current == null || closed) { unavailable(query, reply); return; }
            try {
                workers.execute(() -> {
                    try {
                        if (closed || physical != current) throw new IOException("network changed");
                        byte[] answer = new DnsForwarder(current.dns, transport(current), 1500).resolve(query);
                        if (physical != current) throw new IOException("network changed");
                        if (!closed) reply.accept(answer);
                    } catch (IOException | RuntimeException error) { if (!closed) unavailable(query, reply); }
                });
            } catch (RejectedExecutionException error) { unavailable(query, reply); }
        }

        private void unavailable(byte[] query, Consumer<byte[]> reply) {
            dnsFailures.incrementAndGet();
            reply.accept(DnsMessage.error(query, DnsMessage.SERVFAIL));
        }

        private DnsForwarder.Transport transport(PhysicalNetwork current) {
            return new DnsForwarder.Transport() {
                private void track(Closeable socket) throws IOException {
                    sockets.add(socket);
                    if (closed || physical != current) { release(socket); throw new IOException("network unavailable"); }
                }
                public DatagramSocket udp() throws IOException {
                    DatagramSocket socket = new DatagramSocket(null);
                    try {
                        track(socket);
                        if (!protect(socket)) throw new IOException("VPN socket protection failed");
                        current.network.bindSocket(socket);
                        return socket;
                    } catch (IOException | RuntimeException error) { release(socket); throw error; }
                }
                public Socket tcp() throws IOException {
                    Socket socket = new Socket();
                    try {
                        track(socket);
                        if (!protect(socket)) throw new IOException("VPN socket protection failed");
                        current.network.bindSocket(socket);
                        return socket;
                    } catch (IOException | RuntimeException error) { release(socket); throw error; }
                }
                public void release(Closeable socket) {
                    sockets.remove(socket);
                    DnsForwarder.Transport.super.release(socket);
                }
            };
        }

        void closeSockets() {
            for (Closeable socket : sockets) {
                try { socket.close(); } catch (IOException ignored) { }
                sockets.remove(socket);
            }
        }

        @Override public void close() {
            closed = true;
            tunnel.close();
            workers.shutdownNow();
            closeSockets();
            synchronized (writes) {
                try { descriptor.close(); } catch (IOException ignored) { }
            }
        }
    }
}
