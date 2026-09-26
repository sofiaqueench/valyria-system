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

## Phase 12 — Temporary Voice Rooms

Temporary rooms reuse the existing guild configuration, database, centralized Voice logging, and Phase 10/11 Member Voice XP listener. A member joining the configured generator gets a private room in the configured category, is moved into it, and receives a persistent owner panel in that voice channel's integrated text chat. The panel contains Call Staff, Kick, Rename, User Limit, Lock, Unlock, Transfer Ownership, Ban, Give Access, and Remove Access. Empty rooms are deleted after a configurable grace period; when an owner leaves while others remain, ownership transfers to a room member.

### Configure

1. Create a voice channel for the generator and a category for temporary rooms.
2. Configure them with `/config set-channel key:temp_voice_create_channel channel:#Create-Room` and `/config set-channel key:temp_voice_category channel:<Temporary Rooms>`.
3. Optionally configure alerts with `/config set-channel key:temp_voice_staff_alert channel:#staff-alerts`.
4. Optionally set the panel image using `/config set-value key:temp_voice_banner value:<HTTPS image URL>` and the room name with `/config set-value key:temp_voice_default_name value:🔊・{display_name}'s Chamber`.
5. Review `/config view group:temp_voice`. `temp_voice_default_limit` defaults to 0 (unlimited), `temp_voice_delete_delay` to 5 seconds, and `temp_voice_staff_call_cooldown` to 60 seconds. Set the cooldown to 0 to disable it. Panel visibility and staff override are configurable there too.

The bot needs Manage Channels and Move Members at guild level, plus View Channel, Connect, Send Messages, and Embed Links in the generator/category and generated rooms. It must be able to apply room overwrites (Manage Roles). Ensure configured staff roles are above the bot only when Discord hierarchy requires it. Staff override reuses the existing permission levels; it does not add Staff ranks or promotions.

Room records and room-scoped bans/access are stored in the existing database. Startup recovery restores each existing room and its panel without creating new rooms. Temp Voice uses the existing Member Voice XP rules, so solo-room rewards and Verification/Jail exclusions remain unchanged. Local mocks can check bot-side behavior, but actual permission behavior, panel display, and XP accrual should also be confirmed in a Discord test server.

## Phase 13 — Staff XP and automatic promotion

Staff XP continues to use the existing `staff_xp_history` ledger. Invite attribution and Verification awards now call the shared post-award promotion hook; Member XP, Member Levels, Voice XP, and Temp Voice do not award Staff XP. Staff XP is cumulative and is not reset when a promotion happens.

Configure all 11 promotion slots as an ordered ladder. Thresholds must increase with rank, role IDs must be unique, and High Staff slots must follow Staff slots. Promotions stay inactive until every slot is configured. For example:

```text
/config staff-rank set rank:1 role:@Staff I rank_type:staff required_xp:1000
/config staff-rank set rank:2 role:@Staff II rank_type:staff required_xp:2500
/config staff-rank set rank:3 role:@Staff III rank_type:staff required_xp:5000
...configure ranks 4–6 as staff...
/config staff-rank set rank:7 role:@High Staff I rank_type:high_staff required_xp:25000
...configure ranks 8–11 as high_staff with increasing XP...
```

Thresholds and role names above are examples only; select your own. Use `/config staff-rank view` to inspect the slots, `/config staff-rank remove rank:11` to clear a slot, and `/config view group:staff_xp` to review XP and channel settings. Set the two announcement channels with `/config set-channel key:staff_promotion_channel channel:#staff-promotions` and `/config set-channel key:high_staff_promotion_channel channel:#high-staff-promotions`. Staff can use `/staff-xp` to view their XP, current rank, next threshold, and remaining XP.

At each committed Staff XP award, the bot calculates the highest earned slot, changes only configured Staff Promotion roles, stores the last applied rank, announces once in the matching channel, and writes to the centralized XP log. If one award passes several thresholds, it assigns the highest earned role and sends one announcement. The bot needs Manage Roles and its highest role must be above all 11 promotion roles. A missing announcement channel does not block the role update. Rank configuration is stored in the existing guild settings; the new migration stores per-guild, per-member promotion state. The first award or `/staff-xp` status check can reconcile an earned rank after configuration is completed or a managed rank role is removed.

### Staff Warn XP penalty add-on

Use `/config set-value key:staff_warn_xp_penalty value:500` to set the nonnegative XP penalty (Admin/Owner only), then issue `/staff-warn member:@Member reason:...`. The command checks the configured Staff hierarchy and prevents lower-ranked Staff from warning higher-ranked Staff. Each warning is stored once by interaction ID; the linked `staff_xp_history` entry uses `action=staff_warn` and a negative XP amount. XP never drops below zero. With a complete rank configuration, the bot updates the configured rank role after a threshold reduction, including Staff/High Staff tier changes, and records that rank state without sending a promotion announcement. Configure the optional reduction channel with `/config set-channel key:staff_demotion_channel channel:#staff-demotions`; if unset, the XP log still records the warning and rank change. `/staff-xp` shows the latest XP change and its source.

## Phase 13.5 — Staff Games

The Staff Games panel supports Spy, Criminal, Mafia, Codenames, and Loup Garou. Each game has its own panel channel, public results channel, and configurable host XP. Configure channels using `/config set-channel` with `spy_panel_channel` / `spy_results_channel` (and the corresponding `criminal_*`, `mafia_*`, `codenames_*`, and `loup_garou_*` keys); set host XP with `/config game-xp game:spy xp:100`. Results channels must be visible to @everyone and different from their Staff-only panel channel. Panel channels must deny View Channel to @everyone and grant visibility only to configured Staff roles.

After configuring channels, publish each private panel with `/game-panel publish game:spy` (repeat for each game). Staff start a game by selecting a Staff/High Staff host; at the end they select the host again and enter a member, team, or side as winner. The bot stores the start/end time and duration, atomically stores the game result with one `game_host` transaction in the existing `staff_xp_history`, then runs the existing Staff promotion hook. Only the game result, host, time, duration, and winner appear in the public results channel; XP and controls remain internal. One active session is allowed per game per server. Active games and completed/cancelled records survive restarts. The bot must have Send Messages and Embed Links in panel/results channels; it also needs Manage Roles and the existing role hierarchy for promotion updates.
