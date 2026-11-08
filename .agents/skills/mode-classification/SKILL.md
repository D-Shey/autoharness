---
name: mode-classification
description: Attribute catalog capability triggers between task prompt and user message in CLI mode raising
---

# Mode classification and word attribution

## When to use

Use when explaining why a task mode was raised from light to full, identifying trigger keywords, or attributing matched capability terms to either the task prompt or the user's conversation prompt.

## Steps

1. Classify the combined task prompt and user message together (`catalog.classify(about)`).
2. For each capability and its matched trigger words, check where each word appears:
   - Check if the word is in the task text (case-insensitive substring or token match).
   - Check if the word is in the user message (case-insensitive substring or token match).
3. If the user message is empty or the word does not appear in the user message, do not attribute it to "the user's message".
4. Group words by their actual source: "the task text" or "the user's message". If a word appears in both, default to "the task text".
5. Format the diagnostic explanation clearly with capability names and source tags.

## How to verify

Run unit tests verifying that keywords in task text are credited to "the task text", keywords in user message are credited to "the user's message", and absent user message never receives attributed words.
