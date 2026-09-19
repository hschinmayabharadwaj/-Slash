"""Slack event handling and bot integration."""

import logging
import re
from typing import Optional

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .config import Config
from .database import Database, TaskStatus
from .security import SecurityError

logger = logging.getLogger(__name__)


class SlackHandler:
    """Handles Slack events and interactions."""

    def __init__(self, config: Config, database: Database):
        """Initialize Slack handler.

        Args:
            config: Application configuration
            database: Database instance
        """
        self.config = config
        self.database = database
        self.app = App(token=config.slack.bot_token)

        # Register event handlers
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Register Slack event handlers."""

        @self.app.event("app_mention")
        def handle_mention(event: dict, say: callable) -> None:
            """Handle bot mentions to create tasks."""
            try:
                self._handle_mention(event, say)
            except Exception as e:
                logger.error(f"Error handling mention: {e}", exc_info=True)
                say(f"❌ Error: {e}", thread_ts=event.get("thread_ts", event["ts"]))

        @self.app.action("approve_plan")
        def handle_plan_approval(ack: callable, body: dict) -> None:
            """Handle plan approval button click."""
            try:
                ack()
                self._handle_approval(body, "plan")
            except Exception as e:
                logger.error(f"Error handling plan approval: {e}", exc_info=True)

        @self.app.action("reject_plan")
        def handle_plan_rejection(ack: callable, body: dict) -> None:
            """Handle plan rejection button click."""
            try:
                ack()
                self._handle_rejection(body, "plan")
            except Exception as e:
                logger.error(f"Error handling plan rejection: {e}", exc_info=True)

        @self.app.action("approve_impl")
        def handle_impl_approval(ack: callable, body: dict) -> None:
            """Handle implementation approval button click."""
            try:
                ack()
                self._handle_approval(body, "impl")
            except Exception as e:
                logger.error(f"Error handling impl approval: {e}", exc_info=True)

        @self.app.action("reject_impl")
        def handle_impl_rejection(ack: callable, body: dict) -> None:
            """Handle implementation rejection button click."""
            try:
                ack()
                self._handle_rejection(body, "impl")
            except Exception as e:
                logger.error(f"Error handling impl rejection: {e}", exc_info=True)

    def _handle_mention(self, event: dict, say: callable) -> None:
        """Handle app mention event.

        Args:
            event: Slack event data
            say: Function to send messages
        """
        user_id = event["user"]
        channel_id = event["channel"]
        thread_ts = event.get("thread_ts", event["ts"])
        text = event["text"]

        # Remove bot mention from text
        text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()

        if not text:
            say(
                "👋 Hi! Mention me with a task description to get started.\n"
                "Example: `@bot can you add error handling to the API endpoints?`",
                thread_ts=thread_ts,
            )
            return

        # Parse repository info from text or use default
        # Format: @bot [owner/repo] task description
        repo_match = re.match(r"^([a-zA-Z0-9\-_]+)/([a-zA-Z0-9\-_]+)\s+(.+)$", text)

        if repo_match:
            repo_owner = repo_match.group(1)
            repo_name = repo_match.group(2)
            task_description = repo_match.group(3)
        elif self.config.github.default_owner:
            # Extract repo name from text if default owner is set
            repo_match = re.match(r"^([a-zA-Z0-9\-_]+)\s+(.+)$", text)
            if repo_match:
                repo_owner = self.config.github.default_owner
                repo_name = repo_match.group(1)
                task_description = repo_match.group(2)
            else:
                say(
                    "❌ Please specify repository: `@bot owner/repo task description`",
                    thread_ts=thread_ts,
                )
                return
        else:
            say(
                "❌ Please specify repository: `@bot owner/repo task description`",
                thread_ts=thread_ts,
            )
            return

        # Check user authorization
        try:
            from .security import SecurityManager

            security = SecurityManager(self.config.security)
            security.check_user(user_id)
        except SecurityError as e:
            say(f"❌ {e}", thread_ts=thread_ts)
            return

        # Create branch name
        branch_name = f"bot/{repo_name}-{thread_ts.replace('.', '-')}"

        # Create task in database
        try:
            task = self.database.create_task(
                slack_user_id=user_id,
                slack_channel_id=channel_id,
                slack_thread_ts=thread_ts,
                repo_owner=repo_owner,
                repo_name=repo_name,
                branch_name=branch_name,
                task_description=task_description,
            )

            say(
                f"✅ Task created! I'll start planning the implementation.\n\n"
                f"**Repository:** `{repo_owner}/{repo_name}`\n"
                f"**Branch:** `{branch_name}`\n"
                f"**Task:** {task_description}\n\n"
                f"_Task ID: {task.id}_",
                thread_ts=thread_ts,
            )

            logger.info(f"Created task {task.id} for {repo_owner}/{repo_name}")

        except Exception as e:
            logger.error(f"Failed to create task: {e}", exc_info=True)
            say(f"❌ Failed to create task: {e}", thread_ts=thread_ts)

    def _handle_approval(self, body: dict, approval_type: str) -> None:
        """Handle approval action.

        Args:
            body: Slack interaction payload
            approval_type: 'plan' or 'impl'
        """
        # Extract task ID from action value
        task_id = int(body["actions"][0]["value"])
        user_id = body["user"]["id"]

        task = self.database.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found for approval")
            return

        # Check if user is the one who created the task
        if task.slack_user_id != user_id:
            self._send_ephemeral_message(
                body["channel"]["id"],
                user_id,
                "⚠️ Only the task creator can approve changes.",
            )
            return

        # Update task status
        if approval_type == "plan":
            self.database.update_task_status(task_id, TaskStatus.PLAN_APPROVED)
            message = "✅ Plan approved! Starting implementation..."
        else:
            self.database.update_task_status(task_id, TaskStatus.IMPL_APPROVED)
            message = "✅ Implementation approved! Creating pull request..."

        # Send message to thread
        self.app.client.chat_postMessage(
            channel=task.slack_channel_id,
            thread_ts=task.slack_thread_ts,
            text=message,
        )

        logger.info(f"Task {task_id} {approval_type} approved by {user_id}")

    def _handle_rejection(self, body: dict, rejection_type: str) -> None:
        """Handle rejection action.

        Args:
            body: Slack interaction payload
            rejection_type: 'plan' or 'impl'
        """
        # Extract task ID from action value
        task_id = int(body["actions"][0]["value"])
        user_id = body["user"]["id"]

        task = self.database.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found for rejection")
            return

        # Check if user is the one who created the task
        if task.slack_user_id != user_id:
            self._send_ephemeral_message(
                body["channel"]["id"],
                user_id,
                "⚠️ Only the task creator can reject changes.",
            )
            return

        # Update task status
        if rejection_type == "plan":
            self.database.update_task_status(task_id, TaskStatus.PLAN_REJECTED)
            message = "❌ Plan rejected. Task cancelled."
        else:
            self.database.update_task_status(task_id, TaskStatus.IMPL_REJECTED)
            message = "❌ Implementation rejected. Task cancelled."

        # Send message to thread
        self.app.client.chat_postMessage(
            channel=task.slack_channel_id,
            thread_ts=task.slack_thread_ts,
            text=message,
        )

        logger.info(f"Task {task_id} {rejection_type} rejected by {user_id}")

    def _send_ephemeral_message(
        self, channel_id: str, user_id: str, text: str
    ) -> None:
        """Send ephemeral message visible only to one user.

        Args:
            channel_id: Channel ID
            user_id: User ID
            text: Message text
        """
        try:
            self.app.client.chat_postEphemeral(
                channel=channel_id, user=user_id, text=text
            )
        except Exception as e:
            logger.error(f"Failed to send ephemeral message: {e}")

    def post_plan(self, task_id: int, plan: str) -> None:
        """Post a plan to Slack with approval buttons.

        Args:
            task_id: Task ID
            plan: Plan text
        """
        task = self.database.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found")
            return

        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"**📋 Implementation Plan**\n\n{plan}"},
            },
            {"type": "divider"},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ Approve"},
                        "style": "primary",
                        "action_id": "approve_plan",
                        "value": str(task_id),
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "❌ Reject"},
                        "style": "danger",
                        "action_id": "reject_plan",
                        "value": str(task_id),
                    },
                ],
            },
        ]

        self.app.client.chat_postMessage(
            channel=task.slack_channel_id,
            thread_ts=task.slack_thread_ts,
            text=f"Plan ready for review",
            blocks=blocks,
        )

    def post_implementation(self, task_id: int, summary: str, diff_preview: str) -> None:
        """Post implementation results with approval buttons.

        Args:
            task_id: Task ID
            summary: Implementation summary
            diff_preview: Preview of changes (truncated diff)
        """
        task = self.database.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found")
            return

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"**✨ Implementation Complete**\n\n{summary}",
                },
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"**Changes:**\n```\n{diff_preview}\n```"},
            },
            {"type": "divider"},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ Approve & Create PR"},
                        "style": "primary",
                        "action_id": "approve_impl",
                        "value": str(task_id),
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "❌ Reject"},
                        "style": "danger",
                        "action_id": "reject_impl",
                        "value": str(task_id),
                    },
                ],
            },
        ]

        self.app.client.chat_postMessage(
            channel=task.slack_channel_id,
            thread_ts=task.slack_thread_ts,
            text=f"Implementation ready for review",
            blocks=blocks,
        )

    def post_pr_created(self, task_id: int, pr_url: str) -> None:
        """Post PR creation notification.

        Args:
            task_id: Task ID
            pr_url: Pull request URL
        """
        task = self.database.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found")
            return

        self.app.client.chat_postMessage(
            channel=task.slack_channel_id,
            thread_ts=task.slack_thread_ts,
            text=f"🎉 Pull request created: {pr_url}",
        )

    def post_error(self, task_id: int, error_message: str) -> None:
        """Post error notification.

        Args:
            task_id: Task ID
            error_message: Error message
        """
        task = self.database.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found")
            return

        self.app.client.chat_postMessage(
            channel=task.slack_channel_id,
            thread_ts=task.slack_thread_ts,
            text=f"❌ Task failed: {error_message}",
        )

    def start(self) -> None:
        """Start the Slack bot using Socket Mode."""
        handler = SocketModeHandler(self.app, self.config.slack.app_token)
        logger.info("Starting Slack bot in Socket Mode...")
        handler.start()
