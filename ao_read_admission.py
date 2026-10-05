"""One-time read-admission delivery instruction for the retained engineer.

The room's Read guard correlates each admitted Read with its own assistant
record; several Read calls issued in one message run before that record exists,
so the host refuses all but the last. This part is delivered once per retained
engineer session like the other one-time workflow parts.
"""

PART = "read_admission_v1"
INSTRUCTION_SHA256 = "6fa2a1310c6d724334b2d32e1fc6a5c91811b8e4962b1ac3057348e146d79e99"
INSTRUCTION = (
    "Read admission (host behaviour, no new permission): the room's Read guard admits a Read only after "
    "correlating it with its own assistant record in the native transcript, and that record is usually not "
    "yet written while several Read calls issued in one message are running. Parallel Read calls in one "
    "message are therefore mostly refused (only the last is admitted), and every refusal costs a full turn. "
    "Issue Read calls one per message, sequentially, and keep each under the 32 KB per-read budget with "
    "offset and limit. Glob, Grep and the DeepSeek tools are not affected."
)
