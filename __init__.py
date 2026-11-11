"""Native profile-scoped adapter registrations; no installed-source changes."""
from dataclasses import fields
import re

from gateway.platforms.base import SendResult
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.relay.adapter import RelayAdapter


_SILENCE = "The model returned only a silence marker for a message that needed a reply. Try again or rephrase."
_EFFORT = re.compile(r"auxiliary\.background_review\.reasoning_effort='[^'\r\n]+' has no effect while the review model is not the main model")


class WarningFilter:
    """Reject only complete warning payloads before any native delivery work."""

    def _filtered(self, content):
        text = content.strip()
        for prefix in ("⚠️ ", "⚠ "):
            if text.startswith(prefix):
                text = text[len(prefix):]
                break
        if ((self._suppress_silence and text == _SILENCE)
                or (self._suppress_effort and _EFFORT.fullmatch(text))):
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
    silence = ctx.get_config("suppress_unexpected_silence", True) is not False
    effort = ctx.get_config("suppress_reasoning_effort", True) is not False
    # Reuse the bundled registrar rather than resolving a deferred loader while
    # another plugin's deadline worker owns the discovery lock.
    from plugins.platforms.slack.adapter import register as register_slack
    register_slack(ctx)
    slack = platform_registry.get("slack")
    if slack is not None:
        class FilteredSlack(WarningFilter, slack.adapter_factory):
            _suppress_silence = silence
            _suppress_effort = effort

            async def send_or_update_status(self, chat_id, status_key, content, *, metadata=None):
                result = self._filtered(content)
                if result is not None:
                    return result
                return await super().send_or_update_status(chat_id, status_key, content, metadata=metadata)

        _register_entry(ctx, slack, FilteredSlack)

    class FilteredRelay(WarningFilter, RelayAdapter):
        _suppress_silence = silence
        _suppress_effort = effort

        async def send_for_platform(self, logical_platform, chat_id, content, reply_to=None, metadata=None):
            result = self._filtered(content)
            if result is not None:
                return result
            return await super().send_for_platform(logical_platform, chat_id, content,
                                                  reply_to=reply_to, metadata=metadata)

    def relay_factory(config):
        # Core registers Relay after plugin discovery and refreshes it on startup.
        # Read its current public snapshot, never displace its global registration.
        entry, _ = platform_registry.snapshot_registration("relay")
        if entry is None:
            raise RuntimeError("Core Relay factory has not been registered")
        native = entry.adapter_factory(config)
        return FilteredRelay(config, native.descriptor, transport=native._transport)

    relay, _ = platform_registry.snapshot_registration("relay")
    _register_entry(ctx, relay or PlatformEntry("relay", "Relay", relay_factory, lambda: True,
                                              source="builtin"), relay_factory)
