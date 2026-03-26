"""Tests for TehubConfirmationMiddleware."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from deerflow.agents.middlewares.tehub_confirmation_middleware import (
    TEHUB_DANGEROUS_TOOLS,
    TEHUB_READONLY_TOOLS,
    TehubConfirmationMiddleware,
    _build_confirmation_message,
    _format_args_summary,
)

# --- Helpers ---


def _make_tool_call_request(name: str, args: dict | None = None, call_id: str = "call_1"):
    """Create a mock ToolCallRequest."""
    req = MagicMock()
    req.tool_call = {"name": name, "args": args or {}, "id": call_id}
    return req


# --- Tool set membership tests ---


class TestToolSets:
    def test_dangerous_tools_not_empty(self):
        assert len(TEHUB_DANGEROUS_TOOLS) > 0

    def test_readonly_tools_not_empty(self):
        assert len(TEHUB_READONLY_TOOLS) > 0

    def test_no_overlap_between_sets(self):
        overlap = TEHUB_DANGEROUS_TOOLS & TEHUB_READONLY_TOOLS
        assert overlap == frozenset(), f"Tools in both sets: {overlap}"

    def test_create_operations_are_dangerous(self):
        for tool in ("createToken", "createStreamInfo", "createTaskInfo"):
            assert tool in TEHUB_DANGEROUS_TOOLS, f"{tool} should be dangerous"

    def test_publish_operations_are_dangerous(self):
        for tool in ("publishStreamConfig", "publishTaskConfigDraft"):
            assert tool in TEHUB_DANGEROUS_TOOLS, f"{tool} should be dangerous"

    def test_start_stop_kill_are_dangerous(self):
        for tool in ("startStream", "stopStream", "startTask", "stopTask", "killTask"):
            assert tool in TEHUB_DANGEROUS_TOOLS, f"{tool} should be dangerous"

    def test_delete_operations_are_dangerous(self):
        for tool in ("deleteStream", "deleteTask"):
            assert tool in TEHUB_DANGEROUS_TOOLS, f"{tool} should be dangerous"

    def test_edit_operations_are_dangerous(self):
        for tool in (
            "editTokenInfo",
            "editStreamInfo",
            "editTaskInfo",
            "editIsSchemaCheck",
            "updateStreamParkArgs",
        ):
            assert tool in TEHUB_DANGEROUS_TOOLS, f"{tool} should be dangerous"

    def test_get_operations_are_readonly(self):
        for tool in (
            "getByTokenName",
            "getStreamEntityById",
            "getDraftStreamConfig",
            "getStreamConfigByVersion",
            "getStreamInfoPoByTokenId",
            "getTaskEntityById",
            "getTaskJobStartInfo",
        ):
            assert tool in TEHUB_READONLY_TOOLS, f"{tool} should be readonly"


# --- Message formatting tests ---


class TestBuildConfirmationMessage:
    def test_includes_tool_name_description(self):
        msg = _build_confirmation_message("deleteStream", {"streamId": "123"})
        assert "删除 Stream" in msg

    def test_includes_args_summary(self):
        msg = _build_confirmation_message("stopTask", {"taskId": "456"})
        assert "taskId" in msg
        assert "456" in msg

    def test_warning_icon_present(self):
        msg = _build_confirmation_message("startStream", {})
        assert "⚠️" in msg

    def test_unknown_tool_uses_tool_name_as_fallback(self):
        msg = _build_confirmation_message("unknownTool", {})
        assert "unknownTool" in msg

    def test_empty_args_handled_gracefully(self):
        msg = _build_confirmation_message("deleteTask", {})
        assert "删除 Task" in msg


class TestFormatArgsSummary:
    def test_empty_args(self):
        result = _format_args_summary({})
        assert "无参数" in result

    def test_shows_key_value_pairs(self):
        result = _format_args_summary({"streamId": "abc", "version": 2})
        assert "streamId" in result
        assert "abc" in result

    def test_truncates_long_values(self):
        long_value = "x" * 200
        result = _format_args_summary({"schema": long_value})
        assert len(result) < 300

    def test_caps_at_five_params(self):
        args = {f"param{i}": i for i in range(10)}
        result = _format_args_summary(args)
        # Should mention it has more params
        assert "10" in result


# --- Middleware sync tests ---


class TestTehubConfirmationMiddlewareSync:
    def test_readonly_tool_passes_through(self):
        mw = TehubConfirmationMiddleware()
        req = _make_tool_call_request("getByTokenName", {"tokenName": "my-token"})
        expected = MagicMock()
        handler = MagicMock(return_value=expected)

        result = mw.wrap_tool_call(req, handler)

        handler.assert_called_once_with(req)
        assert result is expected

    def test_dangerous_tool_blocked_before_handler(self):
        mw = TehubConfirmationMiddleware()
        req = _make_tool_call_request("deleteStream", {"streamId": "123"})
        handler = MagicMock()

        result = mw.wrap_tool_call(req, handler)

        handler.assert_not_called()
        assert isinstance(result, Command)

    def test_dangerous_tool_returns_command_with_goto_end(self):
        mw = TehubConfirmationMiddleware()
        req = _make_tool_call_request("createToken", {"tokenName": "new-token"})
        handler = MagicMock()

        result = mw.wrap_tool_call(req, handler)

        assert isinstance(result, Command)
        assert result.goto is not None

    def test_dangerous_tool_includes_tool_message(self):
        mw = TehubConfirmationMiddleware()
        req = _make_tool_call_request("publishStreamConfig", {"streamId": "42"}, call_id="cid_1")
        handler = MagicMock()

        result = mw.wrap_tool_call(req, handler)

        assert isinstance(result, Command)
        messages = result.update.get("messages", [])
        assert len(messages) == 1
        msg = messages[0]
        assert isinstance(msg, ToolMessage)
        assert msg.tool_call_id == "cid_1"
        assert "⚠️" in msg.content

    def test_non_tehub_tool_passes_through(self):
        mw = TehubConfirmationMiddleware()
        req = _make_tool_call_request("bash", {"command": "ls"})
        expected = MagicMock()
        handler = MagicMock(return_value=expected)

        result = mw.wrap_tool_call(req, handler)

        handler.assert_called_once_with(req)
        assert result is expected

    @pytest.mark.parametrize("tool_name", sorted(TEHUB_DANGEROUS_TOOLS))
    def test_all_dangerous_tools_are_blocked(self, tool_name):
        mw = TehubConfirmationMiddleware()
        req = _make_tool_call_request(tool_name)
        handler = MagicMock()

        result = mw.wrap_tool_call(req, handler)

        handler.assert_not_called()
        assert isinstance(result, Command)

    @pytest.mark.parametrize("tool_name", sorted(TEHUB_READONLY_TOOLS))
    def test_all_readonly_tools_pass_through(self, tool_name):
        mw = TehubConfirmationMiddleware()
        req = _make_tool_call_request(tool_name)
        expected = MagicMock()
        handler = MagicMock(return_value=expected)

        result = mw.wrap_tool_call(req, handler)

        handler.assert_called_once_with(req)
        assert result is expected


# --- Middleware async tests ---


class TestTehubConfirmationMiddlewareAsync:
    def test_async_readonly_tool_passes_through(self):
        mw = TehubConfirmationMiddleware()
        req = _make_tool_call_request("getDraftStreamConfig", {"streamId": "99"})
        expected = MagicMock()

        async def handler(r):
            return expected

        result = asyncio.run(mw.awrap_tool_call(req, handler))
        assert result is expected

    def test_async_dangerous_tool_blocked(self):
        mw = TehubConfirmationMiddleware()
        req = _make_tool_call_request("killTask", {"taskId": "77"})

        async def handler(r):
            return MagicMock()

        result = asyncio.run(mw.awrap_tool_call(req, handler))
        assert isinstance(result, Command)

    def test_async_dangerous_includes_warning_message(self):
        mw = TehubConfirmationMiddleware()
        req = _make_tool_call_request("deleteTask", {"taskId": "88"}, call_id="async_id")

        async def handler(r):
            return MagicMock()

        result = asyncio.run(mw.awrap_tool_call(req, handler))
        messages = result.update.get("messages", [])
        assert len(messages) == 1
        assert "⚠️" in messages[0].content
        assert messages[0].tool_call_id == "async_id"
