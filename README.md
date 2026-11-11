# Hermes warning-filter

Suppress configured complete gateway warnings before Slack or Relay delivery. The default regex list covers:

- Unexpected silence: “The model returned only a silence marker for a message that needed a reply. Try again or rephrase.”
- Ineffective background review effort: `auxiliary.background_review.reasoning_effort='…' has no effect while the review model is not the main model`. The effort value is variable, not limited to `high`.

These defaults apply when `patterns` is absent. Matching uses Python `re.fullmatch` after removing surrounding whitespace and one native warning prefix (`⚠️ ` or `⚠ `), not substring searching. Ordinary replies, unrelated warnings, quotations and warning text embedded in a useful answer pass through unchanged with the defaults. Custom patterns define their own complete-message scope; avoid broad expressions such as `.*`. Filtered edits do not erase existing content.

## Install

Install the `feat/warning-filter` implementation branch at its exact commit, initially disabled:

```sh
git clone --branch feat/warning-filter --single-branch https://github.com/u1-liquid/hermes-warning-filter.git
hermes plugins install https://github.com/u1-liquid/hermes-warning-filter.git --ref "$(git -C hermes-warning-filter rev-parse HEAD)" --no-enable
hermes plugins enable warning-filter
```

The native `--ref` option requires a full 40-character commit SHA, not a branch name. This repository uses the native directory-plugin layout (`plugin.yaml` and `__init__.py` at its root). Use the intended Hermes profile for installation and configuration. Restart that profile's Hermes process to load changed adapter registrations.

## Settings

In that profile's native plugin settings, `patterns` is a list of Python regex strings. Omit it to use the manifest defaults, or supply the complete replacement list. For example, retain both defaults and add a third warning without changing Python:

```yaml
plugins:
  enabled: [warning-filter]
  entries:
    warning-filter:
      settings:
        patterns:
          - 'The model returned only a silence marker for a message that needed a reply\. Try again or rephrase\.'
          - "auxiliary\\.background_review\\.reasoning_effort='[^'\\r\\n]+' has no effect while the review model is not the main model"
          - 'Custom maintenance warning: retry in \d+ seconds\.'
```

An explicit list replaces (does not extend) the defaults: use only the third entry to suppress only that warning, or remove an entry to stop suppressing it. Disable all suppression with:

```yaml
plugins:
  entries:
    warning-filter:
      settings:
        patterns: []
```

Preserve other enabled plugins and settings. Invalid list types suppress nothing; non-string entries and invalid regexes are ignored individually, retaining valid siblings. Diagnostics identify the setting/index and failure type without logging message payloads or rule text. Invalid explicit settings never fall back to defaults. Rules are compiled when the plugin registers; restart the profile's Hermes process after settings changes. There is no automatic hot reload.

## Compatibility and verification

Qualified against official Hermes Agent commit `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19` on Python 3.14. Native tests exercise discovery, registration, Slack and logical Relay routing, send/edit/status/draft paths, stream finalization and cleanup, list replacement/removal/empty settings, custom rules, invalid-rule diagnostics, profile isolation, unload, metadata/authentication construction, and the default delivery ledger's retry/recovery controls.

A filtered receipt is unsuccessful, has no message ID, is nonretryable and explicitly reports `filtered: true, delivered: false`. Final warnings are suppressed before an obligation is created, preventing ledger recovery from replaying them as recovered replies. Ordinary failures still use native retry/recovery.

These are controlled-I/O tests, not authenticated live Slack/Relay delivery or a complete model/application turn. Optional runtime-footer augmentation is not qualified; a warning combined with extra prose is deliberately not suppressed. The adapter uses host internals, including Relay's private `_transport`; recertify against the native suite after every host upgrade. Native source-validation coupling warnings are retained in CI.

## Development

Use Python 3.14 and a separate checkout of the pinned host. Keep environments, host source, test homes and logs outside this repository because tests recursively copy the plugin root. CI shows the complete preparation sequence: install `requirements-dev.txt` in a separate venv, set disposable `HERMES_HOME`, `HERMES_RUNTIME_DIR` and `TMPDIR`, set `PYTHONPATH` to the host, and set the host-supported `HERMES_DISABLE_LAZY_INSTALLS=1` before running:

```sh
python -m unittest discover -s tests -p test_native.py -v
python -m hermes_cli.main plugins validate . --json
```

The prepared venv supplies the dependencies; disabling lazy installation does not prepare them. Tests bind both process and context homes per case and assert teardown. Dependabot configuration covers Actions and Python development requirements with separate version/security groups; it becomes active only after reaching the default branch.
