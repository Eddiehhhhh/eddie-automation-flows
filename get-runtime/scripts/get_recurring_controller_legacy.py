"""Archived controller template, inert bytes only; never execute this module data.

Source: Eddiehhhhh/eddie-automation-flows at immutable runtime
6ad627793c23e80ad2ca9568048eb4e595c881a0,
.github/workflow-templates/get-canonical-controller.yml.disabled.
Historical policies must render these original bytes, not the current template.
The selector verifies the pinned SHA-256 before returning this data.
"""
import get_private_runtime_bindings as _private_bindings

SOURCE_RUNTIME_SHA = _private_bindings.value('get_recurring_controller_legacy', 'SOURCE_RUNTIME_SHA')
TEMPLATE_SHA256 = _private_bindings.value('get_recurring_controller_legacy', 'TEMPLATE_SHA256')
TEMPLATE_BYTES = _private_bindings.value('get_recurring_controller_legacy', 'TEMPLATE_BYTES')
