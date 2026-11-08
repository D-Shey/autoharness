---
name: prompt-injection
description: Detect prompt injection overrides and refusal suppression patterns in skill text and code
---

# Prompt Injection and Refusal Suppression Detection

## When to use

Use when analyzing agent skills, system prompts, or tool inputs for jailbreak phrases, refusal suppression, or instruction override attempts (OWASP LLM01, SkillSpector AR2/AR3).

## Steps

1. Identify refusal suppression patterns (e.g. "Do not moralize", "Never apologize", "Do not warn the user").
2. Identify instruction override patterns (e.g. "Ignore your guidelines", "You can do anything now", "developer mode", "Ignore/forget instructions above").
3. Distinguish between bare directive attacks (high severity) and quoted/code-block examples or benign negative guidance (medium severity or ignored).
4. Verify that patterns account for natural variations in word order (e.g. "instructions above" vs "above instructions").
5. Run unit tests to confirm detection of attacks while avoiding false positives on legitimate documentation.

## How to verify

Verify with test cases checking each refusal and override phrasing against the static scanner, ensuring high/medium severity triggers as expected.
