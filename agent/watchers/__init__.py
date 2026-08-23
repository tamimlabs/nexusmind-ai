"""Event watchers for always-awake agent behavior.

Supported platforms:
    GitHub, GitLab, Slack, Discord, Jira, Reddit,
    Hacker News, Email (IMAP), RSS/Atom, Cron, Custom Webhook
"""
from agent.watchers.base import BaseWatcher
from agent.watchers.cron import CronWatcher
from agent.watchers.discord import DiscordWatcher
from agent.watchers.email_watcher import EmailWatcher
from agent.watchers.github import GitHubWatcher
from agent.watchers.gitlab import GitLabWatcher
from agent.watchers.hackernews import HackerNewsWatcher
from agent.watchers.jira import JiraWatcher
from agent.watchers.reddit import RedditWatcher
from agent.watchers.rss import RSSWatcher
from agent.watchers.slack import SlackWatcher
from agent.watchers.webhook import WebhookWatcher

__all__ = [
    "BaseWatcher",
    "CronWatcher",
    "DiscordWatcher",
    "EmailWatcher",
    "GitHubWatcher",
    "GitLabWatcher",
    "HackerNewsWatcher",
    "JiraWatcher",
    "RedditWatcher",
    "RSSWatcher",
    "SlackWatcher",
    "WebhookWatcher",
]
