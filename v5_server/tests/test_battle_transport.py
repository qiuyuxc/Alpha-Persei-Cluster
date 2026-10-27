"""Battle entry must survive UDP duplication, loss, and reordering exactly once.

No live server, database, or device is used. The fragmented Ready case follows the
2026-10-26 capture: sn=5/6, frg=1/0, repeated with a new timestamp before ACK arrival.
"""
import struct
import socket
import threading
import time
import unittest
from unittest.mock import patch

import battle_server as b


class SocketSink:
    def __init__(self):
        self.packets = []

    def setsockopt(self, *args):
        pass

    def bind(self, *args):
        pass

    def settimeout(self, *args):
        pass

    def sendto(self, data, addr):
        self.packets.append((data, addr))


class BattleTransportTests(unittest.TestCase):
    def setUp(self):
        self.sock = SocketSink()
        self.patch = patch.object(b.socket, 'socket', return_value=self.sock)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.results = []
        self.srv = b.BattleServer(on_result=lambda *args: self.results.append(args))
        self.addr = ('127.0.0.1', 30001)
        self.st = self.srv._new_session(101, 202, self.addr)

    def segment(self, sn, body=b'', frg=0, cmd=b.KCP_CMD_PUSH, una=0, ts=100, conv=None):
        return struct.pack('<IBBHIIII', self.st['conn'] if conv is None else conv,
                           cmd, frg, 256, ts, sn, una, len(body)) + body

    def send(self, sn, body=b'', **kwargs):
        packet = bytes([b.MSG]) + struct.pack('<I', self.st['client_id'])
        self.srv._handle_pkt(packet + self.segment(sn, body, **kwargs), self.addr)

    def frame(self, op, body=b'', rpc=1):
        return b.xs_encode_frame(op, body, flag=1, rpc_id=rpc)

    def ready(self, bid=123456, rpc=6):
        return self.frame(b.OP_PLAYER_READY, b._f_varint2(1, 105) + b._f_varint2(2, bid), rpc)

    def result(self, bid=123456, result=1, rpc=9):
        return self.frame(b.OP_BATTLE_RESULT,
                          b._f_varint2(1, bid) + b._f_varint2(2, 105) + b._f_varint2(3, result), rpc)

    def packets(self, cmd=None, op=None):
        found = []
        for pkt, addr in self.sock.packets:
            if pkt[0] != b.MSG:
                continue
            command, frg, wnd, ts, sn, una, size = struct.unpack_from('<BBHIIII', pkt, 9)
            data = pkt[29:]
            self.assertEqual(len(data), size)
            frame = b.xs_parse_frame(data) if command == b.KCP_CMD_PUSH else None
            if cmd is not None and command != cmd:
                continue
            if op is not None and (not frame or frame['opcode'] != op):
                continue
            found.append(dict(cmd=command, sn=sn, una=una, ts=ts, data=data, frame=frame, addr=addr))
        return found

    def unique_events(self, op):
        return {(p['sn'], p['data']) for p in self.packets(op=op)}

    def test_captured_fragmented_ready_retransmission_starts_once(self):
        for sn in range(5):
            self.send(sn, self.frame(b.OP_PING, b._u64(1000 + sn), rpc=sn + 1), una=sn)
        ready = self.ready() + b._f_bytes(3, b'x' * 528)
        for ts in (100, 207):
            prefix = bytes([b.MSG]) + struct.pack('<I', 101)
            self.srv._handle_pkt(prefix + self.segment(5, ready[:446], frg=1, una=5, ts=ts)
                                 + self.segment(6, ready[446:], una=5, ts=ts), self.addr)
        self.assertEqual(len(self.packets(op=b.OP_ACK_PLAYER_READY)), 1)
        self.assertEqual(len(self.packets(op=b.OP_START_MATCH)), 1)
        self.assertEqual(self.st['duplicate_pushes'], 2)
        self.assertEqual(self.st['rcv_nxt'], 7)
        self.assertEqual([p['sn'] for p in self.packets(cmd=b.KCP_CMD_ACK)[-4:]], [5, 6, 5, 6])

    def test_join_does_not_start_before_ready(self):
        self.send(0, self.frame(b.OP_JOIN_ROOM, b._f_varint2(1, 123456)))
        self.assertEqual(len(self.packets(op=b.OP_ACK_JOIN_ROOM)), 1)
        self.assertFalse(self.packets(op=b.OP_START_MATCH))
        self.send(1, self.ready())
        self.assertEqual(len(self.unique_events(b.OP_START_MATCH)), 1)

    def test_duplicate_first_fragment_does_not_corrupt_message(self):
        ready = self.ready()
        self.send(0, ready[:8], frg=1)
        self.send(0, ready[:8], frg=1, ts=200)
        self.send(1, ready[8:])
        self.assertEqual(self.st['battle_id'], 123456)
        self.assertEqual(len(self.packets(op=b.OP_START_MATCH)), 1)

    def test_first_packet_reordered_waits_for_missing_sequence(self):
        self.send(1, self.ready())
        self.assertFalse(self.packets(op=b.OP_START_MATCH))
        self.assertEqual(self.packets(cmd=b.KCP_CMD_ACK)[-1]['una'], 0)
        self.send(0, self.frame(b.OP_PING, b._u64(1000)))
        self.assertEqual(self.st['rcv_nxt'], 2)
        self.assertEqual(len(self.packets(op=b.OP_START_MATCH)), 1)

    def test_reordered_fragments_are_reassembled_in_sequence(self):
        ready = self.ready()
        self.send(1, ready[8:])
        self.send(0, ready[:8], frg=1)
        self.assertEqual(self.st['battle_id'], 123456)
        self.assertEqual(len(self.packets(op=b.OP_START_MATCH)), 1)

    def test_lost_fragment_blocks_following_message_until_retransmitted(self):
        ready = self.ready()
        self.send(0, ready[:8], frg=1)
        self.send(2, self.frame(b.OP_PING, b._u64(1000)))
        self.assertFalse(self.packets(cmd=b.KCP_CMD_PUSH))
        self.assertEqual(self.packets(cmd=b.KCP_CMD_ACK)[-1]['una'], 1)
        self.send(1, ready[8:])
        self.assertEqual([p['frame']['opcode'] for p in self.packets(cmd=b.KCP_CMD_PUSH)],
                         [b.OP_ACK_PLAYER_READY, b.OP_START_MATCH, b.OP_PONG])

    def test_lost_input_ack_duplicate_is_acknowledged_without_redispatch(self):
        self.send(0, self.ready())
        self.send(0, self.ready(), ts=200)
        self.assertEqual(len(self.packets(cmd=b.KCP_CMD_ACK)), 2)
        self.assertEqual(self.packets(cmd=b.KCP_CMD_ACK)[-1]['ts'], 200)
        self.assertEqual(len(self.packets(op=b.OP_START_MATCH)), 1)

    def test_lost_output_retries_same_sequences_and_bytes_then_stops_on_ack(self):
        self.send(0, self.ready())
        before = self.packets(cmd=b.KCP_CMD_PUSH)
        due = max(p['retry_at'] for p in self.st['send_buf'].values())
        self.srv._retransmit_pending(due)
        after = self.packets(cmd=b.KCP_CMD_PUSH)
        self.assertEqual([(p['sn'], p['data']) for p in before],
                         [(p['sn'], p['data']) for p in after[len(before):]])
        self.assertEqual(len(self.unique_events(b.OP_START_MATCH)), 1)
        self.send(0, cmd=b.KCP_CMD_ACK, una=2)
        self.assertFalse(self.st['send_buf'])
        self.srv._retransmit_pending(due + 10)
        self.assertEqual(len(self.packets(cmd=b.KCP_CMD_PUSH)), len(after))

    def test_lost_start_then_later_pong_client_can_deliver_in_order(self):
        self.send(0, self.ready())
        self.send(1, self.frame(b.OP_PING, b._u64(1000)), una=1)
        first = self.packets(cmd=b.KCP_CMD_PUSH)
        delivered, waiting, next_sn = [], {}, 0
        def receive(p):
            nonlocal next_sn
            if p['sn'] >= next_sn:
                waiting[p['sn']] = p
            while next_sn in waiting:
                delivered.append(waiting.pop(next_sn)['frame']['opcode'])
                next_sn += 1
        for p in first:
            if p['frame']['opcode'] != b.OP_START_MATCH:
                receive(p)
        self.assertEqual(delivered, [b.OP_ACK_PLAYER_READY])
        due = max(p['retry_at'] for p in self.st['send_buf'].values())
        self.srv._retransmit_pending(due)
        for p in self.packets(cmd=b.KCP_CMD_PUSH)[len(first):]:
            receive(p)
            receive(p)  # The retry itself can be duplicated by the network.
        self.assertEqual(delivered, [b.OP_ACK_PLAYER_READY, b.OP_START_MATCH, b.OP_PONG])

    def test_peer_ack_does_not_skip_our_receive_sequence(self):
        self.send(0, self.ready(), una=40)
        self.assertEqual(self.packets(cmd=b.KCP_CMD_PUSH)[0]['sn'], 40)
        self.send(40, cmd=b.KCP_CMD_ACK, una=42)
        self.assertEqual(self.st['rcv_nxt'], 1)
        self.assertFalse(self.st['send_buf'])
        self.send(1, self.frame(b.OP_PING, b._u64(1000)), una=42)
        self.assertEqual(self.st['rcv_nxt'], 2)
        self.assertEqual(self.packets(op=b.OP_PONG)[-1]['sn'], 42)

    def test_future_una_cannot_discard_unsent_sequence(self):
        self.send(0, self.ready())
        self.send(999, cmd=b.KCP_CMD_ACK, una=999)
        self.assertEqual(len(self.st['send_buf']), 2)
        self.assertEqual(self.st['rcv_nxt'], 1)

    def test_piggyback_ack_retires_pending_sends(self):
        self.send(0, self.ready())
        self.send(1, self.frame(123), una=2)
        self.assertFalse(self.st['send_buf'])

    def test_same_ready_as_new_application_request_does_not_restart(self):
        self.send(0, self.ready())
        self.send(1, self.ready(rpc=7))
        self.assertEqual(len(self.unique_events(b.OP_START_MATCH)), 1)
        self.assertEqual([p['frame']['rpc_id'] for p in self.packets(op=b.OP_ACK_PLAYER_READY)], [6, 7])

    def test_duplicate_result_registers_once_and_preserves_first_result(self):
        self.send(0, self.ready())
        self.send(1, self.result())
        self.send(1, self.result(), ts=200)
        self.send(2, self.result(result=2, rpc=10))
        self.assertEqual(self.results, [(123456, 1)])
        self.assertEqual(self.srv.get_result(123456)['result'], 1)
        self.assertEqual(len(self.unique_events(b.OP_ACK_BATTLE_GS_RESULT)), 1)
        self.assertEqual([p['frame']['rpc_id'] for p in self.packets(op=b.OP_ACK_BATTLE_RESULT)], [9, 10])

    def test_late_ready_after_result_does_not_reopen_battle(self):
        self.send(0, self.ready())
        self.send(1, self.result())
        count = len(self.packets(op=b.OP_START_MATCH))
        self.send(2, self.ready(rpc=10))
        self.assertFalse(self.st['started'])
        self.assertEqual(len(self.packets(op=b.OP_START_MATCH)), count)

    def test_duplicate_syn_keeps_session_and_pending_replies(self):
        self.send(0, self.ready())
        syn = bytes([b.SYN]) + struct.pack('<II', 101, 0)
        self.srv._handle_pkt(syn, ('127.0.0.1', 30002))
        self.assertIs(self.srv._conns[101], self.st)
        self.assertEqual(self.st['conn'], 202)
        self.assertEqual(self.st['rcv_nxt'], 1)
        self.assertEqual(len(self.st['send_buf']), 2)
        self.srv._retransmit_pending(max(p['retry_at'] for p in self.st['send_buf'].values()))
        self.assertEqual(self.sock.packets[-1][1], ('127.0.0.1', 30002))

    def test_fin_cancels_retries_and_next_scene_starts_independently(self):
        for cid, bid in ((101, 123456), (102, 123457), (103, 123458)):
            if cid != 101:
                self.st = self.srv._new_session(cid, cid + 100, self.addr)
            self.send(0, self.ready(bid=bid))
            self.send(0, self.ready(bid=bid), ts=200)
            self.assertTrue(self.st['started'])
            self.send(1, self.result(bid=bid))
            self.srv._handle_pkt(bytes([b.FIN]) + struct.pack('<I', cid), self.addr)
            count = len(self.sock.packets)
            self.srv._retransmit_pending(10**15)
            self.assertEqual(len(self.sock.packets), count)
        self.assertEqual(self.results, [(123456, 1), (123457, 1), (123458, 1)])
        self.assertEqual(len(self.packets(op=b.OP_START_MATCH)), 3)

    def test_repeated_handshake_ack_does_not_reset_session(self):
        self.send(0, self.ready())
        self.srv._handle_pkt(bytes([b.ACK]) + struct.pack('<II', 202, 101), self.addr)
        self.assertIs(self.srv._conns[101], self.st)
        self.assertEqual(self.st['rcv_nxt'], 1)
        self.send(0, self.ready())
        self.assertEqual(len(self.packets(op=b.OP_START_MATCH)), 1)

    def test_receive_sequence_wraparound_and_duplicate(self):
        self.st['rcv_nxt'] = 0xFFFFFFFF
        self.send(0, self.ready())
        self.send(0xFFFFFFFF, self.frame(b.OP_PING, b._u64(1000)))
        self.send(0xFFFFFFFF, self.frame(b.OP_PING, b._u64(1000)))
        self.assertEqual(self.st['rcv_nxt'], 1)
        self.assertEqual(len(self.packets(op=b.OP_PONG)), 1)
        self.assertEqual(len(self.packets(op=b.OP_START_MATCH)), 1)

    def test_send_ack_wraparound(self):
        self.send(0, self.ready(), una=0xFFFFFFFF)
        self.assertEqual([p['sn'] for p in self.packets(cmd=b.KCP_CMD_PUSH)], [0xFFFFFFFF, 0])
        self.send(0, cmd=b.KCP_CMD_ACK, una=1)
        self.assertFalse(self.st['send_buf'])

    def test_outside_receive_window_is_not_acknowledged(self):
        self.send(b.KCP_RCV_WINDOW, self.ready())
        self.assertFalse(self.sock.packets)
        self.assertFalse(self.st['recv_buf'])

    def test_window_probe_answers_wins_not_fake_ack(self):
        self.send(0, cmd=b.KCP_CMD_WASK)
        self.assertEqual(len(self.packets(cmd=b.KCP_CMD_WINS)), 1)
        self.assertFalse(self.packets(cmd=b.KCP_CMD_ACK))
        self.send(0, cmd=b.KCP_CMD_WINS)
        self.assertEqual(len(self.sock.packets), 1)

    def test_stale_conversation_cannot_start_new_session(self):
        self.send(0, self.ready(), conv=999)
        self.assertFalse(self.sock.packets)
        self.assertEqual(self.st['rcv_nxt'], 0)

    def test_random_prefix_and_multiple_segments(self):
        prefix = bytes([b.MSG]) + struct.pack('<I', 101) + b'\xaa\xbb'
        self.srv._handle_pkt(prefix + self.segment(0, self.frame(b.OP_PING, b._u64(1000)))
                             + self.segment(1, self.ready()), self.addr)
        self.assertEqual(self.st['rcv_nxt'], 2)
        self.assertEqual(len(self.packets(op=b.OP_START_MATCH)), 1)


class LoopbackBattleTests(unittest.TestCase):
    def test_udp_run_loop_retries_lost_start_with_original_sequence(self):
        srv = b.BattleServer(port=0, bind_host='127.0.0.1')
        addr = srv.sock.getsockname()
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(2)
        thread = threading.Thread(target=srv.run, daemon=True)
        thread.start()
        try:
            client.sendto(bytes([b.SYN]) + struct.pack('<II', 101, 0), addr)
            handshake = client.recv(65535)
            self.assertEqual(handshake[0], b.ACK)
            conv = struct.unpack_from('<I', handshake, 1)[0]
            frame = b.xs_encode_frame(b.OP_PLAYER_READY,
                                     b._f_varint2(1, 105) + b._f_varint2(2, 123456), 1, 6)
            packet = bytes([b.MSG]) + struct.pack('<I', 101)
            def segment(sn, data, frg=0):
                return struct.pack('<IBBHIIII', conv, b.KCP_CMD_PUSH, frg, 256,
                                   100, sn, 0, len(data)) + data
            # Reorder and duplicate Ready's two fragments on a real UDP socket.
            client.sendto(packet + segment(1, frame[8:]), addr)
            client.sendto(packet + segment(0, frame[:8], frg=1), addr)
            client.sendto(packet + segment(0, frame[:8], frg=1), addr)
            client.sendto(packet + segment(1, frame[8:]), addr)
            starts = []
            deadline = time.monotonic() + 3
            while len(starts) < 2 and time.monotonic() < deadline:
                reply = client.recv(65535)
                if reply[0] == b.MSG and reply[9] == b.KCP_CMD_PUSH:
                    f = b.xs_parse_frame(reply[29:])
                    if f['opcode'] == b.OP_START_MATCH:
                        starts.append((struct.unpack_from('<I', reply, 17)[0], reply[29:]))
            self.assertEqual(len(starts), 2)
            self.assertEqual(starts[0], starts[1])
            self.assertTrue(thread.is_alive())
        finally:
            srv.stop()
            client.close()
            thread.join(timeout=1)
            srv.sock.close()


if __name__ == '__main__':
    unittest.main()
