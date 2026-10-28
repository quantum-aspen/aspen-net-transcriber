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

To add a recording, paste its Zoom share link into the inbox doc on its own line or bullet.
To file the summary under a series, put the series name on the same line:

- https://illinois.zoom.us/rec/share/…
- https://illinois.zoom.us/rec/share/… Software stack meeting

On its next poll (every 5 minutes) the service moves each line into the queue table at
the bottom of the doc. It creates that table if needed:

| Recording | Series | Status |
|---|---|---|
| https://illinois.zoom.us/rec/share/… | Software stack meeting | queued |

- **Status** goes from `queued` to `working`, then to `done — [summary](…)` or
  `failed: <reason>`. Jobs only start within `RUN_HOURS` (any time if unset; the deployment sets it).
- **Series** is optional. Without it, the agent tries to match an existing series page.
  If none matches, the job fails rather than starting a new series.
- To re-run a meeting, type `retry` into its Status cell.
- Failed jobs are retried automatically with exponential backoff: 15 min, 30 min, then 1 h
  (`RETRY_BASE_SECONDS`, `MAX_ATTEMPTS`=4). A restart or redeploy mid-job doesn't use up an
  attempt. The job resumes from its last finished step.

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

Logs go to stdout. Output from the pipeline steps and the agent is included, tagged
`[<job id>]` (the job's work-dir name). The same output is also kept in the job's
`pipeline.log` and `agent.log` until the job finishes. `RUN_HOURS` uses the container's
clock, so set `TZ` in the deployment, e.g. `TZ=America/Denver`.

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
