# Getting a Google Gemini API Key

This project uses Google's Gemini AI models for code generation and planning. Here's how to get your API key:

## Step 1: Access Google AI Studio

1. Go to [Google AI Studio](https://makersuite.google.com/app/apikey)
2. Sign in with your Google account

## Step 2: Create an API Key

1. Click **"Get API Key"** or **"Create API Key"**
2. Choose to create a new API key in a new or existing Google Cloud project
3. Copy the generated API key (starts with `AIza...`)

⚠️ **Important**: Save this key securely. You won't be able to see it again!

## Step 3: Set Up Your Environment

Add the API key to your environment:

```bash
export GEMINI_API_KEY="AIzaSy..."
```

Or add it to your `config.yaml`:

```yaml
gemini:
  api_key: ${GEMINI_API_KEY}  # Reads from environment
  # Or directly (not recommended for production):
  # api_key: "AIzaSy..."
```

## Step 4: Verify Access

Test your API key:

```bash
python -c "import google.generativeai as genai; genai.configure(api_key='YOUR_KEY'); print('✅ API key works!')"
```

## Available Models

The project defaults to `gemini-1.5-pro`, but you can use other models:

- **gemini-1.5-pro**: Most capable, best for complex coding tasks
- **gemini-1.5-flash**: Faster and more cost-effective for simpler tasks
- **gemini-1.0-pro**: Previous generation, still capable

Change the model in `config.yaml`:

```yaml
agent:
  model: "gemini-1.5-flash"  # Faster, cheaper
  # or
  model: "gemini-1.5-pro"    # More capable (default)
```

## Pricing (as of 2024)

**Gemini 1.5 Pro**:
- Free tier: 50 requests per day
- Input: $3.50 per 1M tokens
- Output: $10.50 per 1M tokens

**Gemini 1.5 Flash**:
- Free tier: 1,500 requests per day
- Input: $0.35 per 1M tokens
- Output: $1.05 per 1M tokens

Check current pricing: https://ai.google.dev/pricing

## Rate Limits

Free tier includes:
- 15 requests per minute (RPM)
- 1 million tokens per minute (TPM)
- 1,500 requests per day (RPD)

For higher limits, enable billing in Google Cloud Console.

## Troubleshooting

### "API key not valid"
- Ensure you've copied the full key (starts with `AIza`)
- Check that the API key hasn't been restricted to specific IPs or referrers
- Verify the Gemini API is enabled in your Google Cloud project

### "Resource exhausted" or "Quota exceeded"
- You've hit the free tier daily limit
- Wait 24 hours or enable billing
- Consider using `gemini-1.5-flash` which has higher free tier limits

### "Model not found"
- Check the model name in your config
- Ensure you're using a valid model identifier
- Update `google-generativeai` package: `pip install -U google-generativeai`

## Security Best Practices

1. **Never commit API keys to version control**
   - Use environment variables
   - Add `config.yaml` to `.gitignore`

2. **Restrict API key usage** (in Google Cloud Console):
   - Limit to specific IPs if possible
   - Set up API restrictions
   - Monitor usage regularly

3. **Rotate keys periodically**
   - Create new keys every few months
   - Delete old keys after rotation

4. **Use separate keys for dev/prod**
   - Different keys for different environments
   - Easier to track and revoke

## Additional Resources

- [Gemini API Documentation](https://ai.google.dev/docs)
- [Google AI Studio](https://makersuite.google.com/)
- [Pricing Calculator](https://ai.google.dev/pricing)
- [Python SDK Documentation](https://ai.google.dev/tutorials/python_quickstart)
