# aspen-net-transcriber

Turns ASPEN-NET Zoom recordings into wiki meeting summaries, unattended.

Someone pastes a Zoom share link into the **Recordings inbox** doc on the
[wiki](https://wiki.quantum-aspen.net). This service, running on ncsa-01:

1. downloads the recording, transcribes it on the CPU with faster-whisper, and pulls out the
   in-meeting chat, `ai:` chat commands, and screenshots of the shared screen;
2. has a Hermes agent write the summary per the
   [`local-meeting-transcript`](https://github.com/quantum-aspen/aspen-net-skills) skill and
   publish it in the Meetings collection;
3. links the summary from the inbox row.

Step 1 involves no model. Step 2 sends the transcript, chat and selected frames to NCSA
Lumen (zero data retention), and nothing else.

## The inbox

Any table in the inbox doc with a **Status** column works:

| Recording | Series | Status |
|---|---|---|
| https://illinois.zoom.us/rec/share/… | Software stack meeting | |

- Leave **Status** empty. The service sets it to `queued`, `working`, then
  `done — [summary](…)` or `failed: <reason>`.
- **Series** is optional. Give it to file the summary under that series page. If it's left
  empty and the agent can't match one, the job fails rather than starting a new series.
- To re-run a meeting, type `retry` into its Status.
- Failed jobs are retried once automatically (`MAX_ATTEMPTS`).

During the meeting, attendees can type chat messages starting with `ai:` (`ai: screenshot`,
`ai: note …`, `ai: off record` … `ai: on record`). See the skill's
`references/chat-commands.md`.

## Configuration

All configuration is through env vars; see [`.env.example`](.env.example). Secrets are
`OUTLINE_API_KEY` (the Transcription Bot user's key) and `LUMEN_API_KEY`.

State lives in the `/opt/data` volume:

| Path | What |
|---|---|
| `transcriber/state.db` | job state; the inbox Status column only mirrors it |
| `transcriber/meetings/` | transcript folders (`transcript.md`, `.srt`, chat, kept frames) |
| `transcriber/work/<id>/` | in-progress jobs; media is deleted when a job finishes |
| `transcriber/hf/` | the whisper model (~1.6 GB, downloaded on first run; safe to delete) |
| everything else | Hermes' own home (config, sessions, logs) |

## Development

```sh
uv sync
uv run pytest && uv run ruff check && uv run ruff format --check
```

The image is built on the official `nousresearch/hermes-agent` image. Its s6 entrypoint
fixes volume ownership, drops to the `hermes` user, and reaps orphaned processes, so keep
it. The skills are baked in read-only from `quantum-aspen/aspen-net-skills` at
`SKILLS_REF`.

```sh
cp .env.example .env              # fill in
docker compose build              # uses ../aspen-net-skills (see compose.yaml)
docker compose run --rm transcriber transcriber run --no-agent '<zoom link>'
docker compose run --rm transcriber transcriber run '<zoom link>'   # DRY_RUN=1 to keep it off the wiki
docker compose up                 # the poller
```

`run --no-agent` stops after transcription and frames, so it needs no keys. Look in the
`data` volume under `transcriber/work/`.

## Releases

`.github/workflows/image.yml` publishes `ghcr.io/quantum-aspen/aspen-net-transcriber`:
`sha-<short>` on every build, `latest` from `main`, and `X.Y.Z` from `vX.Y.Z` tags. It
also rebuilds when aspen-net-skills changes (`repository_dispatch`) and weekly. The
deployments repo pins `tag@sha256`, and Renovate opens the bumps.
