"""PC-side executor for the remote_claude bridge behavior.

Connects to the ops-cure bridge over HTTP, registers itself as a machine,
polls for commands, and spawns the local `claude` CLI as a child process
to fulfil run.start / run.input / run.interrupt commands. Filesystem
commands (fs.list / fs.mkdir / session.delete) run in-process without
spawning claude.
"""
