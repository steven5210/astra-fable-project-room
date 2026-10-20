# Variant: baseline (the current workflow)

The baseline is the installed Project Room controller at the commit recorded in the run manifest, with no staged instruction.
The engineer receives only the normal packet: the frozen specification bytes, the standard workflow parts the controller already
delivers once, and the routine continuation text. Nothing else is added.

Identity for the run record: `controller_commit` plus `staged_instruction: null`.
