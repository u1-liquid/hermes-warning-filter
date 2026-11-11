# Hermes warning-filter

Suppress two complete gateway warnings before Slack or Relay delivery:

- Unexpected silence: “The model returned only a silence marker for a message that needed a reply. Try again or rephrase.”
- Ineffective background review effort: `auxiliary.background_review.reasoning_effort='…' has no effect while the review model is not the main model`. The effort value is variable, not limited to `high`.

Both filters are on by default. Matching allows surrounding whitespace and the native warning icon. Ordinary replies, unrelated warnings, quotations and warning text embedded in a useful answer pass through unchanged. Filtered edits do not erase existing content.

## Install

Install the `feat/warning-filter` implementation branch at its exact commit, initially disabled:

```sh
git clone --branch feat/warning-filter --single-branch https://github.com/u1-liquid/hermes-warning-filter.git
hermes plugins install https://github.com/u1-liquid/hermes-warning-filter.git --ref "$(git -C hermes-warning-filter rev-parse HEAD)" --no-enable
hermes plugins enable warning-filter
```

The native `--ref` option requires a full 40-character commit SHA, not a branch name. This repository uses the native directory-plugin layout (`plugin.yaml` and `__init__.py` at its root). Use the intended Hermes profile for installation and configuration. Restart that profile's Hermes process to load changed adapter registrations.

## Settings

In that profile's `config.yaml`, the two independent keys are:

```yaml
plugins:
  enabled: [warning-filter]
  entries:
    warning-filter:
      settings:
        suppress_unexpected_silence: true
        suppress_reasoning_effort: true
```

Preserve other enabled plugins when configuring your profile. Set either key to native YAML boolean `false` to opt out of that filter. Strings such as `"false"`, zero and null are not opt-outs. Missing settings remain enabled.

## Compatibility and verification

Qualified against official Hermes Agent commit `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19` on Python 3.14. Native tests exercise discovery, registration, Slack and logical Relay routing, send/edit/status/draft paths, stream finalization and cleanup, independent settings, unload, metadata/authentication construction, and the default delivery ledger's retry/recovery controls.

A filtered receipt is unsuccessful, has no message ID, is nonretryable and explicitly reports `filtered: true, delivered: false`. Final warnings are suppressed before an obligation is created, preventing ledger recovery from replaying them as recovered replies. Ordinary failures still use native retry/recovery.

These are controlled-I/O tests, not authenticated live Slack/Relay delivery or a complete model/application turn. Optional runtime-footer augmentation is not qualified; a warning combined with extra prose is deliberately not suppressed. The adapter uses host internals, including Relay's private `_transport`; recertify against the native suite after every host upgrade. Native source-validation coupling warnings are retained in CI.

## Development

Use Python 3.14 and a separate checkout of the pinned host. Keep environments, host source, test homes and logs outside this repository because tests recursively copy the plugin root. CI shows the complete preparation sequence: install `requirements-dev.txt` in a separate venv, set disposable `HERMES_HOME`, `HERMES_RUNTIME_DIR` and `TMPDIR`, set `PYTHONPATH` to the host, and set the host-supported `HERMES_DISABLE_LAZY_INSTALLS=1` before running:

```sh
python -m unittest discover -s tests -p test_native.py -v
python -m hermes_cli.main plugins validate . --json
```

The prepared venv supplies the dependencies; disabling lazy installation does not prepare them. Tests bind both process and context homes per case and assert teardown. Dependabot configuration covers Actions and Python development requirements with separate version/security groups; it becomes active only after reaching the default branch.
