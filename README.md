# Tel Spot

**English** · [فارسی](README.fa.md)

Your music, in Telegram. One private supergroup with Topics, and a light bot (single process, no ffmpeg, SQLite state) that does three things:

1. **Playlists**: every Spotify playlist becomes a topic; its tracks are uploaded as audio and new tracks are picked up automatically. The playlist cover is posted (and pinned) at the top of its topic.
2. **New releases**: watches the artists you choose on **YouTube** and **SoundCloud** and posts each new song to one topic (`🆕 New Releases`) as soon as *any* platform shows it first. The copy that appears later on another platform is skipped.
3. **Lyrics**: no button under songs (keeps the group clean). Open the bot in private and send a song name or the music file, or reply to a song in the group with `/lyrics` (or the word `lyrics` / `متن` / `تکست`). Lyrics always arrive in your private chat with the bot (press Start there once). Source: [LRCLIB](https://lrclib.net).

4. **Control from Telegram**: everything above can be managed from the group or from a private chat with the bot (see below).

4. **Favorites**: react with ❤️ to any song in the group and the bot copies it into the `❤️ Favorites` topic (created on first use). Remove the heart and the copy is removed. Needs the bot to be a group admin; songs sent before this feature that came from playlists work too, older new-release/search songs do not.

No Spotify login, developer app or Premium is needed. Playlists are read from Spotify's public embed page, so each playlist just has to be **Public**.

## Personal groups (multi-user)

Tel Spot is built so that **your server is the origin and everyone personalizes it**:

- **Without a group**: anybody who starts the bot can search songs / artists / lyrics-by-memory and get lyrics in private chat. Songs are sent only to that chat.
- **With their own group**: tap **➕ Connect my own group** (or add the bot to any supergroup with **Topics** on and make it admin with *Manage topics*). The bot checks the group, tells what is missing (with a *Check again* button), then the person gets their own playlists, followed artists, 🆕 New Releases, 🎧 Picks and ❤️ Favorites topics, fully separate from everybody else. The one who added the bot and the group's admins manage it; members can search and get songs.
- **Your (the owner's) group** is shown to everyone as the **⭐ Recommended group**. Regular users cannot add songs, topics or playlists to it; they only search and receive songs privately. Hearts (❤️) in it count for the owner only.
- **Optional or mandatory**: in `/settings` (owner, private chat) switch *joining the main group* between **optional** (default) and **mandatory** (people who have not joined cannot use the bot at all, and cannot connect groups).
- **Limits** for non-owners (`.env`): `USER_MAX_GROUPS` (2), `USER_MAX_PLAYLISTS` (5 per group), `USER_MAX_ARTISTS` (15 per group), `USER_SONGS_PER_HOUR` (20). Set `MAIN_GROUP_LINK` to your invite link (else the bot tries to create one).
- If the bot is removed from a group it is disconnected and its owner is told; the data is kept.
- In BotFather enable **Allow Groups** for the bot (default on). Use `/groups` in private to switch between your groups.

## Using it from Telegram

Works in a group the bot is connected to and in a private chat with the bot. Send `/menu` for buttons. Management (add/remove playlists, follow artists, 👥 add to group) is for the group's owner and admins; in the owner's main group only the owner manages.

| Command | What it does |
| --- | --- |
| `/menu` | Main menu |
| `/search name` | Search a song or artist (DM: just type the name) |
| `/playlists`, `/addplaylist link` | List / add playlists (DM: paste a Spotify link). Admins only for add/remove |
| `/artists` | Artists you follow for new releases |
| `/users` | Owner only: ids, usernames, block / unblock / revoke |
| `/status` | Health, failed songs, connection check, **Retry failed** (admins) |
| `/favorites` | Link to the Favorites topic and how many songs are in it |
| `/lyrics` | Lyrics (sent to you privately): reply to a song, or `/lyrics Artist - Title`, or in private just send the name / music file |

**Recognise a song from audio**: in private chat send a **voice message**, a video / video note, an audio/MP3 file (also when sent as a file), or a video (10–20 s is best) and the bot tells you which song it is (Shazam's public recognition via `shazamio`, needs `ffmpeg`, installed by the installer). You get the song with the usual buttons (send here / add to group) and a 📝 Lyrics button. Tagged music files still return lyrics. Max 20 MB.

Search takes a song, an artist, or **a line of lyrics you remember** (3+ words; matched through Genius search). Every result has two buttons: tap the **title** to get the song in your private chat with the bot only, or tap **👥** to add it to the `🎧 Picks` topic of the group. Artist pages work the same way. Metadata comes from Deezer's public API. Open an artist for top songs and albums, or open an artist to see top songs and albums (add one song, a page, or a whole album), or tap **Follow** to pick their YouTube / SoundCloud sources; new songs then go to `🆕 New Releases` automatically. Adding playlists in bulk, following and removing are admin-only.

**Alerts**: when something keeps failing (YouTube blocking the server, 5 failed downloads in a row, a Spotify playlist that is no longer Public, a broken artist source) group admins get a message with a suggested fix, and another when it recovers. Admins must press /start in a private chat with the bot to receive them by DM; otherwise they are posted in the group.

**Persian / non-Latin names**: when Deezer has little or nothing, search also lists YouTube results directly (▶️) and offers to follow the typed name.

## Access control and owner

- **Secret question**: set `ACCESS_ANSWER` in the settings (several answers allowed, comma separated; case and spaces are ignored). Anybody using the bot for the first time, in private or in the group, must answer the question (`ACCESS_QUESTION`) in a private chat with the bot. 5 wrong answers lock the user for 15 minutes. Empty `ACCESS_ANSWER` = no question. The answer lives only in your server's `.env`, never in the repo.
- **Users list**: every person who touches the bot is recorded with numeric id, username and name. See them with `sudo telspot users` on the server, or `/users` in the bot (owner only; you can block, unblock or revoke people there).
- **Owner**: put your numeric Telegram id in `OWNER_ID` (get it from @userinfobot), or leave it empty: at start the bot prints a one-time code in the log (`sudo telspot logs`), and sending `/claim CODE` to the bot in private makes you the owner. Only someone who can read the server log can claim. The owner counts as admin everywhere and receives alerts and new-user notices.
- Being verified does not add anyone to the group: membership of the music group is still required.

## One-line install

```bash
curl -fsSL -o /tmp/telspot-install.sh https://raw.githubusercontent.com/amirebi9156/TelSpot/main/install.sh && sudo bash /tmp/telspot-install.sh
```

Afterwards open the management menu any time with `sudo telspot`. It covers install, update, settings, playlists, artists, start/stop/restart, health check, live logs, statistics, retrying failed tracks, yt-dlp update and uninstall. Most actions also work as commands: `telspot status`, `telspot logs`, `telspot playlists`, `telspot artists`, `telspot update`, ...

> **Renamed from Spotigram.** If you have the old version installed, run the one-line install below once: it moves `/opt/spotigram` to `/opt/telspot`, keeps your settings, playlists, artists and history, and renames the service and the command (`sudo telspot`; the old `spotigram` command keeps working). If you switched to a new bot, put its token in *Edit settings* and make it admin of the group.

## Before you install

1. **Telegram**: create a bot with @BotFather. Create a **private** supergroup, turn **Topics** on, add the bot as admin with **Manage Topics** (and **Pin Messages** for pinned covers). You need the group id (starts with `-100`).
2. **Playlists**: make the playlists you want **Public** in the Spotify app, then copy each link (Share -> Copy link). Add them during install or later from the menu.
3. **Artists** (optional): in the menu choose *Manage artists*, type an artist name, and pick the YouTube channels and SoundCloud profile the bot found (or paste links). An `Artist - Topic` YouTube channel is the auto-generated audio channel and is usually the fastest source.
4. Run the server **outside Iran**.

## How it works

- **Playlists**: every cycle the bot reads each playlist's embed page and compares it with the database. New tracks are matched on YouTube by name and duration, downloaded as m4a with `yt-dlp` and sent into the playlist's topic. Telegram `file_id`s are reused when a track is in several playlists.
- **New releases**: YouTube channel RSS feeds and SoundCloud profiles are checked every 3 minutes. The first time an artist is added, existing uploads are only recorded, so nothing old is sent. A new upload is ignored if it looks like a teaser/snippet/live/interview, is shorter than 1 or longer than 12 minutes, or is older than 72 hours. Songs are identified by a normalized "artist + title", so `Artist - Song (Official Video)` and `Song` are the same release.
- **Lyrics**: long lyrics are sent as a `.txt` file.
- Everything is stored in `/opt/telspot` (`.env`, `playlists.txt`, `artists.txt`, `state.db`). The two lists are re-read every cycle, so edits need no restart.

## Limits

- Only **Public** playlists work, and Spotify's embed page exposes the **first 100 tracks** of a playlist. For longer ones, move already-delivered tracks to another playlist (the bot never deletes anything from Telegram) or split the playlist.
- This relies on unofficial page formats, so Spotify, YouTube or SoundCloud changes can break it. `telspot update` and *Update yt-dlp* fix most cases.
- Release detection is as fast as the platforms' feeds (YouTube RSS can lag a few minutes). Spotify is not monitored.
- Lyrics coverage depends on the community database; some songs (especially non-English) may not be found.

## Config (`/opt/telspot/.env`)

| Key | Meaning |
| --- | --- |
| `BOT_TOKEN` | Telegram bot token |
| `CHAT_ID` | Supergroup id |
| `SYNC_INTERVAL` | Seconds between playlist checks (default 600) |
| `RELEASE_INTERVAL` | Seconds between new-release checks (default 180) |
| `RELEASES_TOPIC_NAME` | Name of the new-releases topic |
| `PICKS_TOPIC_NAME` | Name of the topic for songs added from search |
| `ACCESS_ANSWER`, `ACCESS_QUESTION`, `OWNER_ID` | Secret question for new users, and the owner's id (see Access control) |
| `MAIN_GROUP_LINK`, `USER_MAX_GROUPS`, `USER_MAX_PLAYLISTS`, `USER_MAX_ARTISTS`, `USER_SONGS_PER_HOUR` | Recommended-group link and limits for personal groups |
| `FAV_TOPIC_NAME`, `FAV_EMOJIS` | Favorites topic name; reactions that count as a like (default `❤`, e.g. `❤,🔥`) |
| `RELEASE_MAX_AGE_HOURS`, `RELEASE_MIN_SECONDS`, `RELEASE_MAX_SECONDS` | Release filters |
| `YTDLP_COOKIES` | Optional cookies.txt path if YouTube blocks the server |

## Notes

For personal use only. Keep the group private, and respect the terms of the services you connect and copyright law in your country.
