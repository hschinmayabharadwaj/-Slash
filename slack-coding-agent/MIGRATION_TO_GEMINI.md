# Migration from Anthropic to Google Gemini

This document summarizes the changes made to support Google Gemini API instead of Anthropic's Claude.

## Files Modified

### 1. **src/slackagent/config.py**
- Renamed `AnthropicConfig` → `GeminiConfig`
- Updated `Config` dataclass to use `gemini: GeminiConfig`
- Changed validation to check for Gemini API key format
- Updated default model from `claude-sonnet-4` → `gemini-1.5-pro`

### 2. **src/slackagent/agent_runner.py** (Complete rewrite)
- Changed import from `anthropic` → `google.generativeai`
- Replaced `Anthropic` client with `genai.GenerativeModel`
- Updated `run_planning_phase()`:
  - Single prompt instead of system+user message split
  - Uses `generate_content()` with `GenerationConfig`
- Updated `run_implementation_phase()`:
  - Combined prompt format
  - Uses `response.text` instead of extracting from content blocks
- Added temperature control (0.7) for creative code generation

### 3. **src/slackagent/worker.py**
- Changed `config.anthropic.api_key` → `config.gemini.api_key`

### 4. **src/slackagent/cli.py**
- Changed `config.anthropic.api_key` → `config.gemini.api_key`

### 5. **src/slackagent/security.py**
- Added Google API Key pattern to secret scanner (prioritized to top)
- Pattern: `AIza[0-9A-Za-z\-_]{35}` → "Google API Key (Gemini)"

### 6. **pyproject.toml**
- Replaced `anthropic>=0.18.0` → `google-generativeai>=0.3.0`

### 7. **config.yaml.example**
- Changed section from `anthropic:` → `gemini:`
- Updated API key placeholder
- Updated model default to `gemini-1.5-pro`
- Updated comments and descriptions

### 8. **README.md**
- Updated description to mention "Google Gemini" instead of "Claude"
- Changed all references to Anthropic/Claude → Gemini
- Updated API key environment variable examples
- Updated configuration examples
- Added "Google Gemini AI" to features list

### 9. **tests/test_config.py**
- Updated all test cases to use `gemini` config
- Changed API key format in tests (sk-ant-* → AIzaSy*)
- Updated default model assertion to `gemini-1.5-pro`

### 10. **tests/test_security.py**
- Updated test to check for both Gemini and Anthropic API keys
- Added Gemini API key pattern test

## New Files Created

### 11. **GEMINI_SETUP.md**
Complete guide for obtaining and configuring Gemini API key:
- Step-by-step Google AI Studio setup
- Available models and their use cases
- Pricing information
- Rate limits
- Troubleshooting guide
- Security best practices

## API Differences: Anthropic vs Gemini

### Request Format

**Anthropic (Before)**:
```python
response = client.messages.create(
    model="claude-sonnet-4",
    max_tokens=20000,
    system=system_prompt,
    messages=[{"role": "user", "content": user_prompt}]
)
text = "".join(block.text for block in response.content if hasattr(block, "text"))
```

**Gemini (After)**:
```python
response = model.generate_content(
    combined_prompt,
    generation_config=genai.types.GenerationConfig(
        max_output_tokens=20000,
        temperature=0.7
    )
)
text = response.text
```

### Key Differences

1. **Prompt Structure**:
   - Anthropic: Separate system and user messages
   - Gemini: Combined single prompt

2. **Response Format**:
   - Anthropic: Content blocks that need iteration
   - Gemini: Direct `.text` attribute

3. **Configuration**:
   - Anthropic: Parameters in create() call
   - Gemini: GenerationConfig object

4. **Authentication**:
   - Anthropic: Client instantiation with API key
   - Gemini: Global configure() + model instance

## Environment Variables

### Before
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

### After
```bash
export GEMINI_API_KEY="AIza..."
```

## Configuration File

### Before
```yaml
anthropic:
  api_key: ${ANTHROPIC_API_KEY}

agent:
  model: "claude-sonnet-4"
```

### After
```yaml
gemini:
  api_key: ${GEMINI_API_KEY}

agent:
  model: "gemini-1.5-pro"
```

## Model Options

### Gemini Models Available

| Model | Best For | Token Limit | Speed |
|-------|----------|-------------|-------|
| gemini-1.5-pro | Complex coding tasks, large codebases | 1M | Moderate |
| gemini-1.5-flash | Simple tasks, fast iterations | 1M | Fast |
| gemini-1.0-pro | General purpose (older) | 30K | Moderate |

### Recommended Settings

**For Planning Phase**:
```yaml
agent:
  model: "gemini-1.5-pro"  # Better reasoning
  planning_budget: 20000
```

**For Speed**:
```yaml
agent:
  model: "gemini-1.5-flash"  # 10x cheaper
  planning_budget: 15000
  implementation_budget: 60000
```

## Pricing Comparison

### Anthropic Claude Sonnet 4
- Input: $3 per 1M tokens
- Output: $15 per 1M tokens

### Gemini 1.5 Pro
- Input: $3.50 per 1M tokens
- Output: $10.50 per 1M tokens
- **Free tier: 50 requests/day**

### Gemini 1.5 Flash
- Input: $0.35 per 1M tokens (90% cheaper!)
- Output: $1.05 per 1M tokens (93% cheaper!)
- **Free tier: 1,500 requests/day**

## Migration Checklist

If migrating an existing installation:

- [ ] Install new dependency: `pip install google-generativeai`
- [ ] Uninstall old dependency: `pip uninstall anthropic`
- [ ] Get Gemini API key from Google AI Studio
- [ ] Update `config.yaml` (rename section, change API key)
- [ ] Update environment variables
- [ ] Change model name to `gemini-1.5-pro` or `gemini-1.5-flash`
- [ ] Test with CLI mode: `python -m slackagent.cli --repo /path --task "test"`
- [ ] Rebuild Docker image if needed
- [ ] Update any custom prompts if you modified them

## Rollback Instructions

If you need to switch back to Anthropic:

1. Checkout previous commit: `git checkout <commit-before-gemini>`
2. Or manually:
   - Change imports back to `anthropic`
   - Update config sections
   - Restore message format with system/user split
   - Update pyproject.toml dependency

## Notes

- **Prompt Engineering**: Gemini doesn't use separate system messages, so prompts are combined
- **Temperature**: Added explicit temperature=0.7 for better code generation variety
- **Token Counting**: Gemini and Anthropic count tokens differently; adjust budgets if needed
- **Context Length**: Gemini 1.5 supports up to 1M tokens (vs Claude's 200K), great for large codebases

## Testing

All existing tests have been updated and should pass:

```bash
pytest tests/
```

Test the integration:
```bash
# Planning only
python -m slackagent.cli --repo . --task "Add docstrings" --plan-only

# Full run
python -m slackagent.cli --repo . --task "Refactor config loading"
```
