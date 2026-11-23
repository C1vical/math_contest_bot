# Privacy Policy

**Math Contest Bot**. Last updated: November 23, 2026

This policy explains what data Math Contest Bot (the "Bot") handles when you use it on
Discord. It covers the official Bot run by the maintainers of
[this repository](https://github.com/C1vical/math_contest_bot). The code is public, so
other people can run their own copies; those copies are their operators' responsibility.

## Summary

- The Bot's database holds math problems only. It has no user accounts, profiles or
  usage history.
- The Bot works through slash commands and buttons. It doesn't receive the messages you
  send in servers or direct messages.
- Your data is never sold, used for advertising, or used to train AI models.

## What the Bot handles

| Data | Why | How long |
|---|---|---|
| Your user ID, basic profile details such as your username, your roles and permissions, the server and channel, and the options you chose, sent by Discord when you use a command or button | To reply, and to check whether you may use the command there | While the command is being handled |
| Your user ID and the time you used a command or button | To limit how often commands can be used | In memory; gone when the Bot restarts |
| A channel's ID and the last 50 problems shown there | So `/random` avoids repeats | In memory; gone when the Bot restarts |
| Your user ID, and the filters you gave `/random`, inside the buttons under a problem | So only you can use your Previous, Next and Another random buttons | Stored by Discord as part of the Bot's message, until that message is deleted |
| Your user ID, the command name and the error details, if a command fails unexpectedly | To find and fix the problem | In the Bot's logs, for only as long as needed to fix problems and keep the Bot running |
| Basic information about the servers the Bot is in, such as their names, channels and roles, sent by Discord automatically | To check permissions and stay connected to Discord | In memory; gone when the Bot restarts |

## Sharing

The maintainers don't sell or share your data with anyone, except:

- **Discord**, which the Bot runs on. See [Discord's Privacy Policy](https://discord.com/privacy).
- **The hosting provider** of the machine the Bot runs on, which may store its logs.
- **When the law requires it.**

Problem images are kept on a separate storage service. The Bot requests them itself, and
those requests contain no information about you.

The Solution and Source buttons open other websites, which have their own privacy
policies.

## Your choices

- **See or delete your data:** [contact us](#contact) to ask what the Bot holds about
  you or to have it deleted. Include your Discord user ID.
- **Delete the Bot's messages:** anyone with Manage Messages can delete them, which also
  removes the user ID in their buttons.
- **Stop using the Bot:** server admins can remove it at any time. Data held in memory
  is cleared when the Bot restarts.

## Security

Access to the Bot and its logs is limited to its maintainers, and the Bot communicates
with Discord over encrypted connections. If we learn that someone may have gained
unauthorized access to user data, we will notify Discord and the affected users.

## Children

You must meet Discord's minimum age for your country to use the Bot. If you believe
the Bot holds data about someone younger, contact us and we will delete it.

## Changes

Updates are posted on this page with a new date. Earlier versions are in the
repository's history.

## Contact

Open an issue at <https://github.com/C1vical/math_contest_bot/issues>. Issues are public,
so please don't include anything private beyond your Discord user ID. If you would rather
not post your ID, open an issue asking for a private way to reach us.
