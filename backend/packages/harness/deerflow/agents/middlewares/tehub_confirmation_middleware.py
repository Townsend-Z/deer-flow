"""Middleware that intercepts dangerous Tehub MCP tool calls and triggers human confirmation."""

from collections.abc import Callable
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.graph import END
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

# Tools that require explicit human confirmation before execution
TEHUB_DANGEROUS_TOOLS: frozenset[str] = frozenset(
    {
        # Create operations
        "createToken",
        "createStreamInfo",
        "createTaskInfo",
        # Publish operations
        "publishStreamConfig",
        "publishTaskConfigDraft",
        # Start/Stop/Kill operations
        "startStream",
        "stopStream",
        "startTask",
        "stopTask",
        "killTask",
        # Delete operations
        "deleteStream",
        "deleteTask",
        # Edit/Modify operations
        "editTokenInfo",
        "editStreamInfo",
        "editTaskInfo",
        "editIsSchemaCheck",
        "updateStreamParkArgs",
    }
)

# Read-only tools — pass through without confirmation
TEHUB_READONLY_TOOLS: frozenset[str] = frozenset(
    {
        "getByTokenName",
        "getStreamEntityById",
        "getDraftStreamConfig",
        "getStreamConfigByVersion",
        "getStreamInfoPoByTokenId",
        "getTaskEntityById",
        "getTaskJobStartInfo",
    }
)

# Translated operation descriptions for user-facing confirmation messages
_TOOL_DESCRIPTIONS: dict[str, str] = {
    "createToken": "创建 Token",
    "createStreamInfo": "创建 Stream",
    "createTaskInfo": "创建 Task",
    "publishStreamConfig": "发布 Stream 配置",
    "publishTaskConfigDraft": "发布 Task 配置",
    "startStream": "启动 Stream",
    "stopStream": "停止 Stream",
    "startTask": "启动 Task",
    "stopTask": "停止 Task",
    "killTask": "强制终止 Task",
    "deleteStream": "删除 Stream",
    "deleteTask": "删除 Task",
    "editTokenInfo": "修改 Token 信息",
    "editStreamInfo": "修改 Stream 信息",
    "editTaskInfo": "修改 Task 信息",
    "editIsSchemaCheck": "修改 Stream Schema 校验开关",
    "updateStreamParkArgs": "更新 Stream Park 参数",
}


def _format_args_summary(args: dict) -> str:
    """Format tool call arguments into a readable summary.

    Args:
        args: Tool call argument dictionary

    Returns:
        Human-readable summary of key arguments
    """
    if not args:
        return "（无参数）"

    # Show first 5 key-value pairs to avoid overly long messages
    items = list(args.items())[:5]
    lines = []
    for key, value in items:
        if isinstance(value, str) and len(value) > 80:
            value = value[:77] + "..."
        lines.append(f"  - {key}: {value}")

    if len(args) > 5:
        lines.append(f"  ... 共 {len(args)} 个参数")

    return "\n".join(lines)


def _build_confirmation_message(tool_name: str, args: dict) -> str:
    """Build a human-readable confirmation message for a dangerous tool call.

    Args:
        tool_name: Name of the Tehub MCP tool
        args: Tool call arguments

    Returns:
        Formatted confirmation message
    """
    description = _TOOL_DESCRIPTIONS.get(tool_name, tool_name)
    args_summary = _format_args_summary(args)

    return (
        f"⚠️ 即将执行：**{description}**\n\n"
        f"操作参数：\n{args_summary}\n\n"
        f"此操作可能影响数据或业务状态，请确认是否继续？\n"
        f"回复 **是/确认/yes** 继续，回复 **否/取消/no** 取消。"
    )


class TehubConfirmationMiddlewareState(AgentState):
    """Compatible with the `ThreadState` schema."""

    pass


class TehubConfirmationMiddleware(AgentMiddleware[TehubConfirmationMiddlewareState]):
    """Intercepts dangerous Tehub MCP tool calls and requires explicit human confirmation.

    When the model invokes a Tehub MCP tool that is classified as "dangerous"
    (create / publish / start / stop / kill / delete / edit), this middleware:

    1. Intercepts the tool call before execution
    2. Formats a human-readable summary of the operation and its arguments
    3. Interrupts execution and presents a risk confirmation question to the user
    4. Execution only resumes when the user explicitly confirms

    Read-only tools (get*/list* variants) pass through without interruption.
    """

    state_schema = TehubConfirmationMiddlewareState

    def _handle_dangerous_call(self, request: ToolCallRequest) -> Command:
        """Handle a dangerous tool call by interrupting for confirmation.

        Args:
            request: Tool call request for the dangerous operation

        Returns:
            Command that interrupts execution with a confirmation message
        """
        tool_name = request.tool_call.get("name", "")
        args = request.tool_call.get("args", {})
        tool_call_id = request.tool_call.get("id", "")

        print(f"[TehubConfirmationMiddleware] Intercepted dangerous tool: {tool_name}")

        confirmation_message = _build_confirmation_message(tool_name, args)

        tool_message = ToolMessage(
            content=confirmation_message,
            tool_call_id=tool_call_id,
            name="ask_clarification",
        )

        return Command(
            update={"messages": [tool_message]},
            goto=END,
        )

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Intercept dangerous Tehub tool calls and trigger confirmation (sync).

        Args:
            request: Tool call request
            handler: Original tool execution handler

        Returns:
            Confirmation Command for dangerous tools, or original result for safe tools
        """
        tool_name = request.tool_call.get("name", "")
        if tool_name in TEHUB_DANGEROUS_TOOLS:
            return self._handle_dangerous_call(request)
        return handler(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Intercept dangerous Tehub tool calls and trigger confirmation (async).

        Args:
            request: Tool call request
            handler: Original tool execution handler (async)

        Returns:
            Confirmation Command for dangerous tools, or original result for safe tools
        """
        tool_name = request.tool_call.get("name", "")
        if tool_name in TEHUB_DANGEROUS_TOOLS:
            return self._handle_dangerous_call(request)
        return await handler(request)
