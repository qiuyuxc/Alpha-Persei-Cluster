# -*- coding: utf-8 -*-
"""battle_server.py — V5 动态战斗服（UDP/KCP，XServer 协议）

由 V4.1 battle_server_udp.py（对齐 2026-08-07 材料本战斗流程记录.pcapng 实测格式）移植，
保留实测报文格式，工程层重写：
- 修复 V4.1 _frame_loop 用 client_id（int）当地址发帧导致帧线程首帧即死的缺陷
- 参数化原硬编码常量（Pong 收敛差值序列 / KCP 重传节奏 / 帧间隔）
- KCP 接收去重、按序重组，未确认响应按原序号重传；开始与结算事件幂等
- 会话表加锁（UDP 线程与帧线程并发）；闲置会话自动回收
- 结算结果登记（battle_id → result），供 TCP 侧 h_54032 结算引擎取用（V5 不再从
  UDP 线程裸写 game socket——客户端战毕必然回发 cs_54032，被动应答即够）

线上格式（pcapng 实测，勿改动）：
- UDP 报文：[type 1B][conn u32 LE][conv u32 LE][KCP 段...]            （9B 头）
- KCP 段：  [cmd 1B][frg 1B][wnd 2B][ts 4B][sn 4B][una 4B][len 4B][data]（20B 头，无 conv；
            客户端按 20B 解析，24B 带 conv 会被 PeekSize=-1 判废丢弃）
- 帧：      [flag 1B][opcode u16 LE][rpc u32 LE（仅 flag=1/2）][body]
- Pong(101) body = [clientTime u64 LE][serverTime u64 LE] 裸二进制（非 protobuf）
- start_match(125) = flag 0 + 空 body；Ack_BattleResult(134) = flag 2 + rpc 回显 + body{1:result}
- Ack_BattleGSResult(135) = flag 0 + body{1:1}；ACK9 握手 = [type=2][conn][conv] 9B 无段
"""
import socket
import struct
import threading
import time

SYN, ACK, FIN, MSG, ERR = 1, 2, 3, 4, 5
KCP_CMD_PUSH, KCP_CMD_ACK, KCP_CMD_WASK, KCP_CMD_WINS = 81, 82, 83, 84

# opcode（dump.cs [Message(N)]）
OP_PING = 100
OP_PONG = 101
OP_CONNECT_REQUEST = 102
OP_CONNECT_RESPONSE = 103
OP_JOIN_ROOM = 126
OP_ACK_JOIN_ROOM = 127
OP_FETCH_TEAM_INFO = 128
OP_ACK_FETCH_TEAM_INFO = 129
OP_PLAYER_READY = 130
OP_ACK_PLAYER_READY = 131
OP_START_MATCH = 125
OP_SERVER_FRAME = 124
OP_BATTLE_FRAMES = 136
OP_BATTLE_RESULT = 132
OP_ACK_BATTLE_RESULT = 134
OP_ACK_BATTLE_GS_RESULT = 135
OP_ERROR_CODE = 133

DEFAULT_PORT = 6105

# ---- 可调参数（原 V4.1 硬编码，此处全部参数化） ----
# Pong serverTime 与 clientTime 的差值序列：前几次真实波动、随后收敛——
# 客户端 SyncTime 校验收敛过程，恒定差值会被判未同步（pcapng 实测曲线）
PONG_DELTAS = (-585, -313, -338, -315)
FRAME_INTERVAL = 0.033     # 30fps Server_Frame
SESSION_IDLE_TIMEOUT = 600  # 会话闲置回收（秒）
KCP_RCV_WINDOW = 256
KCP_PENDING_LIMIT = 4096
KCP_RETRY_INTERVAL = 0.2
KCP_RETRY_MAX_INTERVAL = 2.0
KCP_MAX_MESSAGE = 1024 * 1024


def _sn_diff(left, right):
    """KCP uint32 sequence comparison, including wraparound."""
    return ((left - right + 0x80000000) & 0xFFFFFFFF) - 0x80000000


def _u64(v):
    return struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF)


def _varint(v):
    out = b""
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def _tag(field, wt):
    return _varint((field << 3) | wt)


def _f_varint2(field, v):
    return _tag(field, 0) + _varint(v)


def _f_bytes(field, b):
    return _tag(field, 2) + _varint(len(b)) + b


def rd_varint(b, i):
    v, sh = 0, 0
    while True:
        x = b[i]
        i += 1
        v |= (x & 0x7F) << sh
        sh += 7
        if not (x & 0x80):
            return v, i


def parse_pb_top(payload):
    """顶层 protobuf 字段扫描：{字段号: varint值 或 bytes}。"""
    out = {}
    i = 0
    try:
        while i < len(payload):
            tag, i = rd_varint(payload, i)
            f, w = tag >> 3, tag & 7
            if w == 0:
                v, i = rd_varint(payload, i)
                out[f] = v
            elif w == 2:
                ln, i = rd_varint(payload, i)
                out[f] = payload[i:i + ln]
                i += ln
            else:
                break
    except (IndexError, ValueError):
        pass
    return out


def xs_encode_frame(opcode, body, flag=0, rpc_id=0):
    if flag == 0:
        return struct.pack("<BH", 0, opcode) + body
    return struct.pack("<BHI", flag, opcode, rpc_id) + body


def xs_parse_frame(buf):
    if len(buf) < 3:
        return None
    flag = buf[0]
    opcode = struct.unpack_from("<H", buf, 1)[0]
    off, rpc_id = 3, 0
    if flag in (1, 2):
        if len(buf) < 7:
            return None
        rpc_id = struct.unpack_from("<I", buf, 3)[0]
        off = 7
    return {"flag": flag, "opcode": opcode, "rpc_id": rpc_id, "body": buf[off:]}


def kcp_encode_seg_noconv(cmd, wnd, ts, sn, una, data=b""):
    """20B 无 conv 段头（客户端按 20B 解析，24B 会被判废）"""
    return struct.pack("<BBHIIII", cmd, 0, wnd, ts, sn, una, len(data)) + data


def kcp_parse_seg(buf):
    if len(buf) < 24:
        return None
    conv, cmd, frg, wnd, ts, sn, una, ln = struct.unpack_from("<IBBHIIII", buf, 0)
    if 24 + ln > len(buf):
        return None
    return {"conv": conv, "cmd": cmd, "frg": frg, "wnd": wnd, "ts": ts, "sn": sn,
            "una": una, "data": buf[24:24 + ln], "len": ln}


def parse_ping(body):
    return struct.unpack_from("<Q", body, 0)[0] if len(body) >= 8 else 0


def parse_join_room(body):
    f = parse_pb_top(body)
    acct = f.get(2, b"")
    if isinstance(acct, bytes):
        acct = acct.decode(errors="replace")
    elif not isinstance(acct, str):
        acct = str(acct)
    return f.get(1, 0), acct, f.get(3, 0)


def enc_ack_join_room(battle_id, seat_id, role_id):
    return _f_varint2(1, battle_id) + _f_varint2(2, seat_id) + _f_varint2(3, role_id)


def parse_player_ready(body):
    f = parse_pb_top(body)
    return f.get(1, 0), f.get(2, 0)


def enc_ack_player_ready(player_id):
    return _f_varint2(1, player_id)


def enc_server_frame(frame_count, inputs=b""):
    return _f_varint2(1, frame_count) + _f_bytes(2, inputs)


def enc_ack_battle_result(result=1):
    """134 Ack_BattleResult: result(int) -> \x08\x01(胜利) / \x08\x02(失败) / \x08\x03(退出)"""
    return _f_varint2(1, int(result or 1))


def enc_ack_battle_gs_result(code=1):
    """135 Ack_BattleGSResult: code(int) -> \x08\x01(胜利) / \x08\x02(失败) / \x08\x03(退出)"""
    return _f_varint2(1, int(code or 1))


def parse_battle_result(body):
    f = parse_pb_top(body)
    bid = f.get(1, 0)
    uuid_v = f.get(2, 0)
    result = f.get(3, 0)
    info_raw = f.get(4, b"")
    info = {}
    if isinstance(info_raw, bytes) and info_raw:
        info_f = parse_pb_top(info_raw)
        info["result"] = bool(info_f.get(1, 0))
        info["battle_time"] = int(info_f.get(2, 0))
        info["total_dead_num"] = int(info_f.get(3, 0))
        info["total_hitted_num"] = int(info_f.get(4, 0))
        info["injured_num"] = int(info_f.get(5, 0))
        info["fall_down_num"] = int(info_f.get(6, 0))
        info["knockout_num"] = int(info_f.get(7, 0))
        info["qte_count"] = int(info_f.get(8, 0))
        info["resurrect_times"] = int(info_f.get(9, 0))
        info["enemy_dead_num"] = int(info_f.get(11, 0))
    return bid, uuid_v, result, info


class BattleServer:
    """V5 动态战斗服。on_result(battle_id, result) 在 UDP 线程回调（只做登记，勿做重活）。"""

    def __init__(self, port=DEFAULT_PORT, log=None, on_result=None, server_ts=None,
                 bind_host="0.0.0.0"):
        self.port = port
        self.log = log or (lambda *a, **k: None)
        self.on_result = on_result
        self.server_ts = server_ts   # Unix 秒，可含小数；Pong 在线路上使用毫秒，None 保留兼容序列
        self.running = False
        self._lock = threading.RLock()
        self._conns = {}             # client_id -> session
        self._results = {}           # battle_id -> result（132 上报登记，TCP 侧结算取用）
        self._pong_seq = {}          # client_id -> 收敛序列游标
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception:
            pass
        self.sock.bind((bind_host, port))
        self.sock.settimeout(0.1)
        self._frame_thread = None

    # ---------- 对外接口 ----------
    def get_result(self, battle_id, default=None):
        with self._lock:
            return self._results.get(battle_id, default)

    def stop(self):
        self.running = False

    # ---------- 发送 ----------
    def _send_pkt(self, addr, conn, conv, seg):
        pkt = bytes([MSG]) + struct.pack("<I", conn) + struct.pack("<I", conv) + seg
        try:
            self.sock.sendto(pkt, addr)
        except OSError as e:
            self.log(f"[BattleSrv] sendto {addr} 失败: {e}")

    def _send_ack9(self, addr, server_id, client_id):
        try:
            self.sock.sendto(bytes([ACK]) + struct.pack("<I", server_id)
                             + struct.pack("<I", client_id), addr)
        except OSError:
            pass

    def _send_kcp(self, addr, st, cmd, data, sn=None, ts=None):
        with self._lock:
            if ts is None:
                ts = int(time.time() * 1000) & 0xFFFFFFFF
            if sn is None:
                if len(st["send_buf"]) >= KCP_PENDING_LIMIT:
                    raise RuntimeError("KCP unacknowledged send limit reached")
                st["sn"] = (st["sn"] + 1) & 0xFFFFFFFF
                sn = st["sn"]
                if cmd == KCP_CMD_PUSH:
                    st["send_buf"][sn] = {
                        "data": data, "ts": ts, "rto": KCP_RETRY_INTERVAL,
                        "retry_at": time.monotonic() + KCP_RETRY_INTERVAL,
                    }
            seg = kcp_encode_seg_noconv(
                cmd, KCP_RCV_WINDOW - len(st["recv_buf"]), ts, sn, st["rcv_nxt"], data)
            self._send_pkt(addr, st["conn"], st["conv"], seg)
            return sn

    def _send_frame(self, addr, st, opcode, body, flag=2, rpc_id=0, ts=None):
        return self._send_kcp(addr, st, KCP_CMD_PUSH,
                              xs_encode_frame(opcode, body, flag, rpc_id), ts=ts)

    def _retry_sn(self, st, sn):
        """Resend an unacknowledged event with its original sequence and bytes."""
        pending = st["send_buf"].get(sn)
        if pending is not None:
            self._send_kcp(st["addr"], st, KCP_CMD_PUSH, pending["data"],
                           sn=sn, ts=pending["ts"])

    def _retransmit_pending(self, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            for st in self._conns.values():
                for sn, pending in st["send_buf"].items():
                    if now >= pending["retry_at"]:
                        self._retry_sn(st, sn)
                        pending["rto"] = min(pending["rto"] * 2, KCP_RETRY_MAX_INTERVAL)
                        pending["retry_at"] = now + pending["rto"]

    def _ack_sent(self, st, seg):
        # Peer una acknowledges OUR sends. It must never advance OUR receive cursor.
        una = seg["una"]
        if _sn_diff(una, (st["sn"] + 1) & 0xFFFFFFFF) <= 0:
            for sn in list(st["send_buf"]):
                if _sn_diff(sn, una) < 0:
                    del st["send_buf"][sn]
        if seg["cmd"] == KCP_CMD_ACK:
            st["send_buf"].pop(seg["sn"], None)

    def _receive_push(self, st, seg):
        distance = _sn_diff(seg["sn"], st["rcv_nxt"])
        if distance >= KCP_RCV_WINDOW:
            return  # Do not acknowledge data outside the receive window.
        frames = []
        if distance >= 0:
            st["recv_buf"].setdefault(seg["sn"], seg)
            while st["rcv_nxt"] in st["recv_buf"]:
                part = st["recv_buf"].pop(st["rcv_nxt"])
                st["rcv_nxt"] = (st["rcv_nxt"] + 1) & 0xFFFFFFFF
                expected = st["pkt_frg"]
                if expected is not None and part["frg"] != expected:
                    st["pkt_buf"], st["pkt_frg"] = b"", None
                    self.log("[BattleSrv] discarded invalid KCP fragment sequence")
                    continue
                st["pkt_buf"] += part["data"]
                if len(st["pkt_buf"]) > KCP_MAX_MESSAGE:
                    st["pkt_buf"], st["pkt_frg"] = b"", None
                    raise ValueError("KCP message exceeds receive limit")
                if part["frg"]:
                    st["pkt_frg"] = part["frg"] - 1
                else:
                    frames.append((st["pkt_buf"], part["sn"], part["ts"]))
                    st["pkt_buf"], st["pkt_frg"] = b"", None
        else:
            st["duplicate_pushes"] += 1
        # ACK duplicates too: the original ACK may have been lost. Only contiguous,
        # complete messages reach the application, even after loss or reordering.
        self._send_kcp(st["addr"], st, KCP_CMD_ACK, b"", sn=seg["sn"], ts=seg["ts"])
        for data, sn, ts in frames:
            self._handle_frame(data, st, sn, ts)

    # ---------- 接收 ----------
    def _new_session(self, client_id, server_id, addr):
        st = {"conn": server_id, "conv": client_id, "client_id": client_id,
              "ts": 0, "sn": -1, "frame_count": 0, "started": False,
              "send_initialized": False, "addr": addr, "last_seen": time.time(),
              "entry_started": time.monotonic(), "entry_pings": 0,
              "rcv_nxt": 0, "recv_buf": {}, "send_buf": {},
              "pkt_buf": b"", "pkt_frg": None, "duplicate_pushes": 0,
              "battle_events": {}}
        self._conns[client_id] = st
        return st

    def _handle_pkt(self, data, addr):
        with self._lock:
            self._handle_pkt_locked(data, addr)

    def _handle_pkt_locked(self, data, addr):
        if len(data) < 5:
            return
        typ = data[0]
        local_conn = struct.unpack_from("<I", data, 1)[0]
        cid = local_conn  # 会话按 client_id（客户端每包换 UDP 端口，不能按 addr）
        with self._lock:
            st = self._conns.get(cid)

        if typ == SYN and len(data) >= 9:
            client_id = (struct.unpack_from("<I", data, 3)[0]
                         if len(data) >= 11 and data[2] == 1 else local_conn)
            server_id = (int(time.time() * 1000) ^ 0x11A04C54 ^ client_id) & 0xFFFFFFFF
            with self._lock:
                st = self._conns.get(client_id)
                if st is None:
                    st = self._new_session(client_id, server_id, addr)
                elif st["addr"][0] != addr[0]:
                    return
                else:
                    st["addr"], st["last_seen"] = addr, time.time()
                    server_id = st["conn"]
            self.log(f"[BattleSrv] SYN client={client_id} server={server_id} -> ACK9")
            self._send_ack9(addr, server_id, client_id)
            return

        if typ == ACK and len(data) >= 9:
            if not st:
                client_id = struct.unpack_from("<I", data, 5)[0]
                with self._lock:
                    if client_id not in self._conns:
                        self._new_session(client_id, local_conn, addr)
            return

        if typ in (FIN, ERR):
            with self._lock:
                self._conns.pop(cid, None)
                self._pong_seq.pop(cid, None)
            return

        if typ != MSG:
            return

        if not st:
            # 直连模式（无 SYN）：从报文学 conv（对端期望的 server id）
            peer = struct.unpack_from("<I", data, 5)[0] if len(data) >= 9 else 0
            with self._lock:
                st = self._new_session(cid, peer, addr)
        else:
            st["addr"] = addr  # 每包更新最新端口
            st["last_seen"] = time.time()

        # 解析 KCP 段（24B 头含 conv）；兼容 2B 随机前缀
        off = 5
        if len(data) >= off + 24:
            first_conv = struct.unpack_from("<I", data, off)[0]
            if st.get("conn") and first_conv != st["conn"]:
                off += 2
        while off + 24 <= len(data):
            seg = kcp_parse_seg(data[off:])
            if not seg or seg["conv"] != st["conn"]:
                break
            off += 24 + seg["len"]
            if seg["cmd"] not in (KCP_CMD_PUSH, KCP_CMD_ACK, KCP_CMD_WASK, KCP_CMD_WINS):
                continue
            if not st["send_initialized"]:
                st["send_initialized"] = True
                # Independent sequence spaces: peer una is its next expected SERVER
                # sequence, whereas peer sn numbers CLIENT data (2026-10-26 capture).
                st["sn"] = (seg["una"] - 1) & 0xFFFFFFFF
            self._ack_sent(st, seg)
            if seg["cmd"] == KCP_CMD_PUSH:
                self._receive_push(st, seg)
            elif seg["cmd"] == KCP_CMD_WASK:
                self._send_kcp(st["addr"], st, KCP_CMD_WINS, b"", sn=0, ts=seg["ts"])

    def _handle_frame(self, fbuf, st, kcp_sn=0, kcp_ts=0):
        f = xs_parse_frame(fbuf)
        if not f:
            return
        op, rpc = f["opcode"], f["rpc_id"]
        addr = st.get("addr")
        if op == OP_PING:
            ping_started = time.monotonic()
            ct = parse_ping(f["body"])
            with self._lock:
                n = self._pong_seq.get(st["client_id"], 0)
                self._pong_seq[st["client_id"]] = n + 1
            if self.server_ts:
                srv = int(self.server_ts() * 1000) if callable(self.server_ts) \
                    else int(self.server_ts * 1000)
            else:
                srv = ct + PONG_DELTAS[min(n, len(PONG_DELTAS) - 1)]
            # KCP.Input 按段逐个解析，一段失败整包丢弃 → Pong 必须独立成包，不与 ACK 合并
            self._send_frame(addr, st, OP_PONG, _u64(ct) + _u64(srv),
                             rpc_id=rpc, ts=srv & 0xFFFFFFFF)
            elapsed_ms = (time.monotonic() - ping_started) * 1000
            st["entry_pings"] = st.get("entry_pings", 0) + 1
            delta = srv - ct
            st["pong_delta_min"] = min(st.get("pong_delta_min", delta), delta)
            st["pong_delta_max"] = max(st.get("pong_delta_max", delta), delta)
            st["pong_max_handler_ms"] = max(st.get("pong_max_handler_ms", 0), elapsed_ms)
            if st["entry_pings"] == 1:
                first_ms = (ping_started - st.get("entry_started", ping_started)) * 1000
                self.log(f"[BattleSrv] 入场首个PONG client={st['client_id']} after_ms={first_ms:.2f} "
                         f"client_ms={ct} server_ms={srv} handler_ms={elapsed_ms:.2f}")
        elif op == OP_CONNECT_REQUEST:
            self._send_frame(addr, st, OP_CONNECT_RESPONSE,
                             _f_varint2(1, 0) + _f_varint2(2, 1) + _f_varint2(3, 1), rpc_id=rpc)
        elif op == OP_JOIN_ROOM:
            battle_id, account, role_id = parse_join_room(f["body"])
            st["battle_id"] = battle_id
            st["role_id"] = role_id
            self._send_frame(addr, st, OP_ACK_JOIN_ROOM,
                             enc_ack_join_room(battle_id, 1, role_id), rpc_id=rpc)
        elif op == OP_PLAYER_READY:
            ready_started = time.monotonic()
            uuid_v, battle_id = parse_player_ready(f["body"])
            event = st["battle_events"].setdefault(battle_id, {})
            st["battle_id"] = battle_id
            st["player_ready"] = True
            # ack field1 = battle_id（客户端 Ready 存 packet+24），再推 start_match
            self._send_frame(addr, st, OP_ACK_PLAYER_READY,
                             enc_ack_player_ready(battle_id or 1), rpc_id=rpc)
            if "start_sn" not in event and "result" not in event:
                event["start_sn"] = self._send_frame(addr, st, OP_START_MATCH, b"", flag=0)
                st["started"] = True
                self.log(f"[BattleSrv] StartMatch bid={battle_id} client={st['client_id']} "
                         f"rpc={rpc} recv_sn={kcp_sn} send_sn={event['start_sn']}")
            elif "start_sn" in event and "result" not in event:
                self._retry_sn(st, event["start_sn"])
            if not st.get("entry_ready_logged"):
                st["entry_ready_logged"] = True
                now = time.monotonic()
                total_ms = (now - st.get("entry_started", ready_started)) * 1000
                delta_span = st.get("pong_delta_max", 0) - st.get("pong_delta_min", 0)
                self.log(f"[BattleSrv] 入场Ready client={st['client_id']} total_ms={total_ms:.2f} "
                         f"pings={st.get('entry_pings', 0)} delta_span_ms={delta_span} "
                         f"pong_max_handler_ms={st.get('pong_max_handler_ms', 0):.2f} "
                         f"ready_reply_ms={(now - ready_started) * 1000:.2f}")
        elif op == OP_FETCH_TEAM_INFO:
            self._send_frame(addr, st, OP_ACK_FETCH_TEAM_INFO, b"", rpc_id=rpc)
        elif op == OP_BATTLE_RESULT:
            bid, uuid_v, result, info = parse_battle_result(f["body"])
            event = st["battle_events"].setdefault(bid, {})
            st["started"] = False
            # 先登记再重传（实机 2026-08-22：客户端 cs_54032 在 134/135 重传窗口 ~0.6s
            # 内就到达，晚登记会被结算引擎"缺记从宽按胜利"兜底吞掉——失败被按胜利结算）
            if "result" not in event:
                event["result"] = result
                self.log(f"[BattleSrv] BattleResult bid={bid} result={result} info={info} "
                         f"duplicate_pushes={st['duplicate_pushes']}")
                with self._lock:
                    self._results[bid] = {"result": result, "info": info}
                if self.on_result:
                    try:
                        self.on_result(bid, result)
                    except Exception as e:
                        self.log(f"[BattleSrv] on_result 回调异常: {e}")
            result = event["result"]
            # 收到 132 战报，精准回发 134(ACK, flag=2, rpc回显, 携带对应result) 与 135(GS_ACK, flag=0, 携带对应code=result)
            self._send_frame(addr, st, OP_ACK_BATTLE_RESULT, enc_ack_battle_result(result), rpc_id=rpc)
            if "result_sn" not in event:
                event["result_sn"] = self._send_frame(
                    addr, st, OP_ACK_BATTLE_GS_RESULT, enc_ack_battle_gs_result(result), flag=0)
            else:
                self._retry_sn(st, event["result_sn"])
        elif op in (0, 123):
            # 客户端战斗帧上传（op=0 握手期 / 123 战斗中，失败抓包 2026-08-09 实测：
            # 132 上报后客户端仍持续发 123 直至 FIN）——传输层 KCP ACK 已确认，无需应用层响应
            st.setdefault("frame_up_seen", 0)
            st["frame_up_seen"] += 1
            if st["frame_up_seen"] == 1:
                self.log(f"[BattleSrv] 客户端战斗帧上传 op={op}（静默接收）")
        else:
            self.log(f"[BattleSrv] 未处理 opcode={op} flag={f['flag']} body={len(f['body'])}B")

    # ---------- 周期帧 ----------
    def _frame_loop(self):
        # 仅联机多人模式（is_multiplayer）才下发 124 Server_Frame 锁步推进
        # 单人 PVE 模式官方抓包实测不下发 124，避免干扰客户端本地慢镜头与胜利结算动画
        while self.running:
            time.sleep(FRAME_INTERVAL)
            with self._lock:
                sessions = [(st["addr"], st) for st in self._conns.values()
                            if st.get("started") and st.get("is_multiplayer")]
                for addr, st in sessions:
                    st["frame_count"] += 1
                    try:
                        self._send_frame(addr, st, OP_SERVER_FRAME,
                                         enc_server_frame(st["frame_count"]), flag=0)
                    except Exception:
                        pass

    def _gc_loop(self):
        while self.running:
            time.sleep(60)
            now = time.time()
            with self._lock:
                dead = [cid for cid, st in self._conns.items()
                        if now - st.get("last_seen", now) > SESSION_IDLE_TIMEOUT]
                for cid in dead:
                    self._conns.pop(cid, None)
                    self._pong_seq.pop(cid, None)
                # 结果登记表只留最近 200 条
                if len(self._results) > 200:
                    for k in list(self._results.keys())[:-200]:
                        self._results.pop(k, None)

    # ---------- 主循环 ----------
    def run(self):
        self.running = True
        self._frame_thread = threading.Thread(target=self._frame_loop, daemon=True)
        self._frame_thread.start()
        threading.Thread(target=self._gc_loop, daemon=True).start()
        self.log(f"[BattleSrv] UDP 战斗服监听 :{self.port}")
        while self.running:
            self._retransmit_pending()
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle_pkt(data, addr)
            except Exception as e:
                self.log(f"[BattleSrv] 处理异常: {e}")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="V5 动态战斗服（独立运行）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()
    srv = BattleServer(args.port, log=print)
    try:
        srv.run()
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()
