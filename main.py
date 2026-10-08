"""Provide configurable segmented replies when AstrBot's built-in feature is off."""

from __future__ import annotations

import asyncio
import math
import random
import re
import traceback
import unicodedata
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, ResultContentType, filter
from astrbot.api.message_components import At, Plain, Record, Reply
from astrbot.api.star import Context, Star

EXCLUDED_PLATFORMS = {
    "qq_official_webhook",
    "weixin_official_account",
    "dingtalk",
}
DEFAULT_REGEX = r"""(?>(?<=[A-Za-z])['’](?=[A-Za-z])|\([^()]*\)|（[^（）]*）|\[[^\[\]]*\]|\{[^{}]*\}|「[^「」]*」|『[^『』]*』|【[^【】]*】|〔[^〔〕]*〕|《[^《》]*》|〈[^〈〉]*〉|"(?:\\.|[^"\\])*"|'(?:\\.|(?<=[A-Za-z])'(?=[A-Za-z])|[^'\\])*'|“(?:\\.|[^“”\\])*”|‘(?:\\.|(?<=[A-Za-z])’(?=[A-Za-z])|[^‘’\\])*’|[^。？！~()（）\[\]{}「」『』【】〔〕《》〈〉"'“”‘’])*?[。？！~]+|.+\Z"""
PROTECTED_TEXT_PATTERN = re.compile(
    r"""(?>(?<=[A-Za-z])['’](?=[A-Za-z])|\([^()]*\)|（[^（）]*）|\[[^\[\]]*\]|\{[^{}]*\}|「[^「」]*」|『[^『』]*』|【[^【】]*】|〔[^〔〕]*〕|《[^《》]*》|〈[^〈〉]*〉|"(?:\\.|[^"\\])*"|'(?:\\.|(?<=[A-Za-z])'(?=[A-Za-z])|[^'\\])*'|“(?:\\.|[^“”\\])*”|‘(?:\\.|(?<=[A-Za-z])’(?=[A-Za-z])|[^‘’\\])*’)"""
)
SENTENCE_ENDINGS = "。？！~"


class SegmentedReplyPlugin(Star):
    """Split eligible replies into sequential messages with configurable delays."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.ellipsis_optimization = bool(config.get("ellipsis_optimization", True))
        self.skip_first_delay = bool(config.get("skip_first_delay", True))
        settings = config.get("settings")
        if not isinstance(settings, dict) or not settings:
            settings = config
        raw_acceleration_ratio = config.get("acceleration_ratio", 1.0)
        try:
            self.acceleration_ratio = float(raw_acceleration_ratio)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "acceleration_ratio must be a finite number between 0 and 1"
            ) from exc
        if (
            isinstance(raw_acceleration_ratio, bool)
            or not math.isfinite(self.acceleration_ratio)
            or not 0 <= self.acceleration_ratio <= 1
        ):
            raise ValueError(
                "acceleration_ratio must be a finite number between 0 and 1"
            )
        self._active_replies: dict[str, set[asyncio.Event]] = {}
        self.only_llm_result = bool(settings.get("only_llm_result", True))
        self.interval_method = settings.get("interval_method", "random")
        if self.interval_method not in {"random", "log"}:
            self.interval_method = "random"

        try:
            self.log_base = float(settings.get("log_base", 2.6))
            if self.log_base <= 0 or self.log_base == 1:
                raise ValueError("log_base must be positive and not equal to 1")
        except (TypeError, ValueError):
            logger.warning("Invalid segmented-reply log_base; using 2.6")
            self.log_base = 2.6

        try:
            raw_interval = str(settings.get("interval", "1.5,3.5"))
            self.interval = [
                float(value) for value in raw_interval.replace(" ", "").split(",")
            ]
            if len(self.interval) != 2:
                raise ValueError("interval must contain two values")
        except (TypeError, ValueError) as exc:
            logger.error("Failed to parse segmented-reply interval: %s", exc)
            self.interval = [1.5, 3.5]
        logger.info("Segmented-reply interval: %s", self.interval)

        try:
            self.words_count_threshold = int(settings.get("words_count_threshold", 300))
        except (TypeError, ValueError):
            self.words_count_threshold = 300
        self.split_mode = settings.get("split_mode", "regex")
        if self.split_mode not in {"regex", "words"}:
            self.split_mode = "regex"
        self.regex = str(settings.get("regex", DEFAULT_REGEX))
        words = settings.get("split_words", ["。", "？", "！", "~", "…"]) or []
        self.split_words = [word for word in words if isinstance(word, str) and word]
        if self.split_words:
            escaped_words = sorted(
                (re.escape(word) for word in self.split_words),
                key=len,
                reverse=True,
            )
            self.split_words_pattern = re.compile(
                f"(.*?({'|'.join(escaped_words)})|.+$)",
                re.DOTALL,
            )
        else:
            self.split_words_pattern = None
        self.content_cleanup_rule = settings.get("content_cleanup_rule", "。") or ""

    def _native_segmentation_enabled(self) -> bool:
        """Check whether AstrBot's built-in segmented reply currently owns delivery."""
        try:
            settings = self.context.get_config().get("platform_settings", {})
            return bool(settings.get("segmented_reply", {}).get("enable", False))
        except (AttributeError, TypeError):
            return False

    def _split_text_by_words(self, text: str) -> list[str]:
        """Split text at configured words, removing the matched word from each part."""
        if not self.split_words_pattern:
            return [text]

        result = []
        for match in self.split_words_pattern.findall(text):
            content = match[0] if isinstance(match, tuple) else match
            if not isinstance(content, str):
                continue
            for word in self.split_words:
                if content.endswith(word):
                    content = content[: -len(word)]
                    break
            if content.strip():
                result.append(content)
        return result or [text]

    def _split_text(self, text: str) -> list[str]:
        """Apply the configured splitter, falling back to AstrBot's default regex."""
        if self.split_mode == "words":
            return self._split_text_by_words(text)
        try:
            segments = re.findall(self.regex, text, re.DOTALL | re.MULTILINE)
        except re.error:
            logger.error(
                "Invalid segmented-reply regular expression; using the default: %s",
                traceback.format_exc(),
            )
            segments = re.findall(DEFAULT_REGEX, text, re.DOTALL | re.MULTILINE)
        if not self.ellipsis_optimization:
            return segments
        return [
            part
            for segment in segments
            for part in self._split_text_by_ellipsis(segment)
        ]

    @staticmethod
    def _has_punctuation_before(text: str, index: int) -> bool:
        """Check whether punctuation is the nearest non-whitespace character."""
        previous = index - 1
        while previous >= 0 and text[previous].isspace():
            previous -= 1
        return previous >= 0 and unicodedata.category(text[previous]).startswith("P")

    def _split_text_by_ellipsis(self, text: str) -> list[str]:
        """Split ellipses only when words appear on both sides of the boundary."""
        segments = []
        segment_start = 0
        index = 0
        has_word_before = False

        while index < len(text):
            protected_match = PROTECTED_TEXT_PATTERN.match(text, index)
            if protected_match:
                has_word_before = has_word_before or any(
                    character.isalnum() for character in protected_match.group()
                )
                index = protected_match.end()
                continue

            if text[index] == "…":
                ellipsis_end = index + 1
                while ellipsis_end < len(text) and text[ellipsis_end] == "…":
                    ellipsis_end += 1

                has_word_after = False
                lookahead = ellipsis_end
                while lookahead < len(text):
                    protected_match = PROTECTED_TEXT_PATTERN.match(text, lookahead)
                    if protected_match:
                        if any(
                            character.isalnum() for character in protected_match.group()
                        ):
                            has_word_after = True
                            break
                        lookahead = protected_match.end()
                        continue

                    character = text[lookahead]
                    if character in SENTENCE_ENDINGS:
                        break
                    if character.isalnum():
                        has_word_after = True
                        break
                    lookahead += 1

                if (
                    has_word_before
                    and has_word_after
                    and not self._has_punctuation_before(text, index)
                ):
                    segments.append(text[segment_start:ellipsis_end])
                    segment_start = ellipsis_end
                    has_word_before = False
                index = ellipsis_end
                continue

            has_word_before = has_word_before or text[index].isalnum()
            index += 1

        segments.append(text[segment_start:])
        return segments

    @staticmethod
    def _word_count(text: str) -> int:
        """Count ASCII words or alphanumeric characters, matching AstrBot's behavior."""
        if all(ord(character) < 128 for character in text):
            return len(text.split())
        return sum(character.isalnum() for character in text)

    def _component_interval(self, component: Any) -> float:
        """Return the configured delay before sending one message component."""
        if self.interval_method == "log":
            if isinstance(component, Plain):
                interval = math.log(self._word_count(component.text) + 1, self.log_base)
                return random.uniform(interval, interval + 0.5)
            return random.uniform(1, 1.75)
        return random.uniform(self.interval[0], self.interval[1])

    def _install_segmented_sender(self, event: AstrMessageEvent) -> None:
        """Wrap one event's sender so its decorated components are delivered in order."""
        if hasattr(event, "_segmented_reply_original_send"):
            return
        original_send = event.send
        reply_accelerated = False

        async def send_segmented(message: MessageChain | None) -> None:
            nonlocal reply_accelerated
            if message is None or not message.chain:
                await original_send(message)
                return

            headers = [
                component
                for component in message.chain
                if isinstance(component, (At, Reply))
            ]
            components = [
                component
                for component in message.chain
                if not isinstance(component, (At, Reply))
            ]
            if not components:
                await original_send(message)
                return

            session_id = event.unified_msg_origin
            acceleration = asyncio.Event() if self.acceleration_ratio < 1 else None
            if acceleration is not None:
                if reply_accelerated:
                    acceleration.set()
                self._active_replies.setdefault(session_id, set()).add(acceleration)
            try:
                for index, component in enumerate(components):
                    if index > 0 or not self.skip_first_delay:
                        interval = self._component_interval(component)
                        if acceleration is None or interval <= 0:
                            await asyncio.sleep(interval)
                        elif acceleration.is_set():
                            await asyncio.sleep(interval * self.acceleration_ratio)
                        else:
                            loop = asyncio.get_running_loop()
                            deadline = loop.time() + interval
                            try:
                                await asyncio.wait_for(
                                    acceleration.wait(), timeout=interval
                                )
                            except asyncio.TimeoutError:
                                pass
                            else:
                                remaining = max(0.0, deadline - loop.time())
                                await asyncio.sleep(remaining * self.acceleration_ratio)
                    if isinstance(component, Record):
                        await original_send(message.derive([component]))
                    else:
                        await original_send(message.derive([*headers, component]))
                        headers.clear()
            finally:
                if acceleration is not None:
                    reply_accelerated = reply_accelerated or acceleration.is_set()
                    active_replies = self._active_replies[session_id]
                    active_replies.remove(acceleration)
                    if not active_replies:
                        del self._active_replies[session_id]

        event._segmented_reply_original_send = original_send
        event.send = send_segmented

    @filter.on_waiting_llm_request()
    async def accelerate_pending_reply(self, event: AstrMessageEvent) -> None:
        """Shorten pending reply delays before this session queues for the LLM lock."""
        for acceleration in self._active_replies.get(event.unified_msg_origin, ()):
            acceleration.set()

    @filter.on_decorating_result(priority=-10000)
    async def prepare_segmented_reply(self, event: AstrMessageEvent) -> None:
        """Split reply text and install ordered delivery for this event.

        Args:
            event: Event containing the complete reply before core decoration.
        """
        if self._native_segmentation_enabled():
            return
        result = event.get_result()
        if (
            result is None
            or not result.chain
            or result.result_content_type
            in {ResultContentType.STREAMING_RESULT, ResultContentType.STREAMING_FINISH}
            or event.get_platform_name() in EXCLUDED_PLATFORMS
            or (self.only_llm_result and not result.is_model_result())
        ):
            return

        new_chain = []
        for component in result.chain:
            if (
                not isinstance(component, Plain)
                or len(component.text) > self.words_count_threshold
            ):
                new_chain.append(component)
                continue

            segments = self._split_text(component.text)
            if not segments:
                new_chain.append(component)
                continue

            for segment in segments:
                if self.content_cleanup_rule:
                    try:
                        segment = re.sub(self.content_cleanup_rule, "", segment)
                    except re.error:
                        logger.error(
                            "Invalid segmented-reply cleanup expression; disabling it: %s",
                            traceback.format_exc(),
                        )
                        self.content_cleanup_rule = ""
                segment = segment.strip()
                if segment:
                    new_chain.append(Plain(segment))
        result.chain = new_chain
        self._install_segmented_sender(event)

    @filter.after_message_sent(priority=-10000)
    async def restore_event_send(self, event: AstrMessageEvent) -> None:
        """Restore the original send method after the response has been delivered.

        Args:
            event: Event whose send method may have been wrapped by this plugin.
        """
        original_send = getattr(event, "_segmented_reply_original_send", None)
        if original_send is not None:
            event.send = original_send
            del event._segmented_reply_original_send
