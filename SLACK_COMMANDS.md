# Slack bot commands

The worker keeps a Slack Socket Mode connection open so authorized App Home
direct-message commands are handled immediately. Gmail scans remain on their
separate one-minute schedule.

## Commands

- `rm`: for every thread currently carrying `Bluevua_KOL_Active/01_Needs Review`, remove that label plus the `Bluevua_KOL_Active` parent and every configured member child label. Those threads leave Active Track on the next scan. This never deletes email. Only Shanshan and Candice (from `pilot_recipient_slack_id`) may run it.
- `summary`: report Active Track total, Needs Reply total, Needs Review total, task count by owner, unanswered Reach Out total, and the Google Sheet link.
- `task`: list the requesting member's assigned KOLs, Gmail links, and current reply status.

Commands must be the entire message, ignoring surrounding spaces and letter case.

When `Testing developer` is active, only that configured user is authorized and replies go only to that DM. When it is inactive, active Team rotation members are authorized and each response returns to the requesting member's DM.

## Slack app setup

1. In **OAuth & Permissions → Bot Token Scopes**, add `im:history`. Keep the existing `chat:write` and `im:write` scopes.
2. Reinstall the app to the workspace so the bot token receives the new scope.
3. In **App Home**, enable the Messages tab and allow users to send messages from it.
4. Enable the **Home Tab**. The worker publishes command help and the Sheet link with `views.publish`.
5. Under **Socket Mode**, enable Socket Mode.
6. Under **Basic Information → App-Level Tokens**, create a token with `connections:write` and put its `xapp-...` value in `.env` as `SLACK_APP_TOKEN`.
7. Under **Event Subscriptions → Subscribe to bot events**, add `message.im`.
8. Restart the EC2 worker after updating the tokens.

No public Slack webhook or Request URL is needed because Socket Mode carries the events over the persistent outbound connection.

## Daily Digest

The one-minute worker checks `daily_send_time` in the configured `timezone` and
sends no more than once per local calendar day. The digest includes current
totals plus additions and removals since the previous digest snapshot. When
`Testing developer` is active it is the primary recipient. Otherwise,
`pilot_mode` routes the primary copy to `pilot_recipient_slack_id`; production
mode routes it to `production_channel_id`. An active `Testing Channel` always
receives an additional copy.
