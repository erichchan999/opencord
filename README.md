# fx-never-sleeps Discord sandbox

Discord users @mention a bot in one channel. Each request gets its own Discord thread and its own OpenCode session, running on DeepSeek. Up to 16 sessions run in parallel. They all work in one shared clone of the repo. The agent can read and change the `fx-never-sleeps` repo, run shell commands, use the open web, and open PRs. Slash commands choose each thread's model, thinking effort and agent.

Everything runs as Docker containers on Docker Desktop, in the `fx-sandbox` compose project, so you can watch them, their logs and their resource use in the Docker Desktop dashboard. Nothing from Windows is mounted into them.

## Layout

| Container | Holds | Notes |
| --- | --- | --- |
| `discord-bridge` | Discord bot token | Watches one channel, opens threads, forwards text to the agent's API. |
| `opencode-agent` | Scoped GitHub token | `opencode serve` plus the repo. Users fully control this container. |
| `llm-proxy` | DeepSeek API key | Forwards only chat completions and the model list, adds the real key, rate-limits. |

Each container's secrets come from its own file in `secrets/`, so no container gets another one's keys.

| Path | What it is |
| --- | --- |
| `compose.yaml` | The three containers, their networks and resource limits |
| `start.ps1` | Deploy, status (including which commit is deployed), logs and stop |
| `discord-bridge/` | `bridge.py` (messages, threads and the request flow), `commands.py` (slash commands), `events.py` (the agent's event stream), `status.py` (the live status message), `questions.py` (the agent's questions), `state.py` (thread map, settings and open questions), `catalog.py` (the models and agents the agent offers) and `opencode.py` (OpenCode API client). Python 3.13, discord.py and aiohttp. |
| `llm-proxy/server.py` | The proxy that holds the DeepSeek key. Python standard library only. |
| `opencode-agent/` | Agent image, egress firewall, startup scripts, OpenCode config. Node, because OpenCode is installed from npm. |
| `secrets.example/` | One env file per container with placeholders. Real values go in `secrets/`, which git ignores. |

### Isolation

- **Windows:** no Windows folder is mounted into any container. The images are built from this folder, but only the files each Dockerfile copies end up inside.
- **Network:** the agent's firewall is set up as root at startup, before it drops to the `node` user with no capabilities. It blocks every private range, which covers the Windows host, your LAN and the other containers. The only private destination it allows is port 8080 on the internal network shared with the proxy, so a recreated proxy with a new IP stays reachable. The open internet is allowed.
- **Internal networks:** `bridge-net` (bridge ↔ agent) and `llm-net` (agent ↔ proxy) have no route outside.
- **Limits:** CPU, memory and process limits on every container. `no-new-privileges` is set and all capabilities are dropped.

The remaining risk is a container escape or a Linux kernel exploit. Docker Desktop's VM can reach your Windows files (it shares them for bind mounts), and all WSL distros share one VM kernel, so an escape could reach `C:`.

In Docker Desktop's settings, keep "Expose daemon on tcp://localhost:2375 without TLS" turned off.

## Setup

1. **Docker Desktop:** install it with the WSL 2 backend. In Settings → General, turn on "Start Docker Desktop when you sign in", so the bot comes back after a reboot (the containers restart on their own once Docker is up).

2. **Secrets:** copy the examples, then fill in each file.

   ```powershell
   Copy-Item -Recurse secrets.example secrets
   ```

   | File | Set |
   | --- | --- |
   | `secrets/proxy.env` | `DEEPSEEK_API_KEY` |
   | `secrets/agent.env` | `GH_TOKEN` (see step 3). `REPO` if it isn't `fx-never-sleeps`. |
   | `secrets/bridge.env` | `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` (see step 5). Optionally `MAX_CONCURRENT` and the three time limits described under "Using it". |
   | `secrets/shared.env` | `OPENCODE_SERVER_PASSWORD`, any long random string. |

   One way to make the password:

   ```powershell
   $b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); -join ($b | ForEach-Object { $_.ToString('x2') })
   ```

3. **GitHub token:** create a fine-grained personal access token with:
   - Repository access: only `fx-never-sleeps`
   - Contents: read and write
   - Pull requests: read and write
   - No other permissions. Leaving out Workflows means nobody can add Actions workflows.

4. **Protect `main`:** in the repo, add a ruleset on `main` that requires a pull request with your approval and blocks force pushes and deletion.

5. **Discord bot:** in the Developer Portal, turn Message Content Intent on and Public Bot off. Invite the bot with `https://discord.com/oauth2/authorize?client_id=<application id>&scope=bot&permissions=309237746752`, which grants:
   - View Channel
   - Send Messages
   - Read Message History
   - Create Public Threads
   - Send Messages in Threads
   - Add Reactions (for the `!stop` reaction)
   - Attach Files

   The `bot` scope also covers the slash commands. The bridge registers them with your server each time it starts and logs `slash commands ready: ...`.

   To get the channel ID, turn on Developer Mode in Discord, right-click the channel and choose Copy Channel ID.

6. **Start it:**

   ```powershell
   .\start.ps1
   ```

   This builds the images from this folder and starts the containers. The bridge log should end with `watching #<channel>`, and it names any missing permissions.

Other commands:

```powershell
.\start.ps1 -Status   # container status, and which commit each container runs
.\start.ps1 -Logs     # follow logs
.\start.ps1 -Stop     # stop containers (repo clone, sessions and thread map are kept)
```

After editing any file here, run `.\start.ps1` again to redeploy.

### Which version is running

Each deploy stamps every container with an `fx.revision` label: the commit this folder was at, plus `-dirty` if it had uncommitted changes. `.\start.ps1 -Status` shows it next to the folder's current commit, with an `UpToDate` column. In Docker Desktop, open a container and look under its labels.

## Using it

- **New request:** @mention the bot in the channel. It opens a thread with a new OpenCode session working in `/work/repo`.
- **Branches:** the bridge doesn't create branches or worktrees. Ask the agent for one if a change needs it.
- **Follow-ups:** any message in that thread continues the same session. Only threads under `DISCORD_CHANNEL_ID` count: if you move the bot to another channel, its old threads stop responding.
- **Attachments:** text files attached to a message (source code, logs, Markdown, JSON, a `Dockerfile` and so on, up to 100 KB each and 200 KB for all of a message's files) are sent to the agent as their content. Other files, such as images, and bigger ones are passed as a link the agent can try to fetch.
- **Parallel threads:** up to `MAX_CONCURRENT` (16) threads run at once, and further requests wait in a queue. All sessions share one checkout, so two threads editing the same files at the same time can collide.
- **Progress:** while the agent works, one status message in the thread shows the tool call in progress and the agent's todo list. It is edited in place, at most every two seconds, and ends as a single line with the time taken, the number of tool calls and the size of the context. The answer follows as a normal message.
- **Questions:** when the agent asks something, each question appears as a message with a button per option, or a menu when there are many or several can be picked, plus "Type an answer". Anyone in the thread can answer. If exactly one question is open, a plain message in the thread is taken as its answer. The request waits, and its time limit is paused, until every question has an answer.
- **Cancel:** `!stop` in the thread aborts the current request, dismisses its open questions and drops any of that thread's messages still waiting in the queue.
- **Time limits:** a request is stopped, and the thread is told, in three cases. Each can be changed in `secrets/bridge.env`.

  | Setting | Default | Stops a request when |
  | --- | --- | --- |
  | `PROMPT_TIMEOUT_SECONDS` | 1800 (30 min) | it has worked this long, not counting time spent waiting for an answer |
  | `PROMPT_SILENCE_SECONDS` | 900 (15 min) | the agent has shown no activity at all for this long |
  | `QUESTION_TIMEOUT_SECONDS` | 3600 (60 min) | a question has gone unanswered this long |

- **Restarts:** a request that is running when the bridge restarts is picked back up: its status message carries on, and the answer is posted when it is ready, or straight away if it finished meanwhile. Open questions keep their buttons. Messages that were still waiting in a thread's queue are lost, and so is progress the agent reported while the bridge was down. If the agent container restarts, its running requests end there.
- **Idle threads:** a thread uses a slot only while the agent is answering it. Old threads cost a few KB of session history on disk, and any new message in one resumes its session.
- **Replies:** the bot never pings anyone, whatever the agent writes.

### Slash commands

Anyone who can post in the channel can use them. A change is announced in the thread. Lookups (`/status`, `/todo`, `/help`) are shown only to the person who asked.

| Command | In a thread | In the main channel |
| --- | --- | --- |
| `/model` | Choose the thread's model | Choose the model new threads start on |
| `/effort` | Choose how hard the model thinks: `off`, `low`, `high` or `max` | The same, for new threads |
| `/agent` | Switch between OpenCode agents. `plan` reads and proposes without editing files; `build` is the default. | The same, for new threads |
| `/status` | Show the model, effort, agent, whether a request is running, and how full the context is | Show the defaults and how many threads are working |
| `/todo` | Show the agent's whole todo list | |
| `/diff` | Post the file changes this thread's session made, as a `.diff` file with a section per request | |
| `/export` | Post the conversation as a Markdown file: each request, the agent's answers and the tools it used | |
| `/stop` | Same as `!stop` | |
| `/undo` | Take back the last request: it leaves the context and its file changes are undone. Asks first, naming the files. Run it again to go back further. | |
| `/redo` | Bring back what `/undo` took back. Only possible until the next message is sent. | |
| `/new` | Start a fresh session in the same thread. Settings stay, earlier context is dropped. | |
| `/compact` | Replace the conversation so far with a summary, to free up context | |
| `/fork` | Open a new thread that carries on from here, with a copy of the conversation and the thread's settings | |
| `/rename` | Rename the thread and its OpenCode session. Discord allows two renames per thread every ten minutes. | |
| `/shell` | Run a shell command in the checkout and post its output. The agent sees the output in later messages. Stopped after `SHELL_TIMEOUT_SECONDS` (600). | |
| `/command` | Run one of the agent's own commands or skills, with autocomplete, such as `review` or `init`. Followed with a status message like any request. | |
| `/help` | List the commands | List the commands |

Typing `default` as the value puts a setting back on the agent's own config. A change made while a request is running applies from the next message. `/compact`, `/fork`, `/shell` and `/command` wait for the request in progress, like a message would. `/undo` and `/redo` refuse while a request is running.

`/undo` restores the files the request changed to how they were before it. Every thread works in the same checkout, so if another thread changed those files since, its changes to them are lost too. The confirmation says so.

Settings are stored per thread, next to the thread map on the `bridge-data` volume, and are sent to the agent with every message. A thread keeps them when its session is lost or replaced with `/new`. A new thread copies the main channel's defaults when it is opened, so changing the defaults later leaves existing threads alone.

## Models

The models, the default and the effort levels all come from `opencode-agent/opencode.json`. The slash commands read them from the agent, so after you edit that file and redeploy, `/model` and `/effort` offer the new list.

- `model` is what a thread uses until someone runs `/model`. It is `deepseek-flash`; `deepseek-v4-pro` is also configured.
- Each model's `variants` are its effort levels. `low`, `high` and `max` send DeepSeek's `reasoning_effort`, and `off` turns thinking off. With no level chosen, nothing is sent and DeepSeek uses its own default, which is thinking on at `high`.

## Tests

The bridge has unit tests for its settings, slash commands, event handling, status message, questions, attachments and the request flow from prompt to reply. They run inside a Docker build, so nothing needs installing on Windows:

```powershell
docker build --target test --progress plain -t fx-sandbox-bridge-test discord-bridge
```

The build fails if a test fails. The tests use stand-ins for Discord and the agent, so they do not cover the live connection to either. The agent stand-in plays back event streams recorded from a real OpenCode 1.19.5 server, kept in `discord-bridge/tests/fixtures/`.

## Not done yet

- A system prompt for the branch-and-PR workflow
- Tighter access control (a role or a user allowlist)
- Scheduled resets of the agent container and its volumes
