import importlib.util
import json
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.event import MessageChain, MessageEventResult, ResultContentType
from astrbot.api.message_components import At, Plain, Record, Reply

plugin_path = Path(__file__).resolve().parents[1] / "main.py"
plugin_spec = importlib.util.spec_from_file_location(
    "segmented_reply_plugin", plugin_path
)
plugin_module = importlib.util.module_from_spec(plugin_spec)
plugin_spec.loader.exec_module(plugin_module)
SegmentedReplyPlugin = plugin_module.SegmentedReplyPlugin


def test_splitter_settings_have_mode_conditions():
    schema = json.loads(
        (plugin_path.parent / "_conf_schema.json").read_text(encoding="utf-8")
    )

    settings = schema["settings"]["items"]
    assert list(schema) == [
        "enable",
        "settings",
        "ellipsis_optimization",
        "skip_first_delay",
    ]
    assert schema["ellipsis_optimization"]["default"] is True
    assert schema["skip_first_delay"]["default"] is True
    assert "ellipsis_optimization" not in settings
    assert settings["regex"]["condition"] == {"split_mode": "regex"}
    assert settings["split_words"]["condition"] == {"split_mode": "words"}


def test_default_regex_matches_schema_and_compiles():
    schema = json.loads(
        (plugin_path.parent / "_conf_schema.json").read_text(encoding="utf-8")
    )
    default_regex = schema["settings"]["items"]["regex"]["default"]

    assert default_regex == plugin_module.DEFAULT_REGEX
    assert "…" not in default_regex
    assert re.compile(default_regex)


def test_ellipsis_only_splits_between_text_and_outside_protected_groups():
    plugin = make_plugin(
        split_mode="regex",
        regex=plugin_module.DEFAULT_REGEX,
        content_cleanup_rule="",
    )

    assert plugin._split_text("……正文。后续……内容。") == [
        "……正文。",
        "后续……",
        "内容。",
    ]
    assert plugin._split_text("“等待……完成。”然后……继续。") == [
        "“等待……完成。”然后……",
        "继续。",
    ]
    assert plugin._split_text("正文……。下一句。") == ["正文……。", "下一句。"]
    assert plugin._split_text("，……正文。") == ["，……正文。"]
    assert plugin._split_text("正文……内容。") == ["正文……", "内容。"]
    assert plugin._split_text("正文，……内容。") == ["正文，……内容。"]
    assert plugin._split_text("（123）……456。") == ["（123）……456。"]
    assert plugin._split_text("正文， ……内容。") == ["正文， ……内容。"]


def test_ellipsis_optimization_can_be_disabled():
    plugin = make_plugin(
        split_mode="regex",
        regex=plugin_module.DEFAULT_REGEX,
        content_cleanup_rule="",
        ellipsis_optimization=False,
    )

    assert plugin.ellipsis_optimization is False
    assert plugin._split_text("前文……后文。") == ["前文……后文。"]


def make_plugin(*, ellipsis_optimization=None, skip_first_delay=None, **overrides):
    settings = {
        "only_llm_result": False,
        "split_mode": "words",
        "split_words": ["。", "！？"],
        "interval_method": "random",
        "interval": "0,0",
        **overrides,
    }
    config = {"enable": True, "settings": settings}
    if ellipsis_optimization is not None:
        config["ellipsis_optimization"] = ellipsis_optimization
    if skip_first_delay is not None:
        config["skip_first_delay"] = skip_first_delay
    context = SimpleNamespace(
        get_config=lambda: {"platform_settings": {"segmented_reply": {"enable": False}}}
    )
    return SegmentedReplyPlugin(context, config)


def test_legacy_flat_settings_remain_supported():
    plugin = SegmentedReplyPlugin(
        SimpleNamespace(),
        {
            "enable": True,
            "only_llm_result": False,
            "words_count_threshold": 123,
        },
    )

    assert plugin.only_llm_result is False
    assert plugin.words_count_threshold == 123


def make_event(
    chain,
    platform="aiocqhttp",
    content_type=ResultContentType.GENERAL_RESULT,
):
    result = MessageEventResult(
        chain=chain,
        result_content_type=content_type,
    )
    event = SimpleNamespace(
        get_result=lambda: result,
        get_platform_name=lambda: platform,
        send=AsyncMock(),
    )
    return event, result


def test_default_text_length_threshold_is_300():
    plugin = make_plugin()

    assert plugin.words_count_threshold == 300
    assert plugin.content_cleanup_rule == "。"


@pytest.mark.asyncio
async def test_word_splitter_removes_longest_matching_word():
    plugin = make_plugin()
    event, result = make_event([Plain("第一段。第二段！？尾巴")])

    await plugin.prepare_segmented_reply(event)

    assert [part.text for part in result.chain] == ["第一段", "第二段", "尾巴"]
    assert hasattr(event, "_segmented_reply_original_send")


@pytest.mark.asyncio
async def test_only_llm_setting_accepts_model_results():
    plugin = make_plugin(only_llm_result=True)
    event, result = make_event(
        [Plain("第一段。第二段。")],
        content_type=ResultContentType.LLM_RESULT,
    )

    await plugin.prepare_segmented_reply(event)

    assert [part.text for part in result.chain] == ["第一段", "第二段"]


@pytest.mark.asyncio
async def test_long_text_is_left_as_one_component():
    plugin = make_plugin(words_count_threshold=3)
    original = Plain("一。二。三。")
    event, result = make_event([original])

    await plugin.prepare_segmented_reply(event)

    assert result.chain == [original]


@pytest.mark.asyncio
async def test_regex_mode_and_cleanup_rule_match_native_behavior():
    plugin = make_plugin(
        split_mode="regex",
        regex=r".*?[。？！~…]+|.+$",
        content_cleanup_rule="[。？！]",
    )
    event, result = make_event([Plain("第一段。第二段？")])

    await plugin.prepare_segmented_reply(event)

    assert [part.text for part in result.chain] == ["第一段", "第二段"]


@pytest.mark.asyncio
async def test_excluded_platform_and_non_model_result_are_untouched():
    plugin = make_plugin(only_llm_result=True)
    original = Plain("第一段。第二段。")
    excluded_event, excluded_result = make_event([original], "dingtalk")
    general_event, general_result = make_event([original])

    await plugin.prepare_segmented_reply(excluded_event)
    await plugin.prepare_segmented_reply(general_event)

    assert excluded_result.chain == [original]
    assert general_result.chain == [original]
    assert not hasattr(excluded_event, "_segmented_reply_original_send")
    assert not hasattr(general_event, "_segmented_reply_original_send")


@pytest.mark.asyncio
async def test_native_feature_takes_precedence():
    plugin = make_plugin()
    plugin.context.get_config = lambda: {
        "platform_settings": {"segmented_reply": {"enable": True}}
    }
    original = Plain("第一段。第二段。")
    event, result = make_event([original])
    original_send = event.send

    await plugin.prepare_segmented_reply(event)

    assert result.chain == [original]
    assert event.send is original_send


def test_log_interval_uses_ascii_words_and_unicode_characters(monkeypatch):
    plugin = make_plugin(interval_method="log", log_base=2)
    ranges = []

    def capture_range(start, end):
        ranges.append((start, end))
        return start

    monkeypatch.setattr(plugin_module.random, "uniform", capture_range)

    assert plugin._component_interval(Plain("one two")) == pytest.approx(1.5849625)
    assert plugin._component_interval(Plain("你好")) == pytest.approx(1.5849625)
    assert ranges[0] == pytest.approx((1.5849625, 2.0849625))
    assert ranges[1] == pytest.approx((1.5849625, 2.0849625))


@pytest.mark.asyncio
async def test_sender_preserves_headers_and_sends_record_separately(monkeypatch):
    plugin = make_plugin()
    monkeypatch.setattr(plugin_module.asyncio, "sleep", AsyncMock())
    event, _ = make_event([])
    original_send = event.send
    plugin._install_segmented_sender(event)
    message = MessageChain(
        [At(qq="1"), Reply(id="2"), Plain("one"), Record(file="a"), Plain("two")]
    )

    await event.send(message)
    await plugin.restore_event_send(event)

    sent_chains = [call.args[0].chain for call in original_send.await_args_list]
    assert [[type(part) for part in chain] for chain in sent_chains] == [
        [At, Reply, Plain],
        [Record],
        [Plain],
    ]
    assert event.send is original_send


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("skip_first_delay", "expected_actions"),
    [
        (None, ["send:first", "sleep:1.5", "send:second"]),
        (False, ["sleep:1.5", "send:first", "sleep:1.5", "send:second"]),
    ],
)
async def test_first_segment_delay_can_be_skipped(
    monkeypatch, skip_first_delay, expected_actions
):
    plugin = make_plugin(skip_first_delay=skip_first_delay)
    actions = []
    sleep = AsyncMock(side_effect=lambda seconds: actions.append(f"sleep:{seconds}"))
    monkeypatch.setattr(plugin_module.asyncio, "sleep", sleep)
    monkeypatch.setattr(plugin, "_component_interval", lambda component: 1.5)
    event, _ = make_event([])
    event.send.side_effect = lambda message: actions.append(
        f"send:{message.chain[0].text}"
    )
    original_send = event.send
    plugin._install_segmented_sender(event)

    await event.send(MessageChain([Plain("first"), Plain("second")]))

    assert actions == expected_actions
    assert original_send.await_count == 2
