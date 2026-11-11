"""Controlled native-host tests; no registration doubles or runtime patches."""
# Native entry contract: activate the selected dependency generation before any
# host dependency imports, including after a supported bootstrap re-exec.
import hermes_bootstrap

import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from hermes_cli.plugins import PluginManager
from gateway.config import PlatformConfig
from gateway.platform_registry import platform_registry
from gateway.relay import register_relay_adapter

SILENCE = "⚠️ The model returned only a silence marker for a message that needed a reply. Try again or rephrase."
EFFORT = "⚠ auxiliary.background_review.reasoning_effort='high' has no effect while the review model is not the main model"
PLUGIN = Path(__file__).resolve().parents[1]


class Recorder:
    def __init__(self):
        self.calls = []
        self.platforms = []

    async def chat_postMessage(self, **kwargs):
        self.calls.append(("send", kwargs))
        return {"ok": True, "ts": "123"}

    async def chat_update(self, **kwargs):
        self.calls.append(("edit", kwargs))
        return {"ok": True, "ts": "123"}

    async def chat_startStream(self, **kwargs):
        self.calls.append(("draft", kwargs))
        return {"ok": True, "ts": "123"}

    async def chat_stopStream(self, **kwargs):
        self.calls.append(("finalize", kwargs))
        return {"ok": True}

    def set_inbound_handler(self, handler):
        self.inbound = handler

    async def connect(self):
        return True

    async def handshake(self):
        return self.descriptor

    async def disconnect(self):
        self.disconnected = True

    async def send_outbound(self, action, *, platform=None):
        self.platforms.append(platform)
        op = "finalize" if action.get("final") else action["op"]
        self.calls.append((op, action))
        return {"success": True, "message_id": "123"}


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="warning-filter-test-", dir=os.environ["TMPDIR"])
        self.home = Path(self.tmp.name)
        self.addCleanup(self._cleanup_home)
        # The ledger deliberately ignores context-local profile overrides. Bind
        # its process home too, so even recovery touches only this disposable DB.
        self.process_home = os.environ.get('HERMES_HOME')
        self.relay_url_env = os.environ.get('GATEWAY_RELAY_URL')
        os.environ['HERMES_HOME'] = str(self.home)
        os.environ['GATEWAY_RELAY_URL'] = 'wss://connector.invalid/relay'
        self.token = set_hermes_home_override(self.home)
        self.addCleanup(self._restore_home)
        shutil.copytree(PLUGIN, self.home / "plugins" / "warning-filter",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (self.home / "config.yaml").write_text(json.dumps({"plugins": {"enabled": ["warning-filter"]}}))
        self.manager = PluginManager(str(self.home))
        self.addCleanup(self.manager.unload)
        self.manager.discover_and_load()
        # Same order as gateway startup: discovery, THEN native core Relay registration.
        self.assertTrue(register_relay_adapter())

    def _restore_home(self):
        reset_hermes_home_override(self.token)
        if self.relay_url_env is None:
            os.environ.pop('GATEWAY_RELAY_URL', None)
        else:
            os.environ['GATEWAY_RELAY_URL'] = self.relay_url_env
        if self.process_home is None:
            os.environ.pop('HERMES_HOME', None)
        else:
            os.environ['HERMES_HOME'] = self.process_home

    def _cleanup_home(self):
        self.tmp.cleanup()
        self.assertFalse(self.home.exists())

    def test_plain_directory_resolver_and_native_drop_in(self):
        from hermes_cli.plugins_cmd import _resolve_git_url
        resolved = _resolve_git_url('/example/author/project/warning-filter')
        self.assertEqual(resolved, ('https://github.com/example/author.git',
                                    'project/warning-filter'))
        for name in ('slack', 'relay'):
            self.assertEqual(platform_registry.get(name).plugin_name, 'warning-filter')
        print('INSTALL INTERPRETATION', resolved, 'ISOLATED DROP-IN', self.home, flush=True)

    def test_default_final_ledger_recovery(self):
        from gateway.config import Platform
        from gateway.platforms.event import MessageEvent
        from gateway.session import SessionSource
        from gateway.run_startup import GatewayStartupMixin
        from gateway.delivery_ledger import ledger_enabled, pending_retries, sweep_failed_for_runtime, RECONNECTED_MARKER
        import time

        class ControlledRunner(GatewayStartupMixin):
            def __init__(self, adapter):
                self.adapter = adapter
                self.adapters = {Platform.SLACK: adapter}
                self.scheduled = []
                self.cleared = []

            def _authorization_adapter(self, platform, profile):
                return self.adapters.get(platform)

            def _schedule_flood_redelivery(self, platform, *, profile=None):
                self.scheduled.append((str(platform), profile))

            async def _clear_durable_active_turn(self, event):
                self.cleared.append(event.message_id)

        self.assertTrue(ledger_enabled(), 'default-on ledger, no gate override')
        adapter, io = self.controlled_adapter('slack')
        runner = ControlledRunner(adapter)
        adapter.gateway_runner = runner
        event = MessageEvent('question', source=SessionSource(Platform.SLACK, 'C_TEST', thread_id='9'), message_id='ledger-warning')
        event._turn_marker_handoff = True
        event._gateway_active_turn_token = 'owned-test-turn'
        event._gateway_marker_released = asyncio.Event()

        async def exercise():
            result, owner = await adapter.send_final_ledgered(event, 'ledger-test', SILENCE, {'thread_id': '9'}, reply_to='9')
            waiting = pending_retries()
            claimed = sweep_failed_for_runtime('slack', now=time.time() + 120)
            recovered = await runner._redeliver_claimed_obligations(claimed)
            print('LEDGER RECEIPT', result, 'WAITING', waiting, 'CLAIMED', claimed,
                  'SCHEDULED', runner.scheduled, 'RECOVERED', recovered, 'TRANSPORT', io.calls, flush=True)
            self.assertFalse(result.success)
            self.assertIsNone(result.message_id)
            self.assertEqual(result.raw_response, {'filtered': True, 'delivered': False})
            self.assertEqual(io.calls, [], 'suppressed final must never reach native recovery transport')
            self.assertEqual(waiting, [])
            self.assertEqual(claimed, [])
            self.assertEqual(runner.scheduled, [])
            self.assertEqual(runner.cleared, ['ledger-warning'])
            self.assertTrue(event._gateway_marker_released.is_set())
        asyncio.run(exercise())

    def test_final_ledger_controls(self):
        self.configure_patterns(self.default_patterns() + [r'Custom maintenance warning: retry in \d+ seconds\.'])
        from dataclasses import replace
        from gateway.config import Platform
        from gateway.platforms.event import MessageEvent
        from gateway.session import SessionSource
        from gateway.run_startup import GatewayStartupMixin
        from gateway.delivery_ledger import ledger_enabled, pending_retries, sweep_failed_for_runtime, RECONNECTED_MARKER
        from hermes_constants import get_process_hermes_home
        import sqlite3
        import time

        class Runner(GatewayStartupMixin):
            def __init__(self, current):
                self.current = current
                self.adapters = {Platform.SLACK: current}
                self.scheduled = []
                self.cleared = []
            def _delivery_adapter_for(self, source):
                return self.current
            def _authorization_adapter(self, platform, profile):
                return self.current
            def _schedule_flood_redelivery(self, platform, *, profile=None):
                self.scheduled.append((platform, profile))
            async def _clear_durable_active_turn(self, event):
                self.cleared.append(event.message_id)

        self.assertEqual(get_process_hermes_home(), self.home)
        self.assertTrue(ledger_enabled())
        def rows():
            db = self.home / 'state.db'
            if not db.exists():
                return []
            from contextlib import closing
            with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
                return conn.execute('SELECT content,state FROM delivery_obligations ORDER BY obligation_id').fetchall()
        def event(ref):
            e = MessageEvent('question', source=SessionSource(Platform.SLACK, 'C_TEST', thread_id='9'), message_id=ref)
            e._turn_marker_handoff = True
            e._gateway_active_turn_token = ref
            e._gateway_marker_released = asyncio.Event()
            return e
        async def recover(runner):
            claims = sweep_failed_for_runtime('slack', now=time.time() + 120)
            count = await runner._redeliver_claimed_obligations(claims)
            return claims, count
        async def exercise():
            for name in ('slack', 'relay'):
                old, old_io = self.controlled_adapter(name)
                live, io = self.controlled_adapter(name)
                if name == 'relay':
                    io._identities = [('slack', 'synthetic-bot')]
                    desc = replace(live.descriptor, platform='slack')
                    live = type(live)(live.config, desc, transport=io)
                runner = Runner(live)
                old.gateway_runner = runner
                live.gateway_runner = runner
                md = {'thread_id': '9', 'team_id': 'T_TEST', '_relay_logical_platform': 'slack'}
                before_rows = rows()
                for index, warning in enumerate((SILENCE, EFFORT, 'Custom maintenance warning: retry in 17 seconds.')):
                    e = event(name + '-blocked-' + str(index))
                    result, owner = await old.send_final_ledgered(e, e.message_id, warning, md, reply_to='9')
                    self.assertIs(owner, live)
                    self.assertEqual((result.success, result.message_id, result.retryable), (False, None, False))
                    self.assertEqual(result.error, 'warning-filter: filtered; not delivered')
                    self.assertEqual(result.raw_response, {'filtered': True, 'delivered': False})
                    self.assertTrue(e._gateway_marker_released.is_set())
                    self.assertIn(e.message_id, runner.cleared)
                    self.assertEqual(rows(), before_rows, 'no warning obligation of any state')
                    claims, count = await recover(runner)
                    self.assertEqual((claims, count, pending_retries()), ([], 0, []))
                self.assertEqual((old_io.calls, io.calls, runner.scheduled), ([], [], []))
                for index, text in enumerate(('ordinary final', 'Useful answer\n' + SILENCE, 'Useful answer\n' + EFFORT)):
                    e = event(name + '-ordinary-' + str(index))
                    result, owner = await old.send_final_ledgered(e, e.message_id, text, md, reply_to='9')
                    self.assertTrue(result.success, result)
                    self.assertIs(owner, live)
                    self.assertTrue(e._gateway_marker_released.is_set())
                    self.assertIn((text, 'delivered'), rows())
                self.assertEqual(old_io.calls, [])
                self.assertEqual(len(io.calls), 3)
                if name == 'relay':
                    self.assertEqual(io.platforms, ['slack'] * 3)
                    self.assertEqual(io.calls[0][1]['metadata']['thread_id'], '9')
                    self.assertEqual(io.calls[0][1]['reply_to'], '9')
                else:
                    self.assertEqual(io.calls[0][1]['thread_ts'], '9')
                print('FINAL CONTROLS', name, 'HOME', self.home, 'ROWS', rows(), 'TRANSPORT', io.calls, flush=True)

            class RejectOnce(Recorder):
                async def send_outbound(self, action, *, platform=None):
                    result = await super().send_outbound(action, platform=platform)
                    if len(self.calls) <= 2:
                        return {'success': False, 'error': 'controlled transport rejection', 'retryable': False}
                    return result
            native = platform_registry.create_adapter('relay', PlatformConfig())
            io = RejectOnce()
            live = type(native)(native.config, replace(native.descriptor, platform='slack'), transport=io)
            runner = Runner(live)
            live.gateway_runner = runner
            e = event('real-rejection')
            result, owner = await live.send_final_ledgered(e, e.message_id, 'recover ordinary', {'thread_id': '9'}, reply_to='9')
            self.assertFalse(result.success)
            self.assertIn(('recover ordinary', 'failed'), rows())
            self.assertEqual(len(runner.scheduled), 1)
            self.assertEqual(len(pending_retries()), 1)
            claims, count = await recover(runner)
            self.assertEqual(count, 1)
            self.assertEqual(claims[0]['marker'], RECONNECTED_MARKER)
            self.assertEqual(io.calls[-1][1]['content'], RECONNECTED_MARKER + 'recover ordinary')
            self.assertIn(('recover ordinary', 'delivered'), rows())
            self.assertEqual(pending_retries(), [])
            self.assertTrue(e._gateway_marker_released.is_set())
            print('REAL REJECTION RECOVERY', result, claims, io.calls, flush=True)
        asyncio.run(exercise())

    def test_stream_authoritative_final_warning_and_cleanup(self):
        from dataclasses import replace
        from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
        from gateway.config import Platform
        from gateway.platforms.event import MessageEvent
        from gateway.session import SessionSource
        from gateway.delivery_ledger import pending_retries
        for name in ('slack', 'relay'):
            adapter, io = self.controlled_adapter(name)
            if name == 'relay':
                desc = replace(adapter.descriptor, platform='slack', supports_draft_streaming=True,
                               supported_ops=('send', 'edit', 'draft'))
                adapter = type(adapter)(adapter.config, desc, transport=io)
            md = {'thread_id': '9'}
            async def completed():
                for warning in (SILENCE, EFFORT):
                    consumer = GatewayStreamConsumer(adapter, 'C_TEST',
                        config=StreamConsumerConfig(transport='draft', cursor=''), metadata=md)
                    consumer.finish(warning)
                    await asyncio.wait_for(consumer.run(), timeout=2)
                    self.assertFalse(consumer.delivered_final_matches(warning))
                    self.assertEqual(io.calls, [])
                consumer = GatewayStreamConsumer(adapter, 'C_TEST',
                    config=StreamConsumerConfig(transport='draft', cursor=''), metadata=md)
                consumer.on_delta('kept real content')
                consumer.finish('kept real content complete')
                await asyncio.wait_for(consumer.run(), timeout=2)
                self.assertTrue(consumer.delivered_final_matches('kept real content complete'))
                self.assertEqual(len(io.calls), 1)
                payload = io.calls[-1][1]
                self.assertEqual(payload.get('content', payload.get('text')), 'kept real content complete')
                print('STREAM FINAL', name, 'WARNINGS not delivered', 'ORDINARY', io.calls, flush=True)
            asyncio.run(completed())

    def test_01_native_registration_and_construction(self):
        for name in ("slack", "relay"):
            entry = platform_registry.get(name)
            self.assertEqual(entry.plugin_name, "warning-filter")
            adapter = platform_registry.create_adapter(name, PlatformConfig())
            self.assertIsNotNone(adapter)
            self.assertTrue(any(c.__name__ == "WarningFilter" for c in type(adapter).__mro__))
            print("NATIVE REGISTRATION", name, type(adapter).__qualname__, entry.source)

    def controlled_adapter(self, name):
        native = platform_registry.create_adapter(name, PlatformConfig())
        recorder = Recorder()
        if name == "relay":
            adapter = type(native)(native.config, native.descriptor, transport=recorder)
        else:
            class ControlledSlack(type(native)):
                def __init__(self, config):
                    from types import SimpleNamespace
                    super().__init__(config)
                    self._app = SimpleNamespace(client=recorder)

                def _get_client(self, *args, **kwargs):
                    return recorder

                def _client_for(self, *args, **kwargs):
                    return recorder
            adapter = ControlledSlack(native.config)
        return adapter, recorder

    def test_complete_messages_and_passthrough_at_native_io(self):
        for name in ("slack", "relay"):
            adapter, io = self.controlled_adapter(name)
            for warning in (SILENCE, EFFORT, EFFORT.replace("'high'", "'minimal'"),
                            SILENCE.removeprefix("⚠️ "), EFFORT.removeprefix("⚠ ")):
                for operation in (
                    adapter.send("C_TEST", warning),
                    adapter.edit_message("C_TEST", "123", warning, finalize=True),
                    adapter.send_draft("C_TEST", 1, warning),
                ):
                    result = asyncio.run(operation)
                    self.assertEqual(result.raw_response, {"filtered": True, "delivered": False})
                    self.assertFalse(result.success)
                    self.assertFalse(result.retryable)
            self.assertEqual(io.calls, [])
            for text in ("ordinary", "⚠ unrelated warning", 'Quoted: "' + SILENCE + '"',
                         "Useful answer\n" + EFFORT):
                result = asyncio.run(adapter.send("C_TEST", text, metadata={"thread_id": "9"}))
                self.assertTrue(result.success, (name, result.error))
            self.assertEqual(len(io.calls), 4)
            count = len(io.calls)
            result = asyncio.run(adapter.edit_message("C_TEST", "123", EFFORT, finalize=True))
            self.assertFalse(result.success)
            self.assertEqual(len(io.calls), count, "filtered edit must not blank/delete prior content")
            result = asyncio.run(adapter.edit_message("C_TEST", "123", "kept final", finalize=True))
            self.assertTrue(result.success, result.error)
            self.assertEqual(io.calls[-1][0], "edit")

    def configure_patterns(self, patterns):
        self.manager.unload()
        (self.home / "config.yaml").write_text(json.dumps({"plugins": {
            "enabled": ["warning-filter"], "entries": {"warning-filter": {
                "settings": {"patterns": patterns}}}}}))
        self.manager.discover_and_load(force=True)

    def default_patterns(self):
        from hermes_cli.plugins_manifest import parse_manifest_file
        return parse_manifest_file(PLUGIN / 'plugin.yaml', PLUGIN, 'user', '').config_schema['patterns']['default']

    def test_patterns_removal_and_profile_isolation(self):
        defaults = self.default_patterns()
        for key, patterns, allowed, blocked in (
            ('silence-removed', defaults[1:], SILENCE, EFFORT),
            ('effort-removed', defaults[:1], EFFORT, SILENCE),
        ):
            self.configure_patterns(patterns)
            for name in ("slack", "relay"):
                adapter, io = self.controlled_adapter(name)
                self.assertTrue(asyncio.run(adapter.send("C_TEST", allowed)).success)
                self.assertFalse(asyncio.run(adapter.send("C_TEST", blocked)).success)
                self.assertEqual(len(io.calls), 1)
                from gateway.config import Platform
                from gateway.platforms.event import MessageEvent
                from gateway.session import SessionSource
                e = MessageEvent('question', source=SessionSource(Platform.SLACK, 'C_TEST'), message_id=key + name)
                async def finals():
                    allowed_result, _ = await adapter.send_final_ledgered(e, key + name, allowed, {}, reply_to=None)
                    blocked_result, _ = await adapter.send_final_ledgered(e, key + name, blocked, {}, reply_to=None)
                    self.assertTrue(allowed_result.success, allowed_result)
                    self.assertFalse(blocked_result.success)
                    self.assertEqual(blocked_result.raw_response, {'filtered': True, 'delivered': False})
                asyncio.run(finals())
                self.assertEqual(len(io.calls), 2)
        other = self.home / "other-profile"
        other.mkdir()
        shutil.copytree(PLUGIN, other / 'plugins' / 'warning-filter',
                        ignore=shutil.ignore_patterns('__pycache__'))
        (other / 'config.yaml').write_text(json.dumps({'plugins': {
            'enabled': ['warning-filter'], 'entries': {'warning-filter': {
                'settings': {'patterns': []}}}}}))
        token = set_hermes_home_override(other)
        process_home = os.environ['HERMES_HOME']
        os.environ['HERMES_HOME'] = str(other)
        other_manager = PluginManager(str(other))
        try:
            self.assertNotEqual(platform_registry.get("relay").plugin_name, "warning-filter")
            other_manager.discover_and_load()
            for name in ('slack', 'relay'):
                adapter, io = self.controlled_adapter(name)
                self.assertTrue(asyncio.run(adapter.send('C_TEST', SILENCE)).success)
                self.assertTrue(asyncio.run(adapter.send('C_TEST', EFFORT)).success)
                self.assertEqual(len(io.calls), 2)
        finally:
            other_manager.unload()
            os.environ['HERMES_HOME'] = process_home
            reset_hermes_home_override(token)
        for name in ('slack', 'relay'):
            adapter, io = self.controlled_adapter(name)
            self.assertFalse(asyncio.run(adapter.send('C_TEST', SILENCE)).success)
            self.assertTrue(asyncio.run(adapter.send('C_TEST', EFFORT)).success)
            self.assertEqual(len(io.calls), 1)
        self.manager.unload()
        self.assertNotEqual(platform_registry.get("relay").plugin_name, "warning-filter")

    def test_custom_replacement_empty_and_full_message_safety(self):
        custom = r'Custom maintenance warning: retry in \d+ seconds\.'
        warning = 'Custom maintenance warning: retry in 17 seconds.'
        for patterns in ([custom], []):
            self.configure_patterns(patterns)
            for name in ('slack', 'relay'):
                adapter, io = self.controlled_adapter(name)
                result = asyncio.run(adapter.send('C_TEST', ' \n⚠️ ' + warning + '\n '))
                self.assertEqual(result.success, not patterns)
                if patterns:
                    self.assertEqual(result.raw_response, {'filtered': True, 'delivered': False})
                    self.assertEqual(io.calls, [])
                for text in (SILENCE, EFFORT, 'ordinary', '⚠ unrelated warning',
                             'Quoted: "' + warning + '"', 'Useful answer\n' + warning,
                             warning + '\nUseful answer'):
                    self.assertTrue(asyncio.run(adapter.send('C_TEST', text)).success)
                self.assertEqual(len(io.calls), 7 + (not patterns))

    def test_invalid_patterns_diagnostics_and_fail_open(self):
        for patterns, diagnostic in (
            (None, 'patterns must be a list'),
            ('ordinary', 'patterns must be a list'),
            ({'rule': 'ordinary'}, 'patterns must be a list'),
            ([None, 7, False, {}, [], '['], 'patterns[0] must be a string'),
            (['['], 'patterns[0] invalid regex'),
        ):
            with self.subTest(patterns=patterns):
                with self.assertLogs('warning-filter', level='WARNING') as captured:
                    self.configure_patterns(patterns)
                self.assertIn(diagnostic, '\n'.join(captured.output))
                for name in ('slack', 'relay'):
                    adapter, io = self.controlled_adapter(name)
                    for text in (SILENCE, EFFORT, 'ordinary'):
                        self.assertTrue(asyncio.run(adapter.send('C_TEST', text)).success)
                    self.assertEqual(len(io.calls), 3)
        with self.assertLogs('warning-filter', level='WARNING') as captured:
            self.configure_patterns(['[', 7, 'Custom warning'])
        self.assertIn('patterns[0] invalid regex', '\n'.join(captured.output))
        self.assertIn('patterns[1] must be a string', '\n'.join(captured.output))
        self.assertNotIn('Custom warning', '\n'.join(captured.output))
        for name in ('slack', 'relay'):
            adapter, io = self.controlled_adapter(name)
            self.assertFalse(asyncio.run(adapter.send('C_TEST', 'Custom warning')).success)
            self.assertTrue(asyncio.run(adapter.send('C_TEST', SILENCE)).success)
            self.assertTrue(asyncio.run(adapter.send('C_TEST', 'ordinary')).success)
            self.assertEqual(len(io.calls), 2)

    def test_native_retry_policy_and_status_logical_lanes(self):
        for name in ("slack", "relay"):
            adapter, io = self.controlled_adapter(name)
            result = asyncio.run(adapter.send("C_TEST", EFFORT))
            self.assertTrue(adapter._send_retry_is_final(result), "filtered sends must be terminal")
            result = asyncio.run(adapter._send_with_retry("C_TEST", EFFORT))
            self.assertFalse(result.success)
            self.assertEqual(io.calls, [])
            if name == "slack":
                self.assertFalse(asyncio.run(adapter.send_or_update_status("C_TEST", "review", EFFORT)).success)
                self.assertEqual(io.calls, [])
                self.assertTrue(asyncio.run(adapter.send_or_update_status("C_TEST", "review", "working")).success)
                self.assertTrue(asyncio.run(adapter.send_or_update_status("C_TEST", "review", "done")).success)
                self.assertEqual([c[0] for c in io.calls], ["send", "edit"])
            else:
                self.assertFalse(asyncio.run(adapter.send_for_platform("slack", "C_TEST", EFFORT)).success)
                self.assertEqual(io.calls, [])

    def test_native_stream_finalization_and_relay_lifecycle(self):
        from dataclasses import replace
        for name in ("slack", "relay"):
            adapter, io = self.controlled_adapter(name)
            md = {"thread_id": "9", "team_id": "T_TEST", "user_id": "U_TEST", "profile": "qa"}
            if name == "relay":
                descriptor = replace(adapter.descriptor, platform="slack", supports_draft_streaming=True,
                                     supported_ops=("send", "edit", "draft"))
                io.descriptor = descriptor
                adapter = type(adapter)(adapter.config, descriptor, transport=io)
                self.assertTrue(asyncio.run(adapter.connect(is_reconnect=True)))
                self.assertIs(io.inbound.__self__, adapter)
            async def stream():
                self.assertTrue((await adapter.send_draft("C_TEST", 7, "answer", metadata=md)).success)
                self.assertTrue((await adapter.send("C_TEST", "answer complete", metadata=md)).success)
            asyncio.run(stream())
            self.assertEqual([c[0] for c in io.calls], ["draft", "finalize"])
            self.assertFalse(asyncio.run(adapter.send("C_TEST", SILENCE, metadata=md)).success)
            self.assertEqual(len(io.calls), 2)
            if name == "relay":
                asyncio.run(adapter.disconnect())
                self.assertTrue(io.disconnected)

    def test_native_entry_metadata_restored_on_unload(self):
        from dataclasses import fields
        relay, _ = platform_registry.snapshot_registration("relay")
        filtered = platform_registry.get("relay")
        for field in fields(relay):
            if field.name not in {"adapter_factory", "check_fn", "source", "plugin_name"}:
                self.assertEqual(getattr(filtered, field.name), getattr(relay, field.name), field.name)
        slack = platform_registry.get("slack")
        self.assertEqual(slack.allowed_users_env, "SLACK_ALLOWED_USERS")
        self.assertFalse(slack.trusted_inbound)
        self.assertIsNotNone(slack.standalone_sender_fn)
        self.manager.unload("warning-filter")
        self.assertIs(platform_registry.get("relay"), relay)
        restored = platform_registry.get("slack")
        self.assertEqual(restored.plugin_name, "slack-platform")
        for field in fields(slack):
            if field.name not in {"adapter_factory", "source", "plugin_name"}:
                before, after = getattr(slack, field.name), getattr(restored, field.name)
                if callable(before):
                    self.assertEqual(before.__qualname__, after.__qualname__, field.name)
                    self.assertEqual(before.__code__.co_filename, after.__code__.co_filename, field.name)
                elif isinstance(before, (str, int, float, bool, type(None))):
                    self.assertEqual(before, after, field.name)
        # Different native import namespaces create distinct policy objects; verify
        # credential admission behavior rather than their Python object identities.
        for key, (accepts, message) in slack.env_value_rules.items():
            restored_accepts, restored_message = restored.env_value_rules[key]
            self.assertEqual(message, restored_message)
            for value in ("invalid", "xoxb-synthetic-token", "xapp-synthetic-token"):
                self.assertEqual(bool(accepts(value)), bool(restored_accepts(value)))
        self.assertEqual(type(platform_registry.create_adapter("slack", PlatformConfig())).__name__, "SlackAdapter")

    def test_actual_warning_status_preserves_open_stream_then_real_finalizes(self):
        from dataclasses import replace
        from gateway.run import _prepare_gateway_status_message, _send_or_update_status_coro
        from gateway.run_turn import _unexpected_silence_reply
        from gateway.config import Platform
        from agent.i18n import t
        for name in ("slack", "relay"):
            adapter, io = self.controlled_adapter(name)
            if name == "relay":
                desc = replace(adapter.descriptor, platform="slack", supports_draft_streaming=True,
                               supported_ops=("send", "edit", "draft"))
                adapter = type(adapter)(adapter.config, desc, transport=io)
            md = {"thread_id": "9", "team_id": "T_TEST", "user_id": "U_TEST"}
            async def exercise():
                self.assertTrue((await adapter.send_draft("C_TEST", 11, "real answer", metadata=md)).success)
                before = len(io.calls)
                for warning in (t("display.review.reasoning_effort_warning", effort="high"),
                                _unexpected_silence_reply()):
                    prepared = _prepare_gateway_status_message(Platform.SLACK, "warn", warning)
                    self.assertIsNotNone(prepared)
                    result = await _send_or_update_status_coro(adapter, "C_TEST", "warn", prepared, md)
                    self.assertFalse(result.success)
                    self.assertEqual(len(io.calls), before)
                # Native state is intact: the actual answer still seals its original
                # stream, rather than becoming a duplicate send or losing prior text.
                self.assertTrue((await adapter.send("C_TEST", "real answer complete", metadata=md)).success)
            asyncio.run(exercise())
            self.assertEqual([c[0] for c in io.calls], ["draft", "finalize"])

    def test_positive_logical_relay_metadata_and_native_factory_auth(self):
        from dataclasses import replace
        native = platform_registry.create_adapter("relay", PlatformConfig())
        io = Recorder()
        io._identities = [("slack", "synthetic-bot")]
        adapter = type(native)(native.config, replace(native.descriptor, platform="slack"), transport=io)
        md = {"thread_id": "9", "scope_id": "T_TEST", "user_id": "U_TEST", "profile": "qa"}
        result = asyncio.run(adapter.send_for_platform("slack", "C_TEST", "answer", reply_to="9", metadata=md))
        self.assertTrue(result.success)
        self.assertEqual(io.calls[0][1]["metadata"], md)
        self.assertEqual(io.calls[0][1]["reply_to"], "9")
        self.assertEqual(io.platforms, ["slack"])
        self.assertEqual(md, {"thread_id": "9", "scope_id": "T_TEST", "user_id": "U_TEST", "profile": "qa"})
        config = {"plugins": {"enabled": ["warning-filter"]},
                  "gateway": {"relay_id": "synthetic-gateway", "relay_secret": "synthetic-secret"}}
        saved = {k: os.environ.pop(k, None) for k in ("GATEWAY_RELAY_ID", "GATEWAY_RELAY_SECRET")}
        try:
            (self.home / "config.yaml").write_text(json.dumps(config))
            register_relay_adapter(force=True, url="wss://connector.invalid/relay")
            base, _ = platform_registry.snapshot_registration("relay")
            expected = base.adapter_factory(PlatformConfig())
            actual = platform_registry.create_adapter("relay", PlatformConfig())
            self.assertEqual(actual.descriptor, expected.descriptor)
            self.assertEqual(type(actual._transport), type(expected._transport))
            for key in ("_url", "_identities", "_gateway_id", "_upgrade_secret", "_reconnect"):
                self.assertEqual(getattr(actual._transport, key), getattr(expected._transport, key))
            self.assertEqual(actual._transport._gateway_id, "synthetic-gateway")
            self.assertEqual(actual._transport._upgrade_secret, "synthetic-secret")
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value
            register_relay_adapter(force=True, url="")

    def test_real_consumer_turn_death_cleanup_preserves_text(self):
        from dataclasses import replace
        from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
        native = platform_registry.create_adapter("relay", PlatformConfig())
        io = Recorder()
        descriptor = replace(native.descriptor, platform="slack", supports_draft_streaming=True,
                             supported_ops=("send", "edit", "draft"))
        adapter = type(native)(native.config, descriptor, transport=io)
        consumer = GatewayStreamConsumer(adapter, "C_TEST",
            config=StreamConsumerConfig(transport="draft", buffer_threshold=1, edit_interval=0, cursor=""),
            metadata={"thread_id": "9"}, run_still_current=lambda: not io.calls)
        consumer.on_delta("kept real content")
        asyncio.run(asyncio.wait_for(consumer.run(), timeout=2))
        self.assertEqual([c[0] for c in io.calls], ["draft", "finalize"])
        self.assertEqual(io.calls[-1][1]["content"], "kept real content")
        count = len(io.calls)
        self.assertFalse(asyncio.run(adapter.send("C_TEST", EFFORT)).success)
        self.assertEqual(len(io.calls), count)

    def test_02_warning_is_not_delivered(self):
        for name in ("slack", "relay"):
            with self.subTest(name=name):
                adapter = platform_registry.create_adapter(name, PlatformConfig())
                result = asyncio.run(adapter.send("C_TEST", SILENCE))
                self.assertFalse(result.success)
                self.assertEqual(result.raw_response, {"filtered": True, "delivered": False})
                self.assertFalse(result.retryable)
                self.assertIsNone(result.message_id)


class RelayBootTests(unittest.TestCase):
    """Optional Relay admission through native discovery/config and factory consumers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='relay-boot-', dir=os.environ['TMPDIR'])
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.saved_env = {k: v for k, v in os.environ.items() if k.startswith('GATEWAY_RELAY_')}
        for key in self.saved_env:
            os.environ.pop(key)
        self.process_home = os.environ.get('HERMES_HOME')
        os.environ['HERMES_HOME'] = str(self.home)
        self.token = set_hermes_home_override(self.home)
        self.addCleanup(self.restore)
        shutil.copytree(PLUGIN, self.home / 'plugins' / 'warning-filter',
                        ignore=shutil.ignore_patterns('__pycache__', '.git'))
        self.manager = PluginManager(str(self.home))
        self.addCleanup(self.manager.unload)

    def restore(self):
        reset_hermes_home_override(self.token)
        if self.process_home is None:
            os.environ.pop('HERMES_HOME', None)
        else:
            os.environ['HERMES_HOME'] = self.process_home
        for key in list(os.environ):
            if key.startswith('GATEWAY_RELAY_'):
                os.environ.pop(key)
        os.environ.update(self.saved_env)

    def boot(self, *, url=False, disabled=False):
        from gateway.config import load_gateway_config
        config = {'plugins': {'enabled': ['warning-filter']}}
        if url:
            config['gateway'] = {'relay_url': 'wss://connector.invalid/relay',
                                 'relay_id': 'synthetic-gateway', 'relay_secret': 'synthetic-secret'}
        if disabled:
            config['platforms'] = {'relay': {'enabled': False}}
        (self.home / 'config.yaml').write_text(json.dumps(config))
        self.manager.discover_and_load()
        registered = register_relay_adapter()
        loaded = load_gateway_config()
        from gateway.config import Platform
        relay = loaded.platforms.get(Platform.RELAY)
        adapter = platform_registry.create_adapter('relay', relay or PlatformConfig())
        print('RELAY BOOT', 'url', url, 'disabled', disabled, 'registered', registered,
              'enabled', bool(relay and relay.enabled), 'adapter', type(adapter).__name__, flush=True)
        return registered, relay, adapter

    def test_absent_url_does_not_autodetect_relay(self):
        registered, relay, adapter = self.boot()
        self.assertFalse(registered)
        self.assertFalse(relay and relay.enabled, 'unconfigured Relay must not enter startup')
        self.assertIsNone(adapter)

    def test_explicit_disable_with_url(self):
        registered, relay, adapter = self.boot(url=True, disabled=True)
        self.assertFalse(registered)
        self.assertFalse(relay.enabled)
        self.assertIsNone(adapter)

    def test_configured_url_native_factory(self):
        registered, relay, adapter = self.boot(url=True)
        self.assertTrue(registered)
        self.assertTrue(relay.enabled)
        self.assertIsNotNone(adapter)
        self.assertTrue(any(c.__name__ == 'WarningFilter' for c in type(adapter).__mro__))
        self.assertEqual(adapter._transport._gateway_id, 'synthetic-gateway')
        self.assertEqual(adapter._transport._upgrade_secret, 'synthetic-secret')

    def test_explicit_disable_vetoes_environment_url(self):
        os.environ['GATEWAY_RELAY_URL'] = 'wss://connector.invalid/relay'
        registered, relay, adapter = self.boot(disabled=True)
        self.assertFalse(registered)
        self.assertFalse(relay and relay.enabled)
        self.assertIsNone(adapter)

    def test_late_url_after_discovery(self):
        from gateway.config import load_gateway_config, Platform
        self.boot()
        # Model only the output of provisioning, never its network/authentication work.
        os.environ['GATEWAY_RELAY_URL'] = 'wss://late.invalid/relay'
        os.environ['GATEWAY_RELAY_ID'] = 'late-gateway'
        os.environ['GATEWAY_RELAY_SECRET'] = 'late-secret'
        self.assertTrue(register_relay_adapter())
        config = load_gateway_config()
        self.assertTrue(config.platforms[Platform.RELAY].enabled)
        adapter = platform_registry.create_adapter('relay', config.platforms[Platform.RELAY])
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter._transport._url, 'wss://late.invalid/relay')
        self.assertEqual(adapter._transport._gateway_id, 'late-gateway')
        self.assertFalse(asyncio.run(adapter.send('C_TEST', SILENCE)).success)

    def test_global_registration_does_not_activate_other_profile(self):
        from gateway.config import load_gateway_config, Platform
        self.boot(url=True)
        other = self.home / 'other-profile'
        other.mkdir()
        shutil.copytree(PLUGIN, other / 'plugins' / 'warning-filter',
                        ignore=shutil.ignore_patterns('__pycache__', '.git'))
        (other / 'config.yaml').write_text(json.dumps({'plugins': {'enabled': ['warning-filter']}}))
        token = set_hermes_home_override(other)
        os.environ['HERMES_HOME'] = str(other)
        manager = PluginManager(str(other))
        try:
            manager.discover_and_load()
            config = load_gateway_config()
            relay = config.platforms.get(Platform.RELAY)
            self.assertFalse(relay and relay.enabled)
            self.assertIsNone(platform_registry.create_adapter('relay', PlatformConfig()))
            os.environ['GATEWAY_RELAY_URL'] = 'wss://other.invalid/relay'
            adapter = platform_registry.create_adapter('relay', PlatformConfig())
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter._transport._url, 'wss://other.invalid/relay')
        finally:
            manager.unload()
            os.environ.pop('GATEWAY_RELAY_URL', None)
            os.environ['HERMES_HOME'] = str(self.home)
            reset_hermes_home_override(token)
        adapter = platform_registry.create_adapter('relay', PlatformConfig())
        self.assertEqual(adapter._transport._url, 'wss://connector.invalid/relay')
        self.assertEqual(adapter._transport._gateway_id, 'synthetic-gateway')


if __name__ == "__main__":
    unittest.main(verbosity=2)
