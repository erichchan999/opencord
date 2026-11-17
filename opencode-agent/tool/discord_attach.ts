// A custom OpenCode tool: the agent calls it to send a file to the Discord
// thread. It only checks the file. The bridge reads the call from the
// session when the request ends and posts the file after the reply
// (discord-bridge/attachments.py), so the file's content never goes
// through the model.
import { tool } from "@opencode-ai/plugin"
import { realpath, stat } from "node:fs/promises"
import path from "node:path"

// The largest file the bridge posts: FILE_LIMIT in discord-bridge/commands.py.
const LIMIT = 8_000_000

export default tool({
  description:
    "Send a file from the working folder to the Discord thread, attached after your reply. " +
    "Call it once for each file, after the file is written. Writing a path in your reply attaches nothing.",
  args: {
    path: tool.schema.string().describe("The file: relative to the working folder, or an absolute path inside it"),
  },
  async execute(args, context) {
    const root = await realpath(context.directory)
    let full: string
    try {
      full = await realpath(path.resolve(context.directory, args.path))
    } catch {
      throw new Error(`There is no file at ${args.path}.`)
    }
    const relative = path.relative(root, full)
    if (relative === "" || relative === ".." || relative.startsWith(".." + path.sep) || path.isAbsolute(relative)) {
      throw new Error(`${args.path} is outside the working folder, ${root}. Copy it in there first.`)
    }
    const info = await stat(full)
    if (!info.isFile()) {
      throw new Error(`${args.path} is not a file.`)
    }
    if (info.size > LIMIT) {
      throw new Error(`${args.path} is ${(info.size / 1_000_000).toFixed(1)} MB. Discord takes files up to 8 MB.`)
    }
    return {
      title: relative,
      output: `${relative} will be attached after your reply.`,
      // What the bridge reads: the checked path, relative to the session's folder.
      metadata: { attach: relative, size: info.size },
    }
  },
})
