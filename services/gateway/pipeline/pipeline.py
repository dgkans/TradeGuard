"""The gateway decision pipeline (PLAN.md Section 2). F5 implements stages 1, 2, 3 and 6.

    1 schema check -> 2 tool allowlist -> 3 policy rules -> [4 taint, 5 intent: F7, F8]
    -> 6 approval rules -> 7 forward (done by the gateway on ALLOW)

The first DENY wins. If nothing denies, the first ESCALATE wins. Otherwise ALLOW.
Any exception inside the pipeline is a DENY (fail closed).
"""

from collections.abc import Awaitable, Callable
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from services.gateway.pipeline.rules import APPROVAL_RULES, CONTEXT_RULES, STATIC_RULES, Rule
from services.gateway.pipeline.types import PolicyContext, ToolCall, Verdict
from services.gateway.policy.model import Policy

ContextFetcher = Callable[[ToolCall], Awaitable[PolicyContext]]
ORDER_TOOL = "place_order"


class Pipeline:
    def __init__(self, policy: Policy, tool_schemas: dict[str, dict[str, Any]]) -> None:
        """`tool_schemas` maps each upstream tool name to its JSON input schema."""
        self.policy = policy
        self.validators: dict[str, Draft202012Validator] = {}
        for name, schema in tool_schemas.items():
            self.validators[name] = Draft202012Validator(schema)
        self.static_rules: list[Rule] = [rule(policy) for rule in STATIC_RULES]
        self.context_rules: list[Rule] = [rule(policy) for rule in CONTEXT_RULES]
        self.approval_rules: list[Rule] = [rule(policy) for rule in APPROVAL_RULES]

    async def decide(
        self, call: ToolCall, fetch_context: ContextFetcher, *, skip_approval: bool = False
    ) -> Verdict:
        """Return the verdict for one tool call. Never raises: errors become DENY.

        `skip_approval` re-checks a human approved call: every deny rule still applies,
        only the approval stage is skipped.
        """
        try:
            return await self._decide(call, fetch_context, skip_approval)
        except Exception as exc:
            return Verdict("DENY", "pipeline_error", f"{type(exc).__name__}: {exc}")

    async def _decide(
        self, call: ToolCall, fetch_context: ContextFetcher, skip_approval: bool
    ) -> Verdict:
        schema_problem = self.check_schema(call)
        if schema_problem is not None:
            return schema_problem
        if not self.policy.is_allowed_tool(call.tool):
            reason = f"{call.tool} is not in policy {self.policy.name!r} tool allowlist"
            return Verdict("DENY", "tool_not_allowed", reason)
        if call.tool != ORDER_TOOL:
            kind = "write" if self.policy.is_write_tool(call.tool) else "read"
            return Verdict("ALLOW", "policy_pass", f"{kind} tool on the allowlist")

        for rule in self.static_rules:
            result = rule.evaluate(call, None)
            if not result.passed:
                return Verdict(result.decision, result.rule_id, result.reason)

        try:
            context = await fetch_context(call)
        except Exception as exc:
            reason = f"could not read account or market state: {type(exc).__name__}: {exc}"
            return Verdict("DENY", "context_unavailable", reason)

        for rule in self.context_rules:
            result = rule.evaluate(call, context)
            if not result.passed:
                return Verdict(result.decision, result.rule_id, result.reason)

        if skip_approval:
            return Verdict("ALLOW", "policy_pass", "re-check after human approval passed")
        for rule in self.approval_rules:
            result = rule.evaluate(call, context)
            if not result.passed:
                return Verdict(result.decision, result.rule_id, result.reason)
        return Verdict("ALLOW", "policy_pass", f"order passed every rule in {self.policy.name!r}")

    def check_schema(self, call: ToolCall) -> Verdict | None:
        validator = self.validators.get(call.tool)
        if validator is None:
            return Verdict("DENY", "schema_unknown", f"no input schema known for {call.tool}")
        try:
            errors = sorted(validator.iter_errors(call.args), key=error_path)
        except SchemaError as exc:
            return Verdict("DENY", "schema_unknown", f"upstream schema is invalid: {exc.message}")
        if errors:
            first = errors[0]
            where = ".".join(str(part) for part in first.absolute_path) or "arguments"
            return Verdict("DENY", "schema_invalid", f"{where}: {first.message}")
        return None


def error_path(error: Any) -> str:
    return ".".join(str(part) for part in error.absolute_path)
