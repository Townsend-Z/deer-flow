"""Tests for the tehub-pipeline subagent configuration."""

from __future__ import annotations


class TestTehubPipelineAgentImport:
    def test_config_is_importable(self):
        from deerflow.subagents.builtins.tehub_pipeline_agent import TEHUB_PIPELINE_CONFIG

        assert TEHUB_PIPELINE_CONFIG is not None

    def test_config_name(self):
        from deerflow.subagents.builtins.tehub_pipeline_agent import TEHUB_PIPELINE_CONFIG

        assert TEHUB_PIPELINE_CONFIG.name == "tehub-pipeline"

    def test_config_has_description(self):
        from deerflow.subagents.builtins.tehub_pipeline_agent import TEHUB_PIPELINE_CONFIG

        assert TEHUB_PIPELINE_CONFIG.description
        assert len(TEHUB_PIPELINE_CONFIG.description) > 20

    def test_config_has_system_prompt(self):
        from deerflow.subagents.builtins.tehub_pipeline_agent import TEHUB_PIPELINE_CONFIG

        assert TEHUB_PIPELINE_CONFIG.system_prompt
        assert len(TEHUB_PIPELINE_CONFIG.system_prompt) > 100

    def test_system_prompt_references_mcp_tools(self):
        from deerflow.subagents.builtins.tehub_pipeline_agent import TEHUB_PIPELINE_CONFIG

        prompt = TEHUB_PIPELINE_CONFIG.system_prompt
        # Core tool names should be referenced in the system prompt
        for tool in ("createToken", "createStreamInfo", "createTaskInfo", "publishStreamConfig"):
            assert tool in prompt, f"Expected '{tool}' in system prompt"

    def test_system_prompt_references_workflows(self):
        from deerflow.subagents.builtins.tehub_pipeline_agent import TEHUB_PIPELINE_CONFIG

        prompt = TEHUB_PIPELINE_CONFIG.system_prompt
        assert "Stream" in prompt
        assert "Task" in prompt

    def test_system_prompt_references_confirmation_rules(self):
        from deerflow.subagents.builtins.tehub_pipeline_agent import TEHUB_PIPELINE_CONFIG

        prompt = TEHUB_PIPELINE_CONFIG.system_prompt
        assert "ask_clarification" in prompt or "确认" in prompt

    def test_timeout_seconds_is_reasonable(self):
        from deerflow.subagents.builtins.tehub_pipeline_agent import TEHUB_PIPELINE_CONFIG

        # Pipeline workflows can be long; at least 15 minutes
        assert TEHUB_PIPELINE_CONFIG.timeout_seconds >= 900

    def test_max_turns_is_sufficient(self):
        from deerflow.subagents.builtins.tehub_pipeline_agent import TEHUB_PIPELINE_CONFIG

        # Multi-step workflows need enough turns
        assert TEHUB_PIPELINE_CONFIG.max_turns >= 50


class TestTehubPipelineInRegistry:
    def test_registered_in_builtin_subagents(self):
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS

        assert "tehub-pipeline" in BUILTIN_SUBAGENTS

    def test_registry_returns_config(self):
        from deerflow.subagents.registry import get_subagent_config

        config = get_subagent_config("tehub-pipeline")
        assert config is not None
        assert config.name == "tehub-pipeline"

    def test_list_subagents_includes_tehub(self):
        from deerflow.subagents.registry import list_subagents

        names = {cfg.name for cfg in list_subagents()}
        assert "tehub-pipeline" in names

    def test_existing_agents_still_registered(self):
        from deerflow.subagents.builtins import BUILTIN_SUBAGENTS

        assert "general-purpose" in BUILTIN_SUBAGENTS
        assert "bash" in BUILTIN_SUBAGENTS

    def test_tehub_pipeline_config_exported(self):
        from deerflow.subagents.builtins import TEHUB_PIPELINE_CONFIG

        assert TEHUB_PIPELINE_CONFIG is not None


class TestSubagentConfigDataclass:
    """Verify the SubagentConfig dataclass is correctly populated."""

    def test_name_is_string(self):
        from deerflow.subagents.builtins.tehub_pipeline_agent import TEHUB_PIPELINE_CONFIG

        assert isinstance(TEHUB_PIPELINE_CONFIG.name, str)

    def test_description_is_string(self):
        from deerflow.subagents.builtins.tehub_pipeline_agent import TEHUB_PIPELINE_CONFIG

        assert isinstance(TEHUB_PIPELINE_CONFIG.description, str)

    def test_system_prompt_is_string(self):
        from deerflow.subagents.builtins.tehub_pipeline_agent import TEHUB_PIPELINE_CONFIG

        assert isinstance(TEHUB_PIPELINE_CONFIG.system_prompt, str)

    def test_max_turns_is_int(self):
        from deerflow.subagents.builtins.tehub_pipeline_agent import TEHUB_PIPELINE_CONFIG

        assert isinstance(TEHUB_PIPELINE_CONFIG.max_turns, int)

    def test_timeout_seconds_is_int(self):
        from deerflow.subagents.builtins.tehub_pipeline_agent import TEHUB_PIPELINE_CONFIG

        assert isinstance(TEHUB_PIPELINE_CONFIG.timeout_seconds, int)

    def test_tools_field_is_none_or_list(self):
        from deerflow.subagents.builtins.tehub_pipeline_agent import TEHUB_PIPELINE_CONFIG

        assert TEHUB_PIPELINE_CONFIG.tools is None or isinstance(TEHUB_PIPELINE_CONFIG.tools, list)
