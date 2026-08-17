from __future__ import annotations

import json
import os
from dataclasses import dataclass
from time import monotonic
from typing import Any, Iterable
from urllib import error, request


# Slack 憑證一律由環境變數提供，程式碼內不放任何預設值。
# 本機放 `.env`，CI 放 masked 的 CI/CD variables。
#   SLACK_WEBHOOK_URL  最終結果通知用的 Incoming Webhook
#   SLACK_BOT_TOKEN    進度訊息就地更新用的 Bot Token（xoxb-...）
#   SLACK_CHANNEL_ID   進度訊息要發送的頻道 ID
# 進度回報預設值
DEFAULT_SLACK_PROGRESS_INTERVAL_SECONDS = 10


@dataclass(frozen=True)
class SlackRunLink:
    """執行結果連結資料，供 Slack 訊息使用。"""

    name: str
    url: str


@dataclass(frozen=True)
class SlackTestSummary:
    """最終結果統計，供 Slack 通知使用。"""

    passed: int
    failed: int
    skipped: int

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.skipped


@dataclass(frozen=True)
class SlackProgressState:
    """用於產生單則 Slack 訊息的目前執行快照。"""

    run_name: str
    total: int
    completed: int
    passed: int
    failed: int
    skipped: int
    run_url: str = ""
    current_case: str = ""
    started_at: float = 0.0
    # 執行中途因未預期例外而中斷；此時 completed 以外的案例都沒跑到。
    aborted: bool = False
    abort_reason: str = ""

    @property
    def pending(self) -> int:
        return max(0, self.total - self.completed)

    @property
    def percent(self) -> int:
        if self.total <= 0:
            return 0
        return min(100, int((self.completed / self.total) * 100))

    @property
    def elapsed_seconds(self) -> int:
        if self.started_at <= 0:
            return 0
        return max(0, int(monotonic() - self.started_at))


def get_slack_webhook_url() -> str:
    webhook_url = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise RuntimeError("SLACK_WEBHOOK_URL is not configured")
    return webhook_url


def get_slack_bot_token() -> str:
    return os.getenv("SLACK_BOT_TOKEN", "").strip()


def get_slack_channel_id() -> str:
    return os.getenv("SLACK_CHANNEL_ID", "").strip()


def get_slack_progress_interval_seconds() -> int:
    configured = os.getenv("SLACK_PROGRESS_INTERVAL_SECONDS", "").strip()
    if configured:
        return max(0, int(configured))
    return max(0, int(DEFAULT_SLACK_PROGRESS_INTERVAL_SECONDS))


def send_test_run_finished(
    webhook_url: str,
    run_links: Iterable[SlackRunLink],
    summary: SlackTestSummary,
) -> None:
    payload = {
        "text": build_test_run_finished_message(run_links, summary),
        "blocks": build_test_run_finished_blocks(run_links, summary),
    }
    _post_webhook_message(webhook_url, payload)


class SlackProgressClient:
    """送出第一則 Slack 訊息後，持續更新同一則訊息。"""

    def __init__(
        self,
        bot_token: str | None = None,
        channel_id: str | None = None,
    ) -> None:
        self.bot_token = (bot_token or "").strip()
        self.channel_id = (channel_id or "").strip()
        self.message_ts = ""

    @classmethod
    def from_env(cls) -> "SlackProgressClient":
        bot_token = get_slack_bot_token()
        channel_id = get_slack_channel_id()
        if not bot_token or not channel_id:
            raise RuntimeError(
                "Single-message Slack progress requires SLACK_BOT_TOKEN and SLACK_CHANNEL_ID"
            )
        return cls(bot_token=bot_token, channel_id=channel_id)

    def send_or_update_progress(self, state: SlackProgressState, finished: bool = False) -> None:
        text = build_test_run_progress_message(state, finished=finished)
        blocks = build_test_run_progress_blocks(state, finished=finished)
        self._post_or_update_api_message(text, blocks)

    def _post_or_update_api_message(self, text: str, blocks: list[dict[str, Any]]) -> None:
        payload: dict[str, Any] = {
            "channel": self.channel_id,
            "text": text,
            "blocks": blocks,
        }
        if self.message_ts:
            payload["ts"] = self.message_ts
            response = _post_slack_api("chat.update", self.bot_token, payload)
        else:
            response = _post_slack_api("chat.postMessage", self.bot_token, payload)
            self.message_ts = str(response.get("ts") or "")


def build_test_run_finished_message(
    run_links: Iterable[SlackRunLink],
    summary: SlackTestSummary,
) -> str:
    links = list(run_links)
    if not links:
        run_name_text = "Local execution"
    else:
        run_name_text = "\n".join(format_run_link(link) for link in links)
    return "\n".join(
        [
            ":white_check_mark: Test Run Finished",
            "Run Name:",
            run_name_text,
            "",
            f"- Passed: {summary.passed} :white_check_mark:",
            f"- Failed: {summary.failed} :x:",
            f"- Skipped: {summary.skipped} :black_right_pointing_double_triangle_with_vertical_bar:",
            f"- Total: {summary.total}",
        ]
    )


def build_test_run_finished_blocks(
    run_links: Iterable[SlackRunLink],
    summary: SlackTestSummary,
) -> list[dict[str, Any]]:
    links = list(run_links)
    run_name_text = "\n".join(format_run_link(link) for link in links) if links else "Local execution"
    status_emoji = ":white_check_mark:" if summary.failed == 0 else ":x:"
    return [
        section_block(f"{status_emoji} *Test Run Finished*"),
        section_block(f"*Run Name:*\n{run_name_text}"),
        fields_block(
            [
                f"*Passed*\n:white_check_mark: {summary.passed}",
                f"*Failed*\n:x: {summary.failed}",
                f"*Skipped*\n:black_right_pointing_double_triangle_with_vertical_bar: {summary.skipped}",
                f"*Total*\n{summary.total}",
            ]
        ),
        context_block("Daily automation summary"),
        divider_block(),
    ]


def build_test_run_progress_message(state: SlackProgressState, finished: bool = False) -> str:
    header = resolve_progress_header(state, finished=finished, markdown=False)
    current_case = resolve_progress_current_case(state, finished=finished)
    run_name_text = build_progress_run_name_text(state, finished=finished)
    lines = [
        header,
        f"Run Name: {run_name_text}",
    ]
    if finished and state.aborted:
        lines.append(f"Not Run: {state.pending}")
        if state.abort_reason:
            lines.append(f"Reason: {state.abort_reason}")
    if not finished:
        lines.append(
            f"Progress: {build_progress_bar(state.completed, state.total)} {state.completed}/{state.total} ({state.percent}%)"
        )
    lines.extend(
        [
            f"Passed: {state.passed}",
            f"Failed: {state.failed}",
            f"Skipped: {state.skipped}",
            f"Total: {state.total}",
            f"Current: {current_case}",
            f"Elapsed: {format_duration(state.elapsed_seconds)}",
        ]
    )
    if not finished:
        lines.insert(
            6,
            f"Pending: {state.pending}",
        )
    return "\n".join(lines)


def build_test_run_progress_blocks(
    state: SlackProgressState,
    finished: bool = False,
) -> list[dict[str, Any]]:
    header = resolve_progress_header(state, finished=finished, markdown=True)
    current_case = resolve_progress_current_case(state, finished=finished)
    run_name_text = build_progress_run_name_text(state, finished=finished)
    fields = [
        f"*Passed*\n:white_check_mark: {state.passed}",
        f"*Failed*\n:x: {state.failed}",
        f"*Skipped*\n:black_right_pointing_double_triangle_with_vertical_bar: {state.skipped}",
        f"*Total*\n{state.total}",
    ]
    if not finished:
        fields.insert(3, f"*Pending*\n{state.pending}")
        fields.append(f"*Progress*\n{state.completed}/{state.total} ({state.percent}%)")
    elif state.aborted:
        # 中斷時「未執行」才是最重要的數字，否則 0 failed 會被誤讀成全部通過。
        fields.append(f"*Not Run*\n:warning: {state.pending}")

    blocks = [
        section_block(header),
        section_block(f"*Run Name:*\n{run_name_text}"),
    ]
    if finished and state.aborted and state.abort_reason:
        blocks.append(section_block(f"*Reason:*\n`{state.abort_reason}`"))
    if not finished:
        blocks.append(section_block(f"`{build_progress_bar(state.completed, state.total)}`"))
    blocks.extend(
        [
            fields_block(fields),
            section_block(f"*Current:*\n`{current_case}`"),
            context_block(f"Elapsed: {format_duration(state.elapsed_seconds)}"),
            divider_block(),
        ]
    )
    return blocks


def resolve_progress_header(
    state: SlackProgressState, finished: bool, markdown: bool
) -> str:
    """依照執行狀態決定 Slack 訊息標題。"""

    if not finished:
        text = "Test Run In Progress"
        emoji = ":hourglass_flowing_sand:"
    elif state.aborted:
        text = "Test Run Aborted"
        emoji = ":rotating_light:"
    else:
        text = "Test Run Finished"
        emoji = ":white_check_mark:"
    return f"{emoji} *{text}*" if markdown else f"{emoji} {text}"


def resolve_progress_current_case(state: SlackProgressState, finished: bool) -> str:
    """回傳 Current 欄位要顯示的內容。"""

    if not finished:
        return state.current_case or "Waiting for next test case"
    if state.aborted:
        return f"Aborted at {state.current_case}" if state.current_case else "Aborted"
    return "Completed"


def build_progress_run_name_text(state: SlackProgressState, finished: bool) -> str:
    if state.run_url:
        return format_run_link(SlackRunLink(name=state.run_name, url=state.run_url))

    run_name = state.run_name.strip() or "Local execution"
    if not finished and run_name == "Per-case TEST_RUN_ID":
        return "Per-case TEST_RUN_ID (執行中，完成後會顯示實際 Run 名稱與連結)"
    return run_name


def build_progress_bar(completed: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return "[" + ("-" * width) + "]"
    safe_completed = max(0, min(completed, total))
    filled = int(round((safe_completed / total) * width))
    return "[" + ("#" * filled) + ("-" * max(0, width - filled)) + "]"


def format_duration(seconds: int) -> str:
    minutes, secs = divmod(max(0, seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_run_link(run_link: SlackRunLink) -> str:
    return f"<{run_link.url}|{run_link.name}>"


def section_block(text: str) -> dict[str, Any]:
    return {
        "type": "section",
        "text": mrkdwn_text(text),
    }


def fields_block(fields: list[str]) -> dict[str, Any]:
    return {
        "type": "section",
        "fields": [mrkdwn_text(field) for field in fields],
    }


def context_block(text: str) -> dict[str, Any]:
    return {
        "type": "context",
        "elements": [mrkdwn_text(text)],
    }


def divider_block() -> dict[str, Any]:
    return {"type": "divider"}


def mrkdwn_text(text: str) -> dict[str, str]:
    return {
        "type": "mrkdwn",
        "text": text,
    }


def _post_webhook_message(webhook_url: str, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    _open_response(req, expect_json=False)


def _post_slack_api(endpoint: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        f"https://slack.com/api/{endpoint}",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    response = _open_response(req, expect_json=True)
    if not isinstance(response, dict) or not response.get("ok"):
        raise RuntimeError(f"Slack API {endpoint} failed: {response}")
    return response


def _open_response(req: request.Request, expect_json: bool) -> dict[str, Any] | None:
    try:
        with request.urlopen(req, timeout=10) as response:
            body = response.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Slack HTTP {exc.code}: {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Cannot connect to Slack: {exc}") from exc

    if not expect_json or not body.strip():
        return None
    return json.loads(body)
