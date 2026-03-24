# Vlog Project Memory

## Core principle: End-to-end alignment

All code and all prompts MUST be aligned. End-to-end means every prompt claim matches actual code behavior, and every code behavior is accurately described in the prompt.

- When changing code, update the prompt if it describes the changed behavior
- When changing prompts, verify the code actually implements what the prompt claims
- Never optimize a single stage in isolation — verify the end-to-end effect
