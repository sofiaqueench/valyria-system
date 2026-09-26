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
7. Set the Verification voice channel with `verification_voice` if you want its mention included in the card. Existing verification roles and panel configuration are unchanged.

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

## Phase 9 — Invite Tracking + Staff XP

Invite attribution is integrated into the existing `LogEventsCog.on_member_join` flow; no additional member-join listener is added. The bot builds a per-guild invite cache on startup/reconnect and refreshes it when invites are created or deleted. Because Discord does not provide a definitive invite-used gateway event, the bot compares invite use counts when a member joins. It attributes a join only when exactly one cached invite increases by exactly one; ambiguous or unavailable results are stored without guessing.

### Setup and configuration
1. Enable Server Members Intent for the bot in the Discord Developer Portal. The bot also enables Discord.py's invite intent in code.
2. Give the bot Manage Server (needed to read guild invites), plus its normal role/channel access.
3. Set the invite log channel with `/config set-channel` → `log_invite`.
4. Set invite Staff XP with `/config set-value` → `staff_xp_invite`. Default: `1` XP; `0` disables awards.
5. Staff eligibility uses the existing Permissions system: members at Staff level or above qualify. The inviter receives XP; the joining member is recorded as the target.
6. Staff can run `/invites` for their own statistics or `/invites member:@Member` to inspect another member. The command reports unique attributed joins, invite XP, and the most-used invite codes.

The first observed join for a member in a guild is stored once. Rejoining does not replace that original attribution or grant XP again, including when the first join could not be attributed. Invite attribution and its XP ledger row are committed together. XP history continues in the existing `staff_xp_history` table (`action=invite`); no second XP system is introduced. This project currently stores Staff XP ledger entries but does not implement a level or promotion calculation.

### Phase 9 verification checklist
- [ ] After startup, use a staff-created invite and verify the correct inviter, invite code, and usage transition are logged.
- [ ] With `staff_xp_invite=5`, each first-time member through an eligible staff invite adds exactly 5 XP to that inviter.
- [ ] A non-staff inviter is attributed but receives no Staff XP.
- [ ] Ambiguous, expired/deleted, cold-cache, or unavailable invite data is not guessed and grants no XP.
- [ ] Bot joins, self-invites, unknown creators, and repeated member joins grant no invite XP.
- [ ] Two guilds keep separate invite caches and database statistics.
- [ ] Restart the bot and confirm the invite cache is rebuilt.
- [ ] `/invites` is limited to Staff and above; invite logs go through the configured `log_invite` channel.
- [ ] Verification, Welcome, and other existing member-join behavior still run.

### Local test
From the project directory, activate the existing environment and start the bot (startup automatically runs Alembic migrations):

```powershell
.\venv\Scripts\activate
python -m bot.main
```

Then create a test invite from a configured staff account, join with a separate test account, and run `/invites`. Discord must allow the bot to read invites; otherwise joins are recorded as unavailable and receive no invite XP.

## Phase 10 — Member XP and Leveling

Normal Member XP is stored in the separate `member_xp` table. It is guild-scoped and is never read from or written to `staff_xp_history`. Levels are derived as `total_xp // 100`; XP is not capped at Level 200. Message and Voice XP both call the same Member XP service.

### Configuration and commands
1. Message rewards reuse `message_xp_min` / `message_xp_max` (defaults 15–25 XP, randomly selected) and `message_xp_cooldown` (default 60 seconds per member per guild). These settings are in `/config view` → XP.
2. Voice rewards reuse `voice_xp_amount` (default 10 XP; `0` disables Voice XP) and `voice_xp_interval` (default 60 seconds). Any eligible normal, Stage, or temporary voice channel counts, including a channel with only one member. Jail Voice, Verification Voice, the server AFK channel, and deafened members (when `voice_xp_ignore_deafened` is enabled) do not earn Voice XP.
3. Create your Discord roles, then configure up to eight milestones with `/config level-role level:10 role:@Level10` (repeat for 30, 50, 70, 90, 120, 150, 200 or your chosen levels). Omit the role parameter to remove that level mapping. The bot creates no roles.
4. Set the announcement room with `/config set-channel key:level_up_channel channel:#level-up`. If unset, text XP level-ups use the message's channel; Voice level-ups need the configured channel.
5. Optional appearance settings: `/config set-value key:level_up_banner value:<HTTPS image URL>` and `/config set-value key:level_up_emoji value:<:emoji:ID>`. Reset either with `/config reset`.
6. Members use `/level` or `/level user:@Member`; `/leaderboard` shows the top 10. Administrators can use `/member-xp add`, `/member-xp remove`, and `/member-xp set`; manual changes follow the same level and role processing.

At a configured milestone the member receives the highest applicable configured Level Role, and only other roles managed by this Level Role mapping are removed. XP jumps across several milestones result in one role update and one level-up announcement. Beyond Level 200, the Level 200 role remains the highest mapped role. The bot needs Manage Roles and a role above the configured Level Roles. Missing/deleted roles and hierarchy failures are logged; unrelated member roles are never removed.

Voice sessions rebuild from currently connected members after startup/reconnect. Time while the bot is offline is not credited. Tracking uses elapsed eligible time and a periodic task; alone in voice is allowed. The existing `voice_xp_min_members` placeholder was removed because it conflicts with solo voice XP.

### Phase 10 verification checklist
- [ ] A valid user message earns a configured random amount in range; bot, webhook, system, and empty messages do not.
- [ ] Message cooldown persists through restart and is isolated per member and guild.
- [ ] Voice XP accrues after eligible time, stops on leave, and combines with message XP in one total.
- [ ] Alone in normal/temporary voice still earns XP; Jail, Verification, AFK, and configured deafened exclusions do not.
- [ ] 99→100 XP becomes Level 1; 3250 XP displays Level 32 with 50 XP toward Level 33.
- [ ] Configured Level Roles replace only other configured Level Roles; multiple crossed milestones resolve to the highest reached one.
- [ ] Level 200 role remains at Level 201+; deleted roles, missing permissions, invalid banner, and invalid emoji do not crash XP awards.
- [ ] `/level`, `/leaderboard`, `/member-xp`, and `/config level-role` permissions and results are correct.
- [ ] Member XP and Staff XP remain independent; invite XP does not affect Member XP.
- [ ] Restart retains Member XP and starts fresh Voice tracking without crediting offline time.

## Phase 11 — Voice XP expansion and Permanent Voice

Phase 11 extends the Phase 10 Member XP tracker; it adds no XP table, voice-level system, or voice XP event listener. Eligible Voice and Stage channels are discovered from member voice state, so dynamically created Temp Voice channels work even when a member is alone. Configured Jail Voice, Verification Voice, AFK and (optionally) deafened states remain excluded. Completed intervals and leftover eligible seconds are preserved when a member leaves or moves into an excluded channel. A bot restart starts active sessions from the reconnect time and never awards downtime.

### Permanent Voice setup
1. Install the updated dependencies with `pip install -r requirements.txt`. The `discord.py[voice]` extra installs discord.py's voice support dependencies.
2. Give the bot View Channel and Connect in the chosen channel. Speak is not needed: the bot connects self-deafened.
3. Run `/config permanent-voice channel:#Valyria-Radio` to set the guild's channel. Run `/config view group:voice` to inspect it. Run `/config permanent-voice` or `/config reset key:permanent_voice_channel` to clear it.
4. Start/restart the bot. It connects on Ready; a centralized maintenance loop handles disconnections with exponential backoff up to 15 minutes. Invalid channel/permission settings are logged once until changed and rechecked periodically.

Permanent Voice is per guild and reuses the project's settings registry and central Voice log. It does not require Speak permission. Connection, restart, and actual Discord voice-gateway behavior must be checked in a live server; local mocked tests cannot establish Discord connectivity.
