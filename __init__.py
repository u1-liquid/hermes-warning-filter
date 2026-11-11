"""Native profile-scoped adapter registrations; no installed-source changes."""
from dataclasses import fields, replace
import logging
import re

from gateway.platforms.base import SendResult
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.relay import register_relay_adapter, relay_url
from gateway.relay.adapter import RelayAdapter


logger = logging.getLogger('warning-filter')


class WarningFilter:
    """Reject only complete warning payloads before any native delivery work."""

    def _filtered(self, content):
        text = content.strip()
        for prefix in ("⚠️ ", "⚠ "):
            if text.startswith(prefix):
                text = text[len(prefix):]
                break
        if any(pattern.fullmatch(text) for pattern in self._patterns):
            return SendResult(success=False, error="warning-filter: filtered; not delivered",
                              raw_response={"filtered": True, "delivered": False}, retryable=False)
        return None

    async def send_final_ledgered(self, event, session_key, text_content, metadata, *,
                                  reply_to, is_ephemeral_response=False):
        result = self._filtered(text_content)
        if result is not None:
            # Nothing is owed: release the native turn marker without creating a
            # failed obligation that recovery would later present as a real reply.
            owner = self._final_delivery_adapter(event.source)
            await self._release_turn_marker(event)
            return result, owner
        return await super().send_final_ledgered(
            event, session_key, text_content, metadata, reply_to=reply_to,
            is_ephemeral_response=is_ephemeral_response)

    def _send_retry_is_final(self, result):
        if result.raw_response == {"filtered": True, "delivered": False}:
            return True
        return super()._send_retry_is_final(result)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        result = self._filtered(content)
        if result is not None:
            return result
        return await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        result = self._filtered(content)
        if result is not None:
            # Never blank or delete an existing message, which may contain real text.
            return result
        return await super().edit_message(chat_id, message_id, content,
                                          finalize=finalize, metadata=metadata)

    async def send_draft(self, chat_id, draft_id, content, metadata=None):
        result = self._filtered(content)
        if result is not None:
            return result
        return await super().send_draft(chat_id, draft_id, content, metadata=metadata)




def _register_entry(ctx, entry, factory):
    kwargs = {f.name: getattr(entry, f.name) for f in fields(entry)
              if f.name not in {"source", "plugin_name", "adapter_factory"}}
    ctx.register_platform(**kwargs, adapter_factory=factory)


def register(ctx):
    manifest = getattr(ctx, 'manifest', None)
    if manifest is None:
        # Native validation probes omit manifest; use the host's same parser.
        from pathlib import Path
        from hermes_cli.plugins_manifest import parse_manifest_file
        root = Path(__file__).resolve().parent
        manifest = parse_manifest_file(root / 'plugin.yaml', root, 'user', '')
    configured = ctx.get_config('patterns', manifest.config_schema['patterns']['default'])
    patterns = []
    if not isinstance(configured, list):
        logger.warning('patterns must be a list; no messages will be suppressed')
    else:
        for index, pattern in enumerate(configured):
            if not isinstance(pattern, str):
                logger.warning('patterns[%d] must be a string; rule ignored', index)
                continue
            try:
                patterns.append(re.compile(pattern))
            except (re.error, OverflowError):
                logger.warning('patterns[%d] invalid regex; rule ignored', index)
    patterns = tuple(patterns)
    # Reuse the bundled registrar rather than resolving a deferred loader while
    # another plugin's deadline worker owns the discovery lock.
    from plugins.platforms.slack.adapter import register as register_slack
    register_slack(ctx)
    slack = platform_registry.get("slack")
    if slack is not None:
        class FilteredSlack(WarningFilter, slack.adapter_factory):
            _patterns = patterns

            async def send_or_update_status(self, chat_id, status_key, content, *, metadata=None):
                result = self._filtered(content)
                if result is not None:
                    return result
                return await super().send_or_update_status(chat_id, status_key, content, metadata=metadata)

        _register_entry(ctx, slack, FilteredSlack)

    class FilteredRelay(WarningFilter, RelayAdapter):
        _patterns = patterns

        async def send_for_platform(self, logical_platform, chat_id, content, reply_to=None, metadata=None):
            result = self._filtered(content)
            if result is not None:
                return result
            return await super().send_for_platform(logical_platform, chat_id, content,
                                                  reply_to=reply_to, metadata=metadata)

    def relay_factory(config):
        # Resolve in the consuming profile, including URLs provisioned after discovery.
        # Core retains ownership of transport construction and authentication.
        register_relay_adapter()
        entry, _ = platform_registry.snapshot_registration("relay")
        if entry is None:
            raise RuntimeError("Core Relay factory has not been registered")
        native = entry.adapter_factory(config)
        return FilteredRelay(config, native.descriptor, transport=native._transport)

    relay, _ = platform_registry.snapshot_registration("relay")
    # Discovery precedes optional native registration. A scoped overlay must not
    # advertise a configured Relay merely because another profile registered one.
    relay = relay or PlatformEntry("relay", "Relay", relay_factory, lambda: False,
                                  source="builtin", emoji="🔌")
    _register_entry(ctx, replace(relay, check_fn=lambda: bool(relay_url())), relay_factory)
