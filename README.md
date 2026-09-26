# Discord Bot Project

بوت Discord احترافي متعدد الأنظمة (Verification, Leveling, Staff XP, Moderation, Tickets, ...).

## Setup
1. `python -m venv venv`
2. `venv\Scripts\activate` (Windows) أو `source venv/bin/activate` (Mac/Linux)
3. `pip install -r requirements.txt`
4. انسخ `.env.example` إلى `.env` واملأ القيم
5. `python -m bot.main`
## Phase 7 — Verification

This feature extends the current settings registry, permission checks, central logging and Alembic migration setup. Startup applies migration 0002.

### Discord setup
1. Enable Server Members Intent in the Discord Developer Portal.
2. Give the bot Manage Roles, View Channels, Send Messages and Embed Links. Give it Ban Members if the team will use Ban.
3. Place the bot role above Unverified, Verified, Male and Female. Configure Discord channel overwrites so Unverified members only see the intended verification channels.
4. Create the Verification Team role, a verification voice channel, a panel text channel and a verification log channel.
5. Use /config set-role for role_unverified, role_verified, role_verification_team, role_male and role_female.
6. Use /config set-channel for verification_voice, verification_panel_channel and log_verification.
7. Optionally change verification_dm_enabled, verification_request_cooldown and staff_xp_verify with /config set-value.
8. Run /verification panel as an Administrator with the configured staff permissions.

The queue is fetched from Discord and lists only non-bot members holding Unverified. It paginates at 25. Only Verification Team members can use it.

### Workflow
A joining member receives Unverified. On entering the configured voice channel, the bot creates a waiting record, DMs the team (best effort) and logs the request. Repeat joins are suppressed during cooldown or while the review is in progress.
The reviewer selects a gender and answers the staff-elsewhere question. Yes requires a server name. Verify is blocked until both answers are present. Success removes Unverified, adds Verified and the gender role, saves a record, writes a central log and one XP ledger entry. Reject leaves the member Unverified. Ban requires Ban Members. /verification status shows the latest record to Staff and above.

### Test checklist
- New member receives Unverified.
- Enter the verification voice channel: team DMs and a verification log appear.
- Leave and re-enter quickly: duplicate DM is suppressed.
- Non-team member tries the panel: denied.
- Team member opens panel: only Unverified members appear; pagination works.
- Self-verification is denied.
- Verify before both required answers: blocked.
- Set Female or Male, answer No, then Verify: role transition and record/log succeed.
- Answer Yes and submit a server name: name appears in the saved record and log.
- Reject leaves the member Unverified.
- Ban removes the member and logs the decision.
- Restart: public panel still responds; pending review can be selected from the queue.
- Duplicate XP award for one verification record is rejected.
- Missing Manage Roles, wrong role hierarchy and missing Ban Members report actionable errors.
- Check /verification status and /logs test.

Common setup issues: unset role/channel IDs, bot role below a configured role, disabled Server Members Intent, reviewers blocking DMs, or missing log_verification.

## Phase 8 — Welcome Card

The Welcome Cog posts a Discord Embed when a member joins. It uses the member's live avatar, the server's current banner and icon when available, and per-guild configuration through the existing `/config` commands. Missing banners use the optional fallback image; unavailable custom emojis use Unicode fallbacks. The Verification Cog remains responsible for assigning Unverified.

### Setup
1. Give the bot View Channel, Send Messages and Embed Links permissions in the welcome channel.
2. Set `welcome_channel` using `/config set-channel` and choose the welcome text channel.
3. Ensure `welcome_enabled` is true using `/config set-value` if it was changed.
4. Optional: customize `welcome_message`, `welcome_embed_title`, `welcome_embed_footer`, `welcome_embed_color`, and visual toggles with `/config set-value`.
5. Optional: configure a custom emoji with `/config set-value` using `welcome_emoji_welcome`, `welcome_emoji_member`, `welcome_emoji_server`, `welcome_emoji_rules`, or `welcome_emoji_verification`. Use Discord syntax such as `<:welcome:123456789012345678>` (or animated `<a:name:id>`). The emoji must belong to this server. Reset a value with `/config reset`.
6. Optional: set `welcome_fallback_image_url` to an HTTPS image URL for servers without a banner. Use `/config reset` to remove it.
7. Set the Verification channel with `verification_channel` if you want its mention included in the card. Existing verification roles and panel configuration are unchanged.

Supported text placeholders: `{user}`, `{mention}`, `{username}`, `{display_name}`, `{user_id}`, `{server}`, `{server_id}`, `{member_count}`, `{created_at}`, `{joined_at}`, and `{inviter}` (currently `Unknown`, since this project has no invite attribution system). Emoji placeholders: `{WELCOME_EMOJI_WELCOME}`, `{WELCOME_EMOJI_MEMBER}`, `{WELCOME_EMOJI_SERVER}`, `{WELCOME_EMOJI_RULES}`, and `{WELCOME_EMOJI_VERIFICATION}`. The existing `welcome_message` setting is the embed description and remains supported.

### Phase 8 verification checklist
- [ ] A configured custom Welcome emoji displays; multiple configured emojis display together.
- [ ] Missing, malformed, deleted, or another-server emoji uses a fallback without crashing.
- [ ] A server banner displays; no banner uses the configured fallback image or omits the image.
- [ ] Member custom, default, and animated avatars render; server icon is shown when enabled.
- [ ] Member count, identity, dates, and all supported placeholders are correct.
- [ ] Embed title, fields, footer, color, and timestamp look correct in the configured channel.
- [ ] Verification remains unaffected and new members still receive Unverified.
- [ ] Existing systems remain unaffected.
