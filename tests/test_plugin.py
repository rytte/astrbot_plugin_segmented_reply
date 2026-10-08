import asyncio
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
    assert settings["acceleration_ratio"]["type"] == "float"
    assert settings["acceleration_ratio"]["default"] == 1.0
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
    session_id="aiocqhttp:FriendMessage:1",
):
    result = MessageEventResult(
        chain=chain,
        result_content_type=content_type,
    )
    event = SimpleNamespace(
        unified_msg_origin=session_id,
        get_result=lambda: result,
        get_platform_name=lambda: platform,
        send=AsyncMock(),
    )
    return event, result


def test_default_text_length_threshold_is_300():
    plugin = make_plugin()

    assert plugin.words_count_threshold == 300
    assert plugin.content_cleanup_rule == "。"
    assert plugin.acceleration_ratio == 1.0


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


@pytest.mark.parametrize("ratio", [0, 0.7, 1])
def test_acceleration_ratio_accepts_values_between_zero_and_one(ratio):
    assert make_plugin(acceleration_ratio=ratio).acceleration_ratio == ratio


@pytest.mark.parametrize(
    "ratio",
    [
        -0.1,
        1.1,
        float("inf"),
        float("-inf"),
        float("nan"),
        None,
        "invalid",
        True,
        False,
    ],
)
def test_invalid_acceleration_ratio_fails_fast(ratio):
    with pytest.raises(ValueError, match="settings.acceleration_ratio"):
        make_plugin(acceleration_ratio=ratio)


@pytest.mark.asyncio
@pytest.mark.parametrize("ratio", [1, 0.7, 0])
@pytest.mark.parametrize("interval_method", ["random", "log"])
async def test_new_messages_scale_pending_delays_without_compounding(
    monkeypatch, ratio, interval_method
):
    plugin = make_plugin(
        acceleration_ratio=ratio,
        interval_method=interval_method,
        interval="4,4",
        log_base=2,
    )
    sleep = AsyncMock()
    monkeypatch.setattr(plugin_module.asyncio, "sleep", sleep)
    monkeypatch.setattr(plugin_module.random, "uniform", lambda start, end: start)
    event, _ = make_event([])
    incoming, _ = make_event([])

    async def receive_new_messages(message):
        await plugin.accelerate_pending_reply(incoming)
        await plugin.accelerate_pending_reply(incoming)

    original_send = event.send
    original_send.side_effect = receive_new_messages
    plugin._install_segmented_sender(event)

    await event.send(MessageChain([Plain("first"), Plain("second"), Plain("third")]))

    interval = 4 if interval_method == "random" else 1
    assert [call.args[0] for call in sleep.await_args_list] == pytest.approx(
        [interval * ratio, interval * ratio]
    )
    assert [call.args[0].chain[0].text for call in original_send.await_args_list] == [
        "first",
        "second",
        "third",
    ]
    assert plugin._active_replies == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("ratio", [0.7, 0])
@pytest.mark.parametrize("skip_first_delay", [True, False])
async def test_current_wait_scales_only_the_time_still_remaining(
    monkeypatch, ratio, skip_first_delay
):
    plugin = make_plugin(acceleration_ratio=ratio, skip_first_delay=skip_first_delay)
    monkeypatch.setattr(plugin, "_component_interval", lambda component: 4)
    sleep = AsyncMock()
    monkeypatch.setattr(plugin_module.asyncio, "sleep", sleep)
    clock = SimpleNamespace(now=10.0)
    clock.time = lambda: clock.now
    event, _ = make_event([])
    incoming, _ = make_event([])
    intervals = []

    async def trigger_acceleration(awaitable, timeout):
        awaitable.close()
        intervals.append(timeout)
        clock.now += 1
        await plugin.accelerate_pending_reply(incoming)
        return True

    monkeypatch.setattr(plugin_module.asyncio, "wait_for", trigger_acceleration)
    original_send = event.send
    plugin._install_segmented_sender(event)

    with monkeypatch.context() as clock_patch:
        clock_patch.setattr(plugin_module.asyncio, "get_running_loop", lambda: clock)
        await event.send(
            MessageChain([Plain("first"), Plain("second"), Plain("third")])
        )

    remaining_intervals = [3 * ratio, 4 * ratio]
    if not skip_first_delay:
        remaining_intervals.append(4 * ratio)
    assert intervals == [4]
    assert [call.args[0] for call in sleep.await_args_list] == pytest.approx(
        remaining_intervals
    )
    assert original_send.await_count == 3
    assert plugin._active_replies == {}


@pytest.mark.asyncio
async def test_acceleration_does_not_carry_over_to_next_reply(monkeypatch):
    plugin = make_plugin(acceleration_ratio=0.7)
    monkeypatch.setattr(plugin, "_component_interval", lambda component: 4)
    sleep = AsyncMock()
    monkeypatch.setattr(plugin_module.asyncio, "sleep", sleep)
    event, _ = make_event([])
    incoming, _ = make_event([])
    intervals = []

    async def wait_without_new_message(awaitable, timeout):
        awaitable.close()
        intervals.append(timeout)
        raise asyncio.TimeoutError

    async def receive_new_message(message):
        await plugin.accelerate_pending_reply(incoming)

    monkeypatch.setattr(plugin_module.asyncio, "wait_for", wait_without_new_message)
    original_send = event.send
    original_send.side_effect = receive_new_message
    plugin._install_segmented_sender(event)
    message = MessageChain([Plain("first"), Plain("second"), Plain("third")])

    await event.send(message)
    assert [call.args[0] for call in sleep.await_args_list] == pytest.approx([2.8, 2.8])
    assert plugin._active_replies == {}

    original_send.side_effect = None
    next_event, _ = make_event([])
    next_original_send = next_event.send
    plugin._install_segmented_sender(next_event)
    await plugin.accelerate_pending_reply(incoming)
    await next_event.send(message)

    assert intervals == [4, 4]
    assert sleep.await_count == 2
    assert original_send.await_count == 3
    assert next_original_send.await_count == 3
    assert plugin._active_replies == {}


@pytest.mark.asyncio
async def test_acceleration_survives_separate_record_delivery(monkeypatch):
    plugin = make_plugin(acceleration_ratio=0.7)
    monkeypatch.setattr(plugin, "_component_interval", lambda component: 4)
    sleep = AsyncMock()
    monkeypatch.setattr(plugin_module.asyncio, "sleep", sleep)
    event, _ = make_event([])
    incoming, _ = make_event([])

    async def receive_new_message(message):
        await plugin.accelerate_pending_reply(incoming)

    original_send = event.send
    original_send.side_effect = receive_new_message
    plugin._install_segmented_sender(event)

    await event.send(MessageChain([Record(file="audio")]))
    original_send.side_effect = None
    await event.send(MessageChain([Plain("first"), Plain("second")]))
    await plugin.restore_event_send(event)

    assert [call.args[0] for call in sleep.await_args_list] == pytest.approx([2.8])
    assert original_send.await_count == 3
    assert event.send is original_send
    assert plugin._active_replies == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("same_session", [False, True])
async def test_acceleration_interrupts_only_matching_session_waits(
    monkeypatch, same_session
):
    plugin = make_plugin(acceleration_ratio=0)
    monkeypatch.setattr(plugin, "_component_interval", lambda component: 60)
    first, _ = make_event([], session_id="session:first")
    second, _ = make_event(
        [], session_id="session:first" if same_session else "session:second"
    )
    unrelated, _ = make_event([], session_id="session:unrelated")
    plugin._install_segmented_sender(first)
    plugin._install_segmented_sender(second)
    original_wait_for = asyncio.wait_for
    waits_started = asyncio.Event()
    waiting_count = 0

    async def watch_wait(awaitable, timeout):
        nonlocal waiting_count
        waiting_count += 1
        if waiting_count == 2:
            waits_started.set()
        return await original_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(plugin_module.asyncio, "wait_for", watch_wait)
    message = MessageChain([Plain("first"), Plain("second")])
    first_delivery = asyncio.create_task(first.send(message))
    second_delivery = asyncio.create_task(second.send(message))
    try:
        await original_wait_for(waits_started.wait(), timeout=2)
        await plugin.accelerate_pending_reply(unrelated)
        assert not first_delivery.done()
        assert not second_delivery.done()

        await plugin.accelerate_pending_reply(first)
        await original_wait_for(first_delivery, timeout=2)
        if not same_session:
            assert not second_delivery.done()
            await plugin.accelerate_pending_reply(second)
        await original_wait_for(second_delivery, timeout=2)
    finally:
        for delivery in (first_delivery, second_delivery):
            if not delivery.done():
                delivery.cancel()
        await asyncio.gather(first_delivery, second_delivery, return_exceptions=True)

    assert plugin._active_replies == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RuntimeError("send failed"), asyncio.CancelledError()]
)
async def test_sender_releases_acceleration_state_when_send_fails(failure):
    plugin = make_plugin(acceleration_ratio=0.7)
    event, _ = make_event([])
    event.send.side_effect = failure
    plugin._install_segmented_sender(event)

    with pytest.raises(type(failure)):
        await event.send(MessageChain([Plain("first"), Plain("second")]))

    assert plugin._active_replies == {}


@pytest.mark.asyncio
async def test_cancelled_wait_releases_acceleration_state(monkeypatch):
    plugin = make_plugin(acceleration_ratio=0.7)
    monkeypatch.setattr(plugin, "_component_interval", lambda component: 60)
    event, _ = make_event([])
    plugin._install_segmented_sender(event)
    original_wait_for = asyncio.wait_for
    wait_started = asyncio.Event()

    async def watch_wait(awaitable, timeout):
        wait_started.set()
        return await original_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(plugin_module.asyncio, "wait_for", watch_wait)
    delivery = asyncio.create_task(
        event.send(MessageChain([Plain("first"), Plain("second")]))
    )
    try:
        await original_wait_for(wait_started.wait(), timeout=2)
        delivery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await delivery
    finally:
        if not delivery.done():
            delivery.cancel()
        await asyncio.gather(delivery, return_exceptions=True)

    assert plugin._active_replies == {}
