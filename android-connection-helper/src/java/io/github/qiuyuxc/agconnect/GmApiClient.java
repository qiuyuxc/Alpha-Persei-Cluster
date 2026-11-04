package io.github.qiuyuxc.agconnect;

import android.content.Context;
import android.content.SharedPreferences;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.IOException;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLException;
import org.json.JSONArray;
import org.json.JSONObject;

/** Direct HTTPS with platform certificate verification and origin-scoped sessions. */
public final class GmApiClient {
    public final String origin;
    private final SharedPreferences sessions;
    private volatile String token;

    public static final class Failure extends Exception {
        public final int status;
        Failure(int status, String message) { super(message); this.status = status; }
    }
    public static final class Result {
        public final Object data;
        public final String message;
        Result(Object data, String message) { this.data = data; this.message = message; }
        public JSONObject object() throws Failure {
            if (!(data instanceof JSONObject)) throw new Failure(0, "服务器响应缺少账号数据");
            return (JSONObject) data;
        }
        public JSONArray array() throws Failure {
            if (!(data instanceof JSONArray)) throw new Failure(0, "服务器响应缺少列表数据");
            return (JSONArray) data;
        }
    }

    public GmApiClient(Context context) {
        origin = GmServer.origin(context);
        sessions = context.getSharedPreferences("agconnect_native_gm_sessions", Context.MODE_PRIVATE);
        token = sessions.getString("token:" + origin, "");
        if (!token.isEmpty() && !validToken(token)) clear();
    }
    public boolean hasSession() { return !token.isEmpty(); }
    public void remember(String value) throws Failure {
        if (!validToken(value)) throw new Failure(0, "服务器未返回有效登录凭证");
        token = value; sessions.edit().putString("token:" + origin, value).apply();
    }
    public void clear() { token = ""; sessions.edit().remove("token:" + origin).apply(); }
    private static boolean validToken(String value) {
        return value != null && value.length() >= 30 && value.length() <= 256
                && value.matches("[A-Za-z0-9_-]+\\.[0-9]{1,19}");
    }
    public static String encode(String value) {
        try { return URLEncoder.encode(value, "UTF-8"); }
        catch (Exception impossible) { throw new IllegalArgumentException(impossible); }
    }
    public Result request(String method, String path, JSONObject body) throws Failure {
        String url = origin + path;
        if (!GmApiPolicy.allows(origin, method, url)) throw new Failure(0, "请求地址不属于此服务器的辅助接口");
        HttpsURLConnection connection = null;
        try {
            connection = (HttpsURLConnection) new URL(url).openConnection();
            connection.setInstanceFollowRedirects(false);
            connection.setConnectTimeout(10000); connection.setReadTimeout(15000);
            connection.setRequestMethod(method);
            connection.setRequestProperty("Accept", "application/json");
            String current = token;
            if (!current.isEmpty()) connection.setRequestProperty("Authorization", "Bearer " + current);
            if (body != null) {
                byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
                connection.setDoOutput(true);
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                connection.setFixedLengthStreamingMode(bytes.length);
                try (java.io.OutputStream output = connection.getOutputStream()) { output.write(bytes); }
            }
            int status = connection.getResponseCode();
            InputStream stream = status >= 400 ? connection.getErrorStream() : connection.getInputStream();
            if (stream == null) throw new Failure(status, "服务器未返回数据，请稍后重试");
            ByteArrayOutputStream bytes = new ByteArrayOutputStream();
            try (InputStream input = stream) {
                byte[] buffer = new byte[8192]; int length;
                while ((length = input.read(buffer)) != -1) {
                    if (bytes.size() + length > 2 * 1024 * 1024) throw new Failure(0, "服务器列表过大，请在完整后台查看");
                    bytes.write(buffer, 0, length);
                }
            }
            JSONObject envelope = new JSONObject(new String(bytes.toByteArray(), StandardCharsets.UTF_8));
            int code = envelope.optInt("code", -1);
            if (status < 200 || status >= 300 || code != 0)
                throw new Failure(status == 401 || code == 401 ? 401 : code,
                        envelope.optString("msg", "操作未完成，请稍后重试"));
            return new Result(envelope.opt("data"), envelope.optString("msg", "成功"));
        } catch (Failure error) { throw error; }
        catch (SSLException error) { throw new Failure(0, "证书校验失败，请检查已安装的服务器用户 CA"); }
        catch (IOException error) { throw new Failure(0, "连接服务器失败，请检查网络后重试"); }
        catch (Exception error) { throw new Failure(0, "服务器响应无法读取，请重试或检查服务端版本"); }
        finally { if (connection != null) connection.disconnect(); }
    }
}
