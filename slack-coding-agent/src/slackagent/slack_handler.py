"""Slack integration (Socket Mode: outbound websocket only, no public endpoint).

Mention the bot to start a task::

    @bot owner/repo add input validation to the signup form
    @bot repo add ...            (when github.default_owner is set)

Inside a task thread::

    @bot status     show where the task is
    @bot cancel     stop the task (requester or an approver)

Plans and diffs are posted with Approve / Reject buttons. Only an approver
may press them: the users in ``security.approvers``, or the requester when
that list is empty.
"""

import logging
import re
from typing import Any, Dict, Optional

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from . import guards
from .config import Config
from .database import ACTIVE, Database, Task, TaskStatus
from .github_ops import GitHubOperationError, validate_repo_name

logger = logging.getLogger(__name__)

_MENTION = re.compile(r"<@[A-Z0-9]+>")
_REPO_TASK = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\s+(.+)$", re.DOTALL)
_NAME_TASK = re.compile(r"^([A-Za-z0-9_.-]+)\s+(.+)$", re.DOTALL)
_BLOCK_TEXT_LIMIT = 2900

# action_id -> (status the task must be in, status on approve/reject)
_APPROVALS = {
    "approve_plan": (TaskStatus.AWAITING_PLAN_APPROVAL, TaskStatus.PLAN_APPROVED),
    "reject_plan": (TaskStatus.AWAITING_PLAN_APPROVAL, TaskStatus.PLAN_REJECTED),
    "approve_impl": (TaskStatus.AWAITING_IMPL_APPROVAL, TaskStatus.IMPL_APPROVED),
    "reject_impl": (TaskStatus.AWAITING_IMPL_APPROVAL, TaskStatus.IMPL_REJECTED),
}

_APPROVAL_MESSAGES = {
    TaskStatus.PLAN_APPROVED: "✅ Plan approved by <@{user}>. Starting the implementation...",
    TaskStatus.PLAN_REJECTED: "❌ Plan rejected by <@{user}>. Task closed.",
    TaskStatus.IMPL_APPROVED: "✅ Changes approved by <@{user}>. Opening a pull request...",
    TaskStatus.IMPL_REJECTED: "❌ Changes rejected by <@{user}>. Nothing was pushed.",
}


def _clip(text: str, limit: int = _BLOCK_TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 20] + "\n…(truncated)"


class SlackHandler:
    """Handles Slack events and posts task updates."""

    def __init__(self, config: Config, database: Database, app: Optional[App] = None):
        self.config = config
        self.database = database
        self.app = app or App(
            token=config.slack.bot_token,
            signing_secret=config.slack.signing_secret or None,
        )
        self._register_handlers()

    # ------------------------------------------------------------------ events

    def _register_handlers(self) -> None:
        @self.app.event("app_mention")
        def handle_mention(event: dict, say) -> None:
            try:
                self._handle_mention(event, say)
            except Exception:
                logger.exception("Error handling mention")
                say("❌ Something went wrong on my side. Please try again.",
                    thread_ts=event.get("thread_ts", event.get("ts")))

        for action_id in _APPROVALS:
            def handler(ack, body, _action_id=action_id) -> None:
                ack()
                try:
                    self._handle_decision(body, _action_id)
                except Exception:
                    logger.exception("Error handling %s", _action_id)

            self.app.action(action_id)(handler)

    def _handle_mention(self, event: dict, say) -> None:
        user_id = event["user"]
        channel_id = event["channel"]
        thread_ts = event.get("thread_ts", event["ts"])
        text = _MENTION.sub("", event.get("text", "")).strip()

        def reply(message: str) -> None:
            say(message, thread_ts=thread_ts)

        if not self._user_allowed(user_id):
            reply("⛔ You're not on the list of people who can use this bot.")
            self.database.record_event(None, "user_denied", user_id, {"channel": channel_id})
            return

        command = text.lower()
        if command in ("cancel", "stop", "status") and event.get("thread_ts"):
            task = self.database.get_task_by_thread(channel_id, thread_ts)
            if not task:
                reply("There's no task in this thread.")
            elif command == "status":
                reply(self._status_text(task))
            else:
                self._cancel(task, user_id, reply)
            return

        if not text:
            reply("👋 Mention me with a repository and a request, e.g. "
                  "`@bot owner/repo add input validation to the signup form`.")
            return

        existing = self.database.get_task_by_thread(channel_id, thread_ts)
        if existing and existing.status in ACTIVE:
            reply(f"I'm already working on task #{existing.id} in this thread. "
                  "Say `cancel` to stop it first.")
            return

        parsed = self._parse_request(text)
        if parsed is None:
            reply("❌ Please name the repository: `@bot owner/repo what you want changed`")
            return
        repo_owner, repo_name, description = parsed
        full_name = f"{repo_owner}/{repo_name}"

        try:
            validate_repo_name(repo_owner, repo_name)
        except GitHubOperationError:
            reply("❌ That doesn't look like a valid `owner/repo` name.")
            return
        if not self.config.repo_allowed(full_name):
            reply(f"⛔ I'm not allowed to work on `{guards.escape_slack(full_name)}`.")
            self.database.record_event(None, "repo_denied", user_id, {"repo": full_name})
            return
        if len(description) > self.config.security.max_request_chars:
            reply(f"❌ That request is too long (limit {self.config.security.max_request_chars} "
                  "characters). Please make it shorter and more specific.")
            return
        quota = self.config.security.max_active_tasks_per_user
        if quota and self.database.count_active_tasks(user_id) >= quota:
            reply(f"⏳ You already have {quota} task(s) in progress. "
                  "Finish or cancel one before starting another.")
            return

        task = self.database.create_task(
            slack_user_id=user_id,
            slack_channel_id=channel_id,
            slack_thread_ts=thread_ts,
            repo_owner=repo_owner,
            repo_name=repo_name,
            branch_name="",
            task_description=description,
        )
        self.database.record_event(task.id, "task_created", user_id, {"repo": full_name})
        reply(
            f"✅ Got it — task #{task.id} on `{guards.escape_slack(full_name)}`.\n"
            f"> {guards.escape_slack(guards.tail(description, 500))}\n"
            "I'll post a plan here for approval before changing anything."
        )
        logger.info("Created task %s for %s", task.id, full_name)

    def _parse_request(self, text: str):
        match = _REPO_TASK.match(text)
        if match:
            return match.group(1), match.group(2), match.group(3).strip()
        if self.config.github.default_owner:
            match = _NAME_TASK.match(text)
            if match:
                return self.config.github.default_owner, match.group(1), match.group(2).strip()
        return None

    def _handle_decision(self, body: Dict[str, Any], action_id: str) -> None:
        """Approve/reject button: approver check + atomic state transition."""
        user_id = body["user"]["id"]
        channel_id = body["channel"]["id"]
        try:
            task_id = int(body["actions"][0]["value"])
        except (KeyError, IndexError, ValueError):
            return
        task = self.database.get_task(task_id)
        if not task:
            return

        if not self._can_approve(task, user_id):
            self._ephemeral(channel_id, user_id, "⚠️ You're not allowed to approve or reject this task.")
            self.database.record_event(task_id, "approval_denied", user_id, {"action": action_id})
            return

        expected, new = _APPROVALS[action_id]
        if not self.database.transition_status(task_id, expected, new):
            self._ephemeral(channel_id, user_id,
                            f"This button is out of date (task #{task_id} is `{task.status.value}`).")
            return

        self.database.record_event(task_id, new.value, user_id, {"diff_sha256": task.diff_sha256})
        message = _APPROVAL_MESSAGES[new].format(user=user_id)
        # Replace the buttons so the decision can't be clicked twice.
        message_ts = (body.get("message") or {}).get("ts")
        if message_ts:
            try:
                original = [b for b in body["message"].get("blocks", []) if b.get("type") != "actions"]
                self.app.client.chat_update(
                    channel=channel_id, ts=message_ts, text=message,
                    blocks=original + [{"type": "context", "elements": [{"type": "mrkdwn", "text": message}]}],
                )
            except Exception:
                logger.warning("could not update approval message", exc_info=True)
        self._post(task, message)
        logger.info("Task %s -> %s by %s", task_id, new.value, user_id)

    def _cancel(self, task: Task, user_id: str, reply) -> None:
        if not (task.slack_user_id == user_id or self._can_approve(task, user_id)):
            reply("⚠️ Only the requester or an approver can cancel this task.")
            return
        if task.status not in ACTIVE:
            reply(f"Task #{task.id} is already finished (`{task.status.value}`).")
            return
        if self.database.transition_status(task.id, task.status, TaskStatus.CANCELLED):
            self.database.record_event(task.id, "cancelled", user_id, {})
            reply(f"🛑 Task #{task.id} cancelled. Nothing further will be pushed.")
        else:
            reply("The task changed state just now — try again.")

    def _status_text(self, task: Task) -> str:
        text = f"Task #{task.id} on `{task.repo_owner}/{task.repo_name}` is `{task.status.value}`."
        if task.pr_url:
            text += f"\nPull request: {task.pr_url}"
        if task.error_message:
            text += f"\n{task.error_message}"
        return text

    # ------------------------------------------------------------------ policy

    def _user_allowed(self, user_id: str) -> bool:
        allowed = self.config.security.allowed_users
        return not allowed or user_id in allowed

    def _can_approve(self, task: Task, user_id: str) -> bool:
        approvers = self.config.security.approvers
        if approvers:
            return user_id in approvers
        return user_id == task.slack_user_id

    # ------------------------------------------------------------------ notifier API (used by Worker)

    def say(self, task: Task, text: str) -> None:
        self._post(task, text)

    def post_plan(self, task_id: int, plan: Dict[str, Any]) -> None:
        task = self.database.get_task(task_id)
        if not task:
            return
        esc = guards.escape_slack
        files = "\n".join(f"• `{esc(f)}`" for f in plan.get("files", [])[:10]) or "• (none listed)"
        body = (
            f"*📋 Plan for task #{task_id}*\n{esc(plan.get('summary', ''))}\n\n"
            f"*Files:*\n{files}\n\n"
            f"*Risk:* {esc(plan.get('risk', 'medium'))} — {esc(plan.get('risk_reason', ''))}\n"
            f"*How to verify:* {esc(plan.get('how_to_verify', ''))}"
        )
        self._post_with_buttons(task, _clip(body), "approve_plan", "reject_plan", "✅ Approve plan")

    def post_implementation(
        self, task_id: int, summary: str, diff_preview: str, checks_report: str = ""
    ) -> None:
        task = self.database.get_task(task_id)
        if not task:
            return
        esc = guards.escape_slack
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": _clip(
                f"*✨ Changes ready for review (task #{task_id})*\n{esc(summary)}")}},
            {"type": "section", "text": {"type": "mrkdwn", "text": _clip(
                f"*Diff:*\n```{esc(diff_preview)}```")}},
        ]
        if checks_report:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _clip(
                f"*Checks:*\n```{esc(guards.tail(checks_report, 1500))}```")}})
        blocks.append(self._buttons(task_id, "approve_impl", "reject_impl", "✅ Approve & open PR"))
        self.app.client.chat_postMessage(
            channel=task.slack_channel_id, thread_ts=task.slack_thread_ts,
            text=f"Changes for task #{task_id} are ready for review", blocks=blocks,
        )

    def post_pr_created(self, task_id: int, pr_url: str) -> None:
        task = self.database.get_task(task_id)
        if task:
            self._post(task, f"🎉 Pull request opened: {pr_url}\nA human still has to review and merge it.")

    def post_error(self, task_id: int, error_message: str) -> None:
        task = self.database.get_task(task_id)
        if task:
            self._post(task, f"❌ Task failed: {guards.escape_slack(error_message)}")

    # ------------------------------------------------------------------ helpers

    def _buttons(self, task_id: int, approve_id: str, reject_id: str, approve_label: str) -> dict:
        return {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": approve_label},
                 "style": "primary", "action_id": approve_id, "value": str(task_id)},
                {"type": "button", "text": {"type": "plain_text", "text": "❌ Reject"},
                 "style": "danger", "action_id": reject_id, "value": str(task_id)},
            ],
        }

    def _post_with_buttons(self, task: Task, text: str, approve_id: str, reject_id: str, label: str) -> None:
        self.app.client.chat_postMessage(
            channel=task.slack_channel_id, thread_ts=task.slack_thread_ts,
            text=f"Task #{task.id} needs your approval",
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}},
                    self._buttons(task.id, approve_id, reject_id, label)],
        )

    def _post(self, task: Task, text: str) -> None:
        try:
            self.app.client.chat_postMessage(
                channel=task.slack_channel_id, thread_ts=task.slack_thread_ts, text=_clip(text, 3900)
            )
        except Exception:
            logger.exception("Failed to post to Slack")

    def _ephemeral(self, channel_id: str, user_id: str, text: str) -> None:
        try:
            self.app.client.chat_postEphemeral(channel=channel_id, user=user_id, text=text)
        except Exception:
            logger.warning("Failed to send ephemeral message", exc_info=True)

    def start(self) -> None:
        """Connect to Slack over Socket Mode (blocks)."""
        logger.info("Starting Slack bot in Socket Mode...")
        SocketModeHandler(self.app, self.config.slack.app_token).start()
