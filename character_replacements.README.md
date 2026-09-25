# Character Replacements

`character_replacements.json` contains literal text replacements that run after OpenCC conversion.

Add entries as JSON key/value pairs:

```json
{
  "纔": "才",
  "longer phrase": "preferred phrase"
}
```

Use an empty replacement value to suppress a literal transcription artifact:

```json
{
  "（聞き取り不能）": ""
}
```

Longer keys are applied before shorter keys, so phrase-level replacements win over single-character replacements. Regex and context-sensitive rules are intentionally not supported.
