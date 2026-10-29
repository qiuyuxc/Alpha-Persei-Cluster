"""A03/A308: reply preparation, final packing, and transactional terminal replies."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import random
import shutil
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SEED = Path(os.environ['GM_TEST_DB_SEED']).resolve()
SANDBOX = tempfile.TemporaryDirectory(prefix='ag-reply-tests-')
BOOT = Path(SANDBOX.name) / 'bootstrap.db'
shutil.copyfile(SEED, BOOT)
os.environ['ACCOUNT_DB_PATH'] = str(BOOT)

from account_db import AccountDB
from auth_service import get_service
from codec import decode, encode
import core
from core import Core, ConfigOperation, Operation
import event_bus
import generator
from gm_actions import context
from inventory_service import InventoryService
from mail_service import MailService
import middleware
from middleware import CSRequest, CSResponse, DownFrame
import operations
import server_net
import social_protocol
import social_service
import transport
from transport import FrameParser, pack_up, unpack_down

UID, OTHER = 2174928301, 264084870
TABLES = ('users', 'currency', 'material', 'hero', 'hero_piece', 'equip', 'servant', 'mail')


class MarkerOp(Operation):
    cmd, sc = 15020, 15021

    def apply(self, data, ctx):
        ctx.db.set_currency(self.uid, 38, InventoryService.get_item_balance(ctx.db, self.uid, 38)[1] + 5)
        ctx.touched_items.add(38)
        return {}

    def respond(self, result, data, ctx):
        return [DownFrame(self.sc, ctx.codec_encode('sc_15021', {'result': 0}))]


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=SANDBOX.name)
        self.addCleanup(self.folder.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(event_bus.EventBus, '_handlers', {}))
        self.stack.enter_context(patch('event_bus.bus.emit'))
        self.stack.enter_context(patch('game_cycles.apply_rotation'))
        self.stack.enter_context(patch('lazy_timer.lazy_timer.pulse'))
        self.stack.enter_context(patch('lazy_timer.lazy_timer.pulse_heartbeat'))
        self.stack.enter_context(patch('lazy_timer.lazy_timer.on_disconnect'))
        self.stack.enter_context(patch.object(MailService, '_instance', None))
        self.stack.enter_context(patch.object(server_net, 'register_game_conn'))
        self.stack.enter_context(patch.object(server_net, 'unregister_game_conn'))
        self.stack.enter_context(patch('server_net.get_online_uids', return_value=[]))
        self.path = Path(self.folder.name) / 'player.db'
        shutil.copyfile(SEED, self.path)
        self.db = AccountDB(str(self.path))
        self.addCleanup(self.db.close)
        social_service.initialize(self.db)
        self.ctx = context(self.db)
        self.ctx.uid = UID
        self.ctx.log = lambda *a, **kw: None
        self.db.set_material(UID, 22001, 2)
        self.db.set_currency(UID, 31, 100)
        self.db.set_currency(UID, 32, 0)
        self.other = self.state(OTHER)
        self.engine = Core(db=self.db, generator=generator, codec_encode=encode,
                           codec_decode=decode, log=lambda *a, **kw: None)

    def tearDown(self):
        self.assertEqual(self.state(OTHER), self.other)
        self.assertEqual(self.db.query('PRAGMA integrity_check')[0]['integrity_check'], 'ok')
        with sqlite3.connect('file:' + str(self.path) + '?mode=ro', uri=True) as saved:
            saved.row_factory = sqlite3.Row
            actual = {table: sorted([dict(r) for r in saved.execute(
                'SELECT * FROM ' + table + ' WHERE uid=?', (UID,))],
                key=lambda row: json.dumps(row, sort_keys=True)) for table in TABLES}
        self.assertEqual(actual, self.state())

    def state(self, uid=UID):
        return {table: sorted(self.db.query('SELECT * FROM ' + table + ' WHERE uid=?', (uid,)),
                              key=lambda row: json.dumps(row, sort_keys=True)) for table in TABLES}

    def balance(self, iid):
        return InventoryService.get_item_balance(self.db, UID, iid)[1]

    def run_op(self, op=None, data=None, packer=None):
        op = op or MarkerOp()
        req = CSRequest(op.cmd, encode('cs_' + str(op.cmd), data or {'num': 1}),
                        index=51, server_idx=65000, uid=UID)
        if packer is not None:
            req.frame_packer = packer
        return self.engine._run_operation(op, req, self.ctx)

    def ack(self, response, sc=15021):
        return decode(next(f['payload'] for f in response.frames if f['sc'] == sc), 'sc_' + str(sc))

    def use(self):
        return self.run_op(operations.UseItemOp(), {'use_item_list': [
            {'item_info': {'id': 22001, 'num': 1, 'time_valid': 0}, 'use_list': []}]})

    def test_item_use_reply_exception_restores_cost_reward_and_allows_one_retry(self):
        before = self.state()
        original = self.ctx.codec_encode
        with patch.object(self.ctx, 'codec_encode', side_effect=lambda n, d:
                          (_ for _ in ()).throw(ValueError('reply fault')) if n == 'sc_17013' else original(n, d)):
            response = self.use()
        self.assertNotEqual(self.ack(response, 17013)['result'], 0)
        self.assertEqual(self.state(), before)
        gold = self.balance(2)
        self.assertEqual(self.ack(self.use(), 17013)['result'], 0)
        self.assertEqual(self.balance(22001), 1)
        self.assertEqual(self.balance(2) - gold, 1000)

    def test_empty_required_item_reply_cannot_be_replaced_by_success(self):
        before = self.state()
        original = self.ctx.codec_encode
        with patch.object(self.ctx, 'codec_encode', side_effect=lambda n, d: b'' if n == 'sc_17013' else original(n, d)):
            response = self.use()
        self.assertNotEqual(self.ack(response, 17013)['result'], 0)
        self.assertEqual(self.state(), before)

    def test_currency_exchange_reply_failure_rolls_back_both_assets(self):
        before = self.state()
        original = self.ctx.codec_encode
        with patch.object(self.ctx, 'codec_encode', side_effect=lambda n, d:
                          (_ for _ in ()).throw(ValueError('reply fault')) if n == 'sc_15021' else original(n, d)):
            response = self.run_op(operations.CurrencyBuyDiamondOp(), {'num': 10})
        self.assertNotEqual(self.ack(response)['result'], 0)
        self.assertEqual(self.state(), before)

    def test_configuration_operation_cannot_hide_a_caught_encoding_error(self):
        op = ConfigOperation(15020, {'sc': 15021, 'apply': [
            {'op': 'set_material', 'material_id': 22001, 'num': 'req.num'}],
            'respond': {'template': {'result': 0}}})
        before = self.state()
        with patch.object(self.ctx, 'codec_encode', side_effect=ValueError('caught encoder fault')):
            response = self.run_op(op, {'num': 7})
        self.assertNotEqual(self.ack(response)['result'], 0)
        self.assertEqual(self.state(), before)

    def test_required_asset_refresh_failure_restores_item_consumption(self):
        before = self.state()
        original = generator.gen_payload
        with patch.object(generator, 'gen_payload', side_effect=lambda cmd, *a, **kw:
                          b'' if cmd == 17023 else original(cmd, *a, **kw)):
            response = self.use()
        self.assertNotEqual(self.ack(response, 17013)['result'], 0)
        self.assertEqual(self.state(), before)

    def test_missing_required_ack_restores_business_state(self):
        before = self.state()
        with patch.object(MarkerOp, 'respond', return_value=[]):
            response = self.run_op()
        self.assertNotEqual(self.ack(response)['result'], 0)
        self.assertEqual(self.state(), before)

    def test_duplicate_required_ack_restores_business_state(self):
        before = self.state()
        payload = encode('sc_15021', {'result': 0})
        with patch.object(MarkerOp, 'respond', return_value=[DownFrame(15021, payload), DownFrame(15021, payload)]):
            response = self.run_op()
        self.assertNotEqual(self.ack(response)['result'], 0)
        self.assertEqual(self.state(), before)

    def test_one_way_push_commits_without_fabricating_an_ack(self):
        response = self.engine._run_operation(operations.RedpointReportOp(),
            CSRequest(56002, {'id': 33}, uid=UID), self.ctx)
        self.assertFalse(response.reply)
        self.assertEqual(response.frames, [])
        self.assertEqual(getattr(response, 'wire_bytes', None), b'')

    def test_all_registered_operations_share_the_precommit_contract(self):
        self.assertGreaterEqual(len(core.OPERATIONS), 327)
        self.assertTrue(all(op.prepare_response_in_transaction for op in core.OPERATIONS.values()))
        exempt = {cmd for cmd, op in core.OPERATIONS.items() if not getattr(op, 'requires_response', True)}
        self.assertEqual(exempt, {32132, 56002})

    def test_caught_nested_apply_failure_does_not_commit_the_failed_operation(self):
        initial = self.balance(38)
        with self.db.transaction():
            self.db.set_currency(UID, 2, self.balance(2) + 1)
            caller = self.state()
            original = MarkerOp.apply

            def fail(op, data, ctx):
                original(op, data, ctx)
                raise RuntimeError('apply fault')

            with patch.object(MarkerOp, 'apply', fail):
                response = self.run_op()
            self.assertNotEqual(self.ack(response)['result'], 0)
            self.assertEqual(self.state(), caller)
        self.assertEqual(self.balance(38), initial)

    def test_caught_nested_reply_failure_preserves_the_callers_changes(self):
        with self.db.transaction():
            self.db.set_currency(UID, 2, self.balance(2) + 1)
            caller = self.state()
            self.ctx.touched_items.add(2)
            with patch.object(MarkerOp, 'respond', side_effect=ValueError('reply fault')):
                response = self.run_op()
            self.assertNotEqual(self.ack(response)['result'], 0)
            self.assertEqual(self.state(), caller)
            self.assertEqual(self.ctx.touched_items, {2})

    def test_legacy_handler_exception_rolls_back_before_a_terminal_reply(self):
        before = self.state()

        def legacy(req, ctx):
            ctx.db.set_currency(req.uid, 38, self.balance(38) + 5)
            raise RuntimeError('legacy handler failed after SQL')

        with patch.dict(middleware.CORE_HANDLERS, {49998: legacy}):
            response = self.engine.dispatch(core.CoreRequest(49998, uid=UID))
        self.assertEqual(self.state(), before)
        self.assertTrue(response.reply or getattr(response, 'close_connection', False))

    def test_main_frame_packing_failure_rolls_back_before_error_ack(self):
        before = self.state()
        calls = []

        def fail(cmd, *a, **kw):
            calls.append(self.db._in_txn())
            raise ValueError('packing fault')

        response = self.run_op(packer=fail)
        self.assertEqual(calls, [True])
        self.assertEqual(self.state(), before)
        self.assertNotEqual(self.ack(response)['result'], 0)
        self.assertTrue(getattr(response, 'wire_bytes', None))

    def test_secondary_frame_packing_failure_discards_the_entire_success_batch(self):
        before = self.state()
        original = MarkerOp.respond

        def with_extra(op, result, data, ctx):
            return original(op, result, data, ctx) + [DownFrame(73011, encode('sc_73011', {'result': 0}))]

        def fail(cmd, *a, **kw):
            if cmd == 73011:
                raise ValueError('secondary packing fault')
            return transport.pack_down(cmd, *a, **kw)

        with patch.object(MarkerOp, 'respond', with_extra):
            response = self.run_op(packer=fail)
        self.assertEqual(self.state(), before)
        self.assertEqual([f['sc'] for f in response.frames], [15021])
        self.assertNotEqual(self.ack(response)['result'], 0)

    def test_oversized_incompressible_frame_restores_business_state(self):
        before = self.state()
        huge = random.Random(17).randbytes(70000)
        with patch.object(MarkerOp, 'respond', return_value=[
                DownFrame(15021, encode('sc_15021', {'result': 0})), DownFrame(73011, huge)]):
            response = self.run_op()
        self.assertNotEqual(self.ack(response)['result'], 0)
        self.assertEqual(self.state(), before)

    def test_prepared_bytes_preserve_compression_high_commands_and_sequences(self):
        payload = encode('sc_15021', {'result': 0, 'reward_list': [{'id': 38, 'num': 1}] * 2500})
        with patch.object(MarkerOp, 'respond', return_value=[DownFrame(15021, payload),
                DownFrame(73011, encode('sc_73011', {'result': 0}), srv=77)]):
            response = self.run_op()
        raw = getattr(response, 'wire_bytes', None)
        self.assertTrue(raw)
        frames = [unpack_down(f) for f in FrameParser('down').feed(raw)]
        self.assertTrue(frames[0]['compressed'])
        self.assertEqual((frames[0]['index'], frames[0]['server_idx']), (51, 65001))
        self.assertEqual((frames[1]['cmd'], frames[1]['flag'], frames[1]['server_idx']), (73011, 1, 77))
        self.assertEqual(len(decode(frames[0]['payload'], 'sc_15021')['reward_list']), 2500)

    def test_no_result_schema_failure_closes_instead_of_forging_a_payload(self):
        op = MarkerOp()
        op.sc = 32009
        before = self.state()
        with patch.object(MarkerOp, 'respond', side_effect=ValueError('data reply fault')):
            response = self.run_op(op)
        self.assertEqual(self.state(), before)
        self.assertTrue(getattr(response, 'close_connection', False))
        self.assertFalse(response.reply)

    def network(self, cmd, data, *, packer=None, dispatch=None, send_failure=False):
        auth = get_service(self.db)
        setup = auth.bootstrap_token(UID)
        auth.create('900099', 'ReplyTestPassword42', setup)
        token = auth.issue(UID, 'game')
        login = {'channel_id': 9, 'server_id': 2, 'account': '900099', 'user_id': UID,
                 'timestamp': 1793238000, 'gstoken': token, 'type': 1}

        class Socket:
            def __init__(self):
                self.input = [pack_up(10042, encode('cs_10042', login)) +
                              pack_up(cmd, encode('cs_' + str(cmd), data), index=73, server_idx=400), b'']
                self.output = []

            def settimeout(self, value):
                pass

            def recv(self, size):
                return self.input.pop(0)

            def sendall(self, payload):
                if send_failure and self.output:
                    raise BrokenPipeError('injected socket write failure')
                self.output.append(payload)

        sock = Socket()
        handler = object.__new__(server_net.GameHandler)
        handler.client_address, handler.request = ('127.0.0.1', 12345), sock
        engine = self.engine if dispatch is None else SimpleNamespace(dispatch=dispatch, log=lambda *a, **kw: None)
        handler.server = SimpleNamespace(core=engine, db=self.db, generator=generator,
            codec_encode=encode, codec_decode=decode, host_ip='127.0.0.1', game_port=8105, frame_delay=0)
        with patch.object(server_net, 'pack_down', packer or transport.pack_down):
            handler.handle()
        return [unpack_down(f) for f in FrameParser('down').feed(b''.join(sock.output))]

    def mail(self):
        return MailService.get_instance(self.db).send_mail(UID, '回执测试', '隔离副本', [{'id': 38, 'number': 7}], db=self.db)

    def test_network_uses_the_once_prepared_packet_from_inside_transaction(self):
        mid = self.mail()
        seen = []

        def pack(cmd, *a, **kw):
            if cmd == 30005:
                seen.append(self.db._in_txn())
            return transport.pack_down(cmd, *a, **kw)

        frames = self.network(30004, {'id': mid}, packer=pack)
        self.assertEqual(seen, [True])
        ack = next(f for f in frames if f['cmd'] == 30005)
        self.assertEqual(decode(ack['payload'], 'sc_30005')['result'], 0)
        self.assertEqual(ack['index'], 73)

    def test_network_required_pack_failure_returns_error_and_restores_mail(self):
        mid = self.mail()
        before = self.state()

        def fail(cmd, *a, **kw):
            if cmd == 30005:
                raise ValueError('mail ack packing fault')
            return transport.pack_down(cmd, *a, **kw)

        frames = self.network(30004, {'id': mid}, packer=fail)
        ack = next(f for f in frames if f['cmd'] == 30005)
        self.assertNotEqual(decode(ack['payload'], 'sc_30005')['result'], 0)
        self.assertEqual(self.state(), before)

    def test_social_packing_failure_restores_room_change(self):
        self.db.execute('INSERT INTO social_rooms VALUES(?,1) ON CONFLICT(uid) DO UPDATE SET room_id=1', (UID,))

        def fail(cmd, *a, **kw):
            if cmd == 27011:
                raise ValueError('social ack packing fault')
            return transport.pack_down(cmd, *a, **kw)

        frames = self.network(27010, {'room_id': 2}, packer=fail)
        self.assertEqual(social_service.room(self.db, UID), 1)
        ack = next(f for f in frames if f['cmd'] == 27011)
        self.assertNotEqual(decode(ack['payload'], 'sc_27011')['result'], 0)

    def test_social_publish_stays_after_commit_while_reply_packing_moves_before_it(self):
        packed, published = [], []

        def pack(cmd, *a, **kw):
            if cmd == 27015:
                packed.append(self.db._in_txn())
            return transport.pack_down(cmd, *a, **kw)

        with patch.object(social_protocol, 'publish', side_effect=lambda *a, **kw: published.append(self.db._in_txn())):
            self.network(27014, {'content': '测试回执', 'type': 1}, packer=pack)
        self.assertEqual(packed, [True])
        self.assertEqual(published, [False])

    def test_socket_write_failure_keeps_committed_mail_without_replaying_award(self):
        mid = self.mail()
        before = self.balance(38)
        self.network(30004, {'id': mid}, send_failure=True)
        self.assertEqual(self.balance(38) - before, 7)
        self.assertEqual(self.db.query('SELECT attach_flag FROM mail WHERE uid=? AND mail_id=?', (UID, mid))[0]['attach_flag'], 2)

    def test_unexpected_dispatch_failure_gets_a_terminal_ack(self):
        def fail(*args):
            raise RuntimeError('dispatch failure')

        frames = self.network(15020, {'num': 10}, dispatch=fail)
        ack = next(f for f in frames if f['cmd'] == 15021)
        self.assertNotEqual(decode(ack['payload'], 'sc_15021')['result'], 0)

    def test_unencodable_error_closes_connection_and_leaves_no_business_write(self):
        before = self.state()
        with patch.object(transport, 'pack_down', side_effect=ValueError('all packing unavailable')):
            response = self.run_op()
        self.assertEqual(self.state(), before)
        self.assertTrue(getattr(response, 'close_connection', False))
        self.assertFalse(response.reply)

    def test_background_encoder_failure_does_not_abort_foreground_business(self):
        owner = threading.get_ident()
        original_encoder = self.ctx.codec_encode
        before = self.balance(38)
        background = []

        def encoder(name, data):
            if threading.get_ident() != owner:
                raise ValueError('independent background encoder failure')
            return original_encoder(name, data)

        original_respond = MarkerOp.respond

        def respond(op, result, data, ctx):
            def worker():
                try:
                    ctx.codec_encode('sc_15021', {'result': 0})
                except ValueError:
                    background.append('failed')
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            return original_respond(op, result, data, ctx)

        with patch.object(self.ctx, 'codec_encode', encoder), patch.object(MarkerOp, 'respond', respond):
            response = self.run_op()
        self.assertEqual(background, ['failed'])
        self.assertEqual(self.ack(response)['result'], 0)
        self.assertEqual(self.balance(38) - before, 5)

    def test_async_ai_handler_keeps_its_independent_scheduling_boundary(self):
        seen = []

        def handler(req, ctx):
            seen.append((self.db._in_txn(), ctx.codec_encode is encode))
            return CSResponse([], reply=False)

        with patch.dict(middleware.CORE_HANDLERS, {19034: handler}):
            response = self.engine.dispatch(core.CoreRequest(19034, uid=UID))
        self.assertEqual(seen, [(False, True)])
        self.assertFalse(response.reply)


if __name__ == '__main__':
    unittest.main()
